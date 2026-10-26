"""Self-accumulating, human-labelled production recall dataset workflow."""

from __future__ import annotations

from dataclasses import replace, asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any
from eimemory.subject_projection_consumption import projection_is_current

from eimemory.adapters.runtime.channel import (
    SUPPORTED_RUNTIME_CHANNELS, resolve_channel_scope, runtime_channel_from_scope,
)
from eimemory.core.clock import now_iso
from eimemory.evaluation.real_query_schema import (
    PRODUCTION_REAL_QUERY_AUTO_REVIEW_LABELER,
    PRODUCTION_RECALL_AUTO_REVIEW_FLAG,
    PRODUCTION_REAL_QUERY_SCHEMA,
    PRODUCTION_REAL_QUERY_TRUSTED_LABELERS,
    production_real_query_label_authority,
    production_real_query_trusted_labelers,
    production_recall_auto_review_enabled,
    _bounded_query_features,
    production_real_query_feature_quality_reasons,
    production_real_query_active_channel_contract,
    _stable_digest,
)
from eimemory.governance.evidence_contract import _pin_scope_matches, same_scope
from eimemory.models.records import RecordEnvelope, ScopeRef


PENDING_QUERY_SCHEMA = "production_recall_pending_case.v1"
ACCEPTED_QUERY_SCHEMA = "production_recall_accepted_case.v1"
PENDING_SOURCE = "eimemory.production_recall.pending_case"
ACCEPTED_SOURCE = "eimemory.production_recall.accepted_case"
LABEL_EVIDENCE_SOURCE = "eimemory.production_recall.label_evidence"
PRODUCTION_QUERY_DATASET_POINTER_SCHEMA = "production_recall_dataset_pointer.v1"

_COLLECT_PENDING_PRODUCTION_QUERY_SQL = (
    "WITH selected_decisions AS ("
    "SELECT decision_id,channel,query_digest,task_type,source_ids_json,created_at,acceptance_generated,"
    "retrieval_diagnostics_json "
    "FROM proactive_decisions INDEXED BY idx_proactive_decisions_production_capture "
    "WHERE channel=? AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? "
    "AND release_bound=1 AND control_cohort=0 {capture_filter} "
    "ORDER BY created_at DESC,decision_id DESC LIMIT ?"
    ") SELECT d.decision_id,d.channel,d.query_digest,d.task_type,d.source_ids_json,d.created_at,d.acceptance_generated,"
    "d.retrieval_diagnostics_json,"
    "i.record_id,i.source_id FROM selected_decisions d "
    "LEFT JOIN proactive_decision_items i ON i.decision_id=d.decision_id "
    "AND i.rowid IN (SELECT rowid FROM proactive_decision_items "
    "WHERE decision_id=d.decision_id ORDER BY item_order,record_id LIMIT 5) "
    "ORDER BY d.created_at DESC,d.decision_id DESC,i.item_order ASC"
)


def _workspace_base(workspace_id: str) -> str:
    marker = "::channel::"
    text = str(workspace_id or "")
    return text.split(marker, 1)[0] if marker in text else text


def resolve_bound_logical_scope(scope: dict[str, Any] | ScopeRef | None) -> tuple[dict[str, str], dict[str, Any]]:
    """Use the release-binding logical user when closure passed the host account.

    Host-user pending stays historical. It is not accepted, deleted, or copied.
    An explicit non-host user, a missing binding file, or more than one bound
    user for the same tenant/agent/workspace is left unchanged.
    """

    import getpass
    from eimemory.adapters.runtime.host_auth import _read_private_file

    requested = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    payload = {
        "tenant_id": requested.tenant_id,
        "agent_id": requested.agent_id,
        "workspace_id": requested.workspace_id,
        "user_id": requested.user_id,
    }
    resolution: dict[str, Any] = {
        "requested_user_id": requested.user_id,
        "user_id": requested.user_id,
        "rewritten": False,
        "reason": "unchanged",
        "historical_host_scope": "not_applicable",
    }
    path = os.environ.get("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", "").strip()
    if not path:
        return payload, resolution
    try:
        host_user = getpass.getuser()
    except OSError:
        host_user = os.environ.get("USER", "")
    service_user = os.environ.get("EIMEMORY_DEPLOY_SCOPE_USER", "") or host_user
    if requested.user_id not in {host_user, service_user}:
        resolution["reason"] = "explicit_user_preserved"
        return payload, resolution
    try:
        raw = _read_private_file(Path(path), max_bytes=64 * 1024)
        bindings = json.loads(raw.decode()) if raw else None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        resolution["reason"] = "bindings_unreadable"
        return payload, resolution
    if not isinstance(bindings, list):
        resolution["reason"] = "bindings_unreadable"
        return payload, resolution
    wanted_base = _workspace_base(requested.workspace_id)
    users: set[str] = set()
    scope_fields = {"tenant_id", "agent_id", "workspace_id", "user_id"}
    for item in bindings:
        pinned = item.get("scope") if isinstance(item, dict) else None
        if (not isinstance(pinned, dict) or set(pinned) != scope_fields
                or not all(isinstance(value, str) and value for value in pinned.values())):
            continue
        try:
            bound = ScopeRef.from_dict(pinned)
        except ValueError:
            continue
        if ("::channel::" in bound.workspace_id
                and (not runtime_channel_from_scope(bound)
                     or bound.workspace_id.count("::channel::") != 1)):
            continue
        # Apply the existing directed agent-rename policy to the same base
        # workspace and candidate user. Only user translation is returned;
        # tenant, agent and workspace remain the caller's exact identities.
        target = replace(requested, workspace_id=wanted_base, user_id=bound.user_id)
        base_pin = {**pinned, "workspace_id": _workspace_base(bound.workspace_id)}
        if _pin_scope_matches(base_pin, target):
            users.add(bound.user_id)
    if len(users) != 1:
        resolution["reason"] = "binding_user_ambiguous" if len(users) > 1 else "binding_user_missing"
        return payload, resolution
    logical_user = next(iter(users))
    if logical_user == requested.user_id:
        resolution["reason"] = "already_logical_user"
        return payload, resolution
    payload["user_id"] = logical_user
    resolution.update({
        "user_id": logical_user,
        "rewritten": True,
        "reason": "host_user_replaced_by_bound_logical_user",
        "historical_host_scope": "excluded_not_accepted",
    })
    return payload, resolution


def collect_pending_production_queries(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    limit: int = 500,
    channel: str | None = None,
    decision_id: str | None = None,
    include_maintenance: bool = False,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Project real proactive audits into digest-only pending label cases."""

    requested = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    resolved_scope, scope_resolution = resolve_bound_logical_scope(requested)
    base = ScopeRef.from_dict(resolved_scope)
    if channel is not None and channel not in SUPPORTED_RUNTIME_CHANNELS:
        raise ValueError("supported exact channel required")
    if (decision_id is not None or include_maintenance) and channel is None:
        raise ValueError("exact channel required for controlled capture")
    if include_maintenance and not decision_id:
        raise ValueError("exact decision required for maintenance projection")
    if source_id is not None and (channel is None or not source_id or source_id == '*'):
        raise ValueError('exact channel and source required')
    channels = [channel] if channel else sorted(SUPPORTED_RUNTIME_CHANNELS)
    exact_capture_requested = channel is not None
    capture_filter = '' if include_maintenance else 'AND acceptance_generated=0 '
    capture_filter += 'AND decision_id=? ' if decision_id is not None else ''
    capture_filter += ("AND json_valid(source_ids_json) AND json_array_length(source_ids_json)=1 "
                       "AND json_extract(source_ids_json,'$[0]')=? ") if source_id is not None else ''
    rows: list[dict[str, Any]] = []
    bounded = max(1, min(500, int(limit)))
    locked = getattr(runtime.store, "locked", None)
    if not callable(locked):
        return {"ok": False, "reason": "proactive_audit_store_unavailable", "created": 0, "pending_record_ids": []}
    with locked() as sqlite:
        for selected_channel in channels:
            exact = ScopeRef.from_dict(resolve_channel_scope(selected_channel, asdict(base)))
            selected = sqlite.execute(
                _COLLECT_PENDING_PRODUCTION_QUERY_SQL.format(capture_filter=capture_filter),
                (
                    selected_channel,
                    exact.tenant_id,
                    exact.agent_id,
                    exact.workspace_id,
                    exact.user_id,
                    *([decision_id] if decision_id is not None else []),
                    *([source_id] if source_id is not None else []),
                    bounded,
                ),
            ).fetchall()
            rows.extend(dict(row) for row in selected)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("decision_id") or ""), []).append(row)
    created: list[str] = []
    new_ids: list[str] = []
    new_case_refs: list[dict[str, Any]] = []
    pin_ids: list[str] = []
    skipped: dict[str, int] = {}
    empty_result_count = 0
    empty_reason_counts: dict[str, int] = {}
    session_deduped_count = 0
    for decision_id, items in grouped.items():
        first = items[0]
        channel = str(first.get("channel") or "")
        try:
            sources = [str(item) for item in json.loads(str(first.get("source_ids_json") or "[]"))]
        except (TypeError, ValueError, json.JSONDecodeError):
            sources = []
        if len(sources) != 1 or sources[0] == "*":
            skipped["non_exact_source"] = skipped.get("non_exact_source", 0) + 1
            continue
        source_id = sources[0]
        query_digest = str(first.get("query_digest") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", query_digest):
            skipped["query_digest_invalid"] = skipped.get("query_digest_invalid", 0) + 1
            continue
        exact_scope = ScopeRef.from_dict(resolve_channel_scope(channel, asdict(base)))
        refs: list[str] = []
        valid = True
        for item in items[:5]:
            if item.get("record_id") is None and item.get("source_id") is None:
                continue  # A real decision with no returned item remains observable.
            if str(item.get("source_id") or "") != source_id:
                valid = False
                break
            # The scoped decision audit is historical observation authority.
            # A returned memory may since have expired or been removed. Current
            # answer/label authority is checked separately when accepting gold.
            ref = str(item.get("record_id") or "")
            if not ref:
                valid = False
                break
            if ref not in refs:
                refs.append(ref)
        if not valid:
            skipped["candidate_boundary_invalid"] = skipped.get("candidate_boundary_invalid", 0) + 1
            continue
        task_type = str(first.get("task_type") or "").strip()[:80]
        if not task_type:
            skipped["task_type_unclassified"] = skipped.get("task_type_unclassified", 0) + 1
            continue
        empty_reason = ""
        if not refs:
            empty_reason = _empty_decision_reason(first.get("retrieval_diagnostics_json"))
            if empty_reason == "session_deduped_prior_delivery":
                # Retrieval selected evidence, but every selected item had
                # already been delivered earlier in this session, so nothing
                # was recorded for this turn. The case stays observable (and is
                # closed not-evaluable by review), but it is not an empty recall.
                session_deduped_count += 1
            else:
                empty_result_count += 1
                empty_reason_counts[empty_reason] = empty_reason_counts.get(empty_reason, 0) + 1
        record_id = "prqp_" + _stable_digest({"schema": PENDING_QUERY_SCHEMA, "decision_id": decision_id, "query_digest": query_digest})[:32]
        pending = RecordEnvelope.create(
            kind="evaluation_packet",
            title=f"Pending production recall label {channel}",
            summary="Digest-only real proactive query awaiting operator relevance labels.",
            content={
                "schema": PENDING_QUERY_SCHEMA,
                "case_id": "real-" + _stable_digest({"decision_id": decision_id, "query_digest": query_digest})[:24],
                "channel": channel,
                "source_id": source_id,
                "scope": asdict(exact_scope),
                "capture_query_digest": query_digest,
                "suggested_query_features": {
                    "terms": [term for term in re.split(r"[^a-zA-Z0-9_.-]+", task_type) if term][:8] or ["unclassified"],
                    "intent": "production recall",
                },
                "candidate_refs": refs,
                "capture_ref": decision_id,
                "captured_at": str(first.get("created_at") or now_iso())[:80],
                "collector": "proactive_audit_capture",
                "acceptance_generated": (None if first.get("acceptance_generated") is None else
                                         bool(first["acceptance_generated"])),
            },
            source=PENDING_SOURCE,
            source_id=source_id,
            scope=exact_scope,
            status="active",
            evidence=refs,
            meta={
                "report_type": "production_recall_pending_case",
                "schema": PENDING_QUERY_SCHEMA,
                "channel": channel,
                "capture_ref": decision_id,
                "query_digest": query_digest,
            },
        )
        pending.record_id = record_id
        existing = runtime.store.get_by_id(record_id, scope=exact_scope)
        if existing is None:
            runtime.store.append(pending)
            created.append(record_id)
            new_ids.append(record_id)
            new_case_refs.append({"pending_record_id": record_id, "capture_ref": decision_id,
                                  "candidate_count": len(refs)})
            pin_ids.append(decision_id)
        else:
            created.append(record_id)
            if getattr(existing, "status", "") == "active":
                pin_ids.append(decision_id)
    retention_pins = _pin_case_decisions(runtime, base=base, channels=channels, pending_decision_ids=pin_ids)
    return {
        "ok": True,
        "new_count": len(new_ids),
        "existing_count": len(created) - len(new_ids),
        "new_pending_record_ids": sorted(new_ids),
        "new_nonempty_count": sum(case["candidate_count"] > 0 for case in new_case_refs),
        "new_empty_count": sum(case["candidate_count"] == 0 for case in new_case_refs),
        "new_case_refs": sorted(new_case_refs, key=lambda case: (
            case["candidate_count"] == 0, case["pending_record_id"])),
        "retention_pins": retention_pins,
        "created": len(created),
        "decision_count": len(grouped),
        "empty_result_count": empty_result_count,
        "empty_reason_counts": dict(sorted(empty_reason_counts.items())),
        "session_deduped_count": session_deduped_count,
        "pending_record_ids": sorted(created),
        "skipped": dict(sorted(skipped.items())),
        "scope_resolution": scope_resolution,
        "explicit": ({"status":"not_requested"} if exact_capture_requested else
                     _collect_explicit_queries(runtime, scope=base, limit=bounded)),
    }


def _empty_decision_reason(raw: Any) -> str:
    """Classify a decision with no recorded items from its stage diagnostics.

    Uses only bounded, digest-free diagnostic labels (never query text).
    """
    try:
        diagnostics = json.loads(str(raw or "")) if raw else None
    except (TypeError, ValueError, json.JSONDecodeError):
        diagnostics = None
    if not isinstance(diagnostics, dict) or not diagnostics:
        return "diagnostics_unavailable"
    post = diagnostics.get("post_selection") if isinstance(diagnostics.get("post_selection"), dict) else {}
    dedupe = diagnostics.get("session_dedupe") if isinstance(diagnostics.get("session_dedupe"), dict) else {}

    def _count(mapping: dict, key: str) -> int:
        value = mapping.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    deduped = _count(post, "session_deduped_count")
    # post_selection is the caller-observed authority; the session_dedupe
    # witness block only exists on decisions recorded since it was added, so
    # it corroborates when present but is not required.
    if (deduped and not _count(post, "render_input_count") and not _count(post, "item_limit_input_count")
            and deduped == _count(post, "session_dedupe_input_count")
            and ("removed_count" not in dedupe or _count(dedupe, "removed_count"))):
        return "session_deduped_prior_delivery"
    selector = diagnostics.get("selector") if isinstance(diagnostics.get("selector"), dict) else {}
    if (not _count(selector, "input_count") and selector.get("status") == "no_evidence"
            and not selector.get("dropped_reasons")):
        # Nothing reached the selector: name the earlier stage that emptied it.
        stages = diagnostics.get("pipeline") if isinstance(diagnostics.get("pipeline"), list) else []
        by_name = {str(st.get("name")): st for st in stages if isinstance(st, dict)}
        retrieved = _count(by_name.get("retrieve", {}), "retrieved_count")
        if not retrieved:
            return "pre_selector:retrieve_empty"
        if "graph_expand" in by_name and not _count(by_name["graph_expand"], "candidate_count"):
            return "pre_selector:graph_expand_empty"
        blocked = by_name.get("score_filter", {}).get("blocked_counts")
        if isinstance(blocked, dict):
            top = sorted((k for k, v in blocked.items() if isinstance(k, str)
                          and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", k) and isinstance(v, int) and v > 0),
                         key=lambda k: (-blocked[k], k))
            if top:
                return f"pre_selector:score_filter:{top[0]}"
        return "pre_selector:unattributed"
    status = str(selector.get("status") or diagnostics.get("retrieval_status") or "unknown")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", status):
        status = "unknown"
    reasons = selector.get("dropped_reasons") if isinstance(selector.get("dropped_reasons"), dict) else {}
    top = sorted((key for key, value in reasons.items()
                  if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", key)
                  and isinstance(value, int) and value > 0),
                 key=lambda key: (-reasons[key], key))
    return f"selector_{status}:{top[0]}" if top else f"selector_{status}"


def _pin_case_decisions(runtime: Any, *, base: ScopeRef, channels: list[str],
                        pending_decision_ids: list[str], limit: int = 500) -> dict[str, Any]:
    """Keep decisions that open/accepted cases depend on out of the ring prune.

    Capture and acceptance validation re-read the authoritative decision, so a
    pruned decision silently turns a case into ``pending_capture_decision_missing``.
    Pins are bounded by the store (see MAX_PINNED_PROACTIVE_DECISIONS).
    """
    pin = getattr(runtime.store, "pin_proactive_decisions", None)
    if not callable(pin):
        return {"status": "unavailable"}
    accepted_ids: list[str] = []
    for channel in channels:
        exact = ScopeRef.from_dict(resolve_channel_scope(channel, asdict(base)))
        records = runtime.store.list_records_by_meta_value(
            kinds=["evaluation_packet"], scope=exact, meta_key="report_type",
            meta_value="production_recall_accepted_case", status="active", limit=limit) or []
        for record in records:
            if record.source != ACCEPTED_SOURCE or not same_scope(record.scope, exact):
                continue
            case = record.content.get("case") if isinstance(record.content, dict) else None
            provenance = case.get("provenance") if isinstance(case, dict) else None
            ref = str((provenance or {}).get("capture_ref") or "") if isinstance(provenance, dict) else ""
            if re.fullmatch(r"pd:[0-9a-f]{32}", ref):
                accepted_ids.append(ref)
    pending_report = pin(pending_decision_ids, reason="pending_case")
    accepted_report = pin(accepted_ids, reason="accepted_case")
    return {"status": "applied", "pending_case": pending_report, "accepted_case": accepted_report}


def _collect_explicit_queries(runtime: Any, *, scope: ScopeRef, limit: int) -> dict:
    from eimemory.evaluation.explicit_recall import collect_explicit_queries
    return collect_explicit_queries(runtime, scope=scope, limit=limit)


def accept_pending_production_query(
    runtime: Any,
    *,
    pending_record_id: str,
    query_features: dict[str, Any],
    labels: list[dict[str, Any]],
    labeler: str,
    operator_scope: dict[str, Any] | ScopeRef | None,
    label_packet_evidence: dict[str, Any],
) -> dict[str, Any]:
    labeler_id = str(labeler or "").strip()
    if labeler_id not in PRODUCTION_REAL_QUERY_TRUSTED_LABELERS:
        raise ValueError("trusted operator labeler required")
    pending = runtime.store.get_by_id(str(pending_record_id or ""))
    if pending is None or pending.kind != "evaluation_packet" or pending.source != PENDING_SOURCE or pending.status != "active":
        raise ValueError("trusted pending production query required")
    payload = pending.content if isinstance(pending.content, dict) else {}
    if payload.get("schema") != PENDING_QUERY_SCHEMA:
        raise ValueError("pending production query schema mismatch")
    bounded_features, reason = _bounded_query_features(query_features)
    if reason:
        raise ValueError(reason)
    quality_reasons = production_real_query_feature_quality_reasons(bounded_features)
    if quality_reasons:
        raise ValueError(str(quality_reasons[0]))
    source_id = str(payload.get("source_id") or "")
    channel = str(payload.get("channel") or "")
    exact_scope = ScopeRef.from_dict(payload.get("scope") or {})
    if not same_scope(pending.scope, exact_scope) and _alias_equivalent_scope(pending.scope, exact_scope):
        # Configured agent rename (user-authorized alias binding): the case
        # lives under the canonical agent while its capture names the legacy id.
        exact_scope = pending.scope
    base_scope = operator_scope if isinstance(operator_scope, ScopeRef) else ScopeRef.from_dict(operator_scope)
    authorized_scope = ScopeRef.from_dict(resolve_channel_scope(channel, asdict(base_scope)))
    evidence_digest = str(label_packet_evidence.get("digest") or label_packet_evidence.get("sha256") or "").lower()
    file_evidence = (
        label_packet_evidence.get("schema") == "secure_dataset_fingerprint.v1"
        and re.fullmatch(r"[0-9a-f]{64}", evidence_digest) is not None
        and type(label_packet_evidence.get("size")) is int
        and int(label_packet_evidence.get("size") or 0) > 0
        and type(label_packet_evidence.get("device")) is int
        and type(label_packet_evidence.get("inode")) is int
    )
    if not (
        source_id
        and same_scope(pending.scope, exact_scope)
        and same_scope(exact_scope, authorized_scope)
        and file_evidence
    ):
        raise ValueError("pending query boundary mismatch")
    capture_reason = pending_production_query_capture_validation_error(
        runtime, pending, exact_scope=exact_scope, channel=channel)
    if capture_reason:
        raise ValueError(capture_reason)
    if not isinstance(labels, list) or not 1 <= len(labels) <= 16:
        raise ValueError("operator labels count invalid")
    normalized_labels: list[dict[str, Any]] = []
    prepared_evidence = []
    seen: set[str] = set()
    accepted_at = now_iso()
    for raw in labels:
        ref = str(raw.get("record_ref") or "") if isinstance(raw, dict) else ""
        grade = raw.get("grade") if isinstance(raw, dict) else None
        if not ref or ref in seen or isinstance(grade, bool) or not isinstance(grade, int) or not 1 <= grade <= 3:
            raise ValueError("operator label invalid")
        record = runtime.store.get_by_id(ref, scope=exact_scope)
        if (record is None or record.status != "active" or record.source_id != source_id
                or not _alias_equivalent_scope(record.scope, exact_scope)
                or not projection_is_current(runtime.store, record)):
            raise ValueError("operator label boundary mismatch")
        seen.add(ref)
        evidence_id = "prle_" + _stable_digest(
            {"pending_record_id": pending.record_id, "record_ref": ref, "grade": grade, "labeler": labeler_id}
        )[:32]
        from dataclasses import asdict as _asdict
        from eimemory.evaluation.label_authority import sign_operator_label
        operator_packet = {
            "schema": "secure_dataset_fingerprint.v1",
            "digest": evidence_digest,
            "size": int(label_packet_evidence["size"]),
            "device": int(label_packet_evidence["device"]),
            "inode": int(label_packet_evidence["inode"]),
        }
        operator_authority = sign_operator_label({
            "scope": _asdict(exact_scope),
            "source_id": source_id,
            "label": {
                "pending_record_id": pending.record_id,
                "record_ref": ref,
                "grade": grade,
                "labeler": labeler_id,
            },
            "operator_packet_evidence": operator_packet,
        })
        evidence_content = {
            "evidence_class": "operator_relevance_label",
            "labeler": labeler_id,
            "pending_record_id": pending.record_id,
            "record_ref": ref,
            "grade": grade,
            "operator_packet_evidence": operator_packet,
        }
        evidence_content["operator_authority"] = operator_authority
        evidence = RecordEnvelope.create(
            kind="evaluation_packet",
            title=f"Trusted production recall label {channel}",
            summary="Operator accepted one exact relevance label.",
            content=evidence_content,
            source=LABEL_EVIDENCE_SOURCE,
            source_id=source_id,
            scope=exact_scope,
            status="active",
            evidence=[pending.record_id, ref],
            meta={
                "report_type": "production_recall_label_evidence",
                "authoritative": True,
                "operator_packet_digest": evidence_digest,
            },
        )
        evidence.record_id = evidence_id
        from .label_authority import label_authority_error
        existing = runtime.store.get_by_id(evidence_id, scope=exact_scope)
        label_reason = label_authority_error(existing or evidence, scope=exact_scope,
            source_id=source_id, pending_id=pending.record_id, record_ref=ref,
            grade=grade, labeler=labeler_id)
        if label_reason:
            raise ValueError(label_reason)
        if existing is None:
            prepared_evidence.append(evidence)
        normalized_labels.append(
            {
                "record_ref": ref,
                "grade": grade,
                "accepted": True,
                "provenance": {"labeler": labeler_id, "labelled_at": accepted_at, "evidence_ref": evidence_id},
            }
        )
    if not normalized_labels:
        raise ValueError("at least one operator label required")
    accepted = _accepted_case_record(
        pending, payload, exact_scope=exact_scope, source_id=source_id, channel=channel,
        bounded_features=bounded_features, normalized_labels=normalized_labels, accepted_at=accepted_at,
    )
    accepted_id = accepted.record_id
    case = accepted.content["case"]
    # Validate the whole operator packet before persisting any of its labels.
    for evidence in prepared_evidence:
        runtime.store.append(evidence)
    if runtime.store.get_by_id(accepted_id, scope=exact_scope) is None:
        runtime.store.append(accepted)
    return {"ok": True, "record_id": accepted_id, "case_id": case["case_id"], "channel": channel}



def _accepted_case_record(
    pending: RecordEnvelope,
    payload: dict[str, Any],
    *,
    exact_scope: ScopeRef,
    source_id: str,
    channel: str,
    bounded_features: dict[str, Any],
    normalized_labels: list[dict[str, Any]],
    accepted_at: str,
    label_authority: str | None = None,
) -> RecordEnvelope:
    """Build the accepted case record shared by operator and auto-review paths."""

    started_at = str(payload.get("captured_at") or accepted_at)
    try:
        if datetime.fromisoformat(started_at.replace("Z", "+00:00")) >= datetime.fromisoformat(accepted_at.replace("Z", "+00:00")):
            started_at = (datetime.fromisoformat(accepted_at) - timedelta(seconds=1)).isoformat()
    except ValueError:
        started_at = (datetime.fromisoformat(accepted_at) - timedelta(seconds=1)).isoformat()
    case = {
        "case_id": str(payload.get("case_id") or ""),
        "collection_window": {"started_at": started_at, "ended_at": accepted_at},
        "channel": channel,
        "source_id": source_id,
        "scope": asdict(exact_scope),
        "query_features": bounded_features,
        "query_digest": _stable_digest(bounded_features),
        "labels": normalized_labels,
        "provenance": {"collector": "proactive_audit_capture", "capture_ref": str(payload.get("capture_ref") or pending.record_id)},
    }
    accepted_id = "prqa_" + _stable_digest({"schema": ACCEPTED_QUERY_SCHEMA, "case": case})[:32]
    meta = {"report_type": "production_recall_accepted_case", "schema": ACCEPTED_QUERY_SCHEMA, "channel": channel, "case_id": case["case_id"]}
    if label_authority:
        meta["label_authority"] = label_authority
    accepted = RecordEnvelope.create(
        kind="evaluation_packet",
        title=f"Accepted production recall case {channel}",
        summary=("Auto-reviewed redacted production recall case." if label_authority == "auto_review"
                 else "Authority-labelled redacted production recall case."),
        content={"schema": ACCEPTED_QUERY_SCHEMA, "case": case},
        source=ACCEPTED_SOURCE,
        source_id=source_id,
        scope=exact_scope,
        status="active",
        evidence=[pending.record_id, *[item["record_ref"] for item in normalized_labels]],
        meta=meta,
    )
    accepted.record_id = accepted_id
    return accepted


def accept_auto_reviewed_production_query(
    runtime: Any,
    *,
    pending_record_id: str,
    query_features: dict[str, Any],
    labels: list[dict[str, Any]],
    auto_review_packet: dict[str, Any],
) -> dict[str, Any]:
    """Persist labels produced by the deterministic auto-reviewer.

    Distinct from operator acceptance: the labeler is ``auto_review``, the
    evidence class is ``auto_review_relevance_label`` and each label carries a
    signed packet (criteria version, inputs digest, per-candidate signals).
    Only returned candidates of the exact captured decision may be labelled.
    """

    from .label_authority import (
        AUTO_REVIEW_EVIDENCE_CLASS, auto_review_packet_error, label_authority_error,
        sign_auto_review_label,
    )
    from .real_query_schema import (
        PRODUCTION_REAL_QUERY_AUTO_REVIEW_LABELER, production_recall_auto_review_enabled,
    )

    if not production_recall_auto_review_enabled():
        raise ValueError("auto_review_disabled")
    labeler_id = PRODUCTION_REAL_QUERY_AUTO_REVIEW_LABELER
    pending = runtime.store.get_by_id(str(pending_record_id or ""))
    if pending is None or pending.kind != "evaluation_packet" or pending.source != PENDING_SOURCE or pending.status != "active":
        raise ValueError("trusted pending production query required")
    payload = pending.content if isinstance(pending.content, dict) else {}
    if payload.get("schema") != PENDING_QUERY_SCHEMA:
        raise ValueError("pending production query schema mismatch")
    bounded_features, reason = _bounded_query_features(query_features)
    if reason:
        raise ValueError(reason)
    quality_reasons = production_real_query_feature_quality_reasons(bounded_features)
    if quality_reasons:
        raise ValueError(str(quality_reasons[0]))
    source_id = str(payload.get("source_id") or "")
    channel = str(payload.get("channel") or "")
    exact_scope = ScopeRef.from_dict(payload.get("scope") or {})
    if not source_id or not same_scope(pending.scope, exact_scope):
        raise ValueError("pending query boundary mismatch")
    packet_reason = auto_review_packet_error(auto_review_packet, pending_id=pending.record_id)
    if packet_reason:
        raise ValueError(packet_reason)
    capture_reason = pending_production_query_capture_validation_error(
        runtime, pending, exact_scope=exact_scope, channel=channel)
    if capture_reason:
        raise ValueError(capture_reason)
    candidate_refs = [str(item) for item in payload.get("candidate_refs") or []]
    if not isinstance(labels, list) or not 1 <= len(labels) <= 5:
        raise ValueError("auto_review labels count invalid")
    packet = dict(auto_review_packet)
    packet_digest = _stable_digest(packet)
    normalized_labels: list[dict[str, Any]] = []
    prepared_evidence = []
    seen: set[str] = set()
    accepted_at = now_iso()
    for raw in labels:
        ref = str(raw.get("record_ref") or "") if isinstance(raw, dict) else ""
        grade = raw.get("grade") if isinstance(raw, dict) else None
        if (not ref or ref in seen or ref not in candidate_refs or ref not in packet["signals"]
                or isinstance(grade, bool) or not isinstance(grade, int) or not 2 <= grade <= 3):
            raise ValueError("auto_review label invalid")
        record = runtime.store.get_by_id(ref, scope=exact_scope)
        if (record is None or record.status != "active" or record.source_id != source_id
                or not _alias_equivalent_scope(record.scope, exact_scope)
                or not projection_is_current(runtime.store, record)):
            raise ValueError("auto_review label boundary mismatch")
        seen.add(ref)
        evidence_id = "prle_" + _stable_digest(
            {"pending_record_id": pending.record_id, "record_ref": ref, "grade": grade, "labeler": labeler_id}
        )[:32]
        label_identity = {
            "pending_record_id": pending.record_id,
            "record_ref": ref,
            "grade": grade,
            "labeler": labeler_id,
        }
        authority = sign_auto_review_label({
            "scope": asdict(exact_scope),
            "source_id": source_id,
            "label": label_identity,
            "auto_review_packet": packet,
        })
        evidence = RecordEnvelope.create(
            kind="evaluation_packet",
            title=f"Auto-reviewed production recall label {channel}",
            summary="Deterministic auto-review accepted one exact relevance label.",
            content={
                "evidence_class": AUTO_REVIEW_EVIDENCE_CLASS,
                **label_identity,
                "auto_review_packet": packet,
                "auto_review_authority": authority,
            },
            source=LABEL_EVIDENCE_SOURCE,
            source_id=source_id,
            scope=exact_scope,
            status="active",
            evidence=[pending.record_id, ref],
            meta={
                "report_type": "production_recall_label_evidence",
                "authoritative": True,
                "label_authority": "auto_review",
                "auto_review_packet_digest": packet_digest,
                "criteria_version": packet["criteria_version"],
            },
        )
        evidence.record_id = evidence_id
        existing = runtime.store.get_by_id(evidence_id, scope=exact_scope)
        label_reason = label_authority_error(existing or evidence, scope=exact_scope,
            source_id=source_id, pending_id=pending.record_id, record_ref=ref,
            grade=grade, labeler=labeler_id)
        if label_reason:
            raise ValueError(label_reason)
        if existing is None:
            prepared_evidence.append(evidence)
        normalized_labels.append(
            {
                "record_ref": ref,
                "grade": grade,
                "accepted": True,
                "provenance": {"labeler": labeler_id, "labelled_at": accepted_at, "evidence_ref": evidence_id},
            }
        )
    accepted = _accepted_case_record(
        pending, payload, exact_scope=exact_scope, source_id=source_id, channel=channel,
        bounded_features=bounded_features, normalized_labels=normalized_labels,
        accepted_at=accepted_at, label_authority="auto_review",
    )
    for evidence in prepared_evidence:
        runtime.store.append(evidence)
    if runtime.store.get_by_id(accepted.record_id, scope=exact_scope) is None:
        runtime.store.append(accepted)
    return {"ok": True, "record_id": accepted.record_id, "case_id": accepted.content["case"]["case_id"],
            "channel": channel, "label_authority": "auto_review",
            "evidence_ids": [item["provenance"]["evidence_ref"] for item in normalized_labels]}

def validated_production_capture_identity(runtime, pending, *, exact_scope, channel):
    """Return an event key only after the existing live capture authority checks."""
    reason = pending_production_query_capture_validation_error(
        runtime, pending, exact_scope=exact_scope, channel=channel)
    if reason:
        raise ValueError(reason)
    return (channel, exact_scope.tenant_id, exact_scope.agent_id,
            exact_scope.workspace_id, exact_scope.user_id, pending.source_id,
            pending.content['capture_ref'])


def require_distinct_production_capture_identities(identities):
    """Reject repeated events, including conflicting labels; never select a winner."""
    seen = set()
    for identity in identities:
        if (not isinstance(identity, tuple) or len(identity) != 7
                or not all(isinstance(value, str) for value in identity)
                or identity[0] not in SUPPORTED_RUNTIME_CHANNELS
                or not identity[5] or identity[5] == '*'
                or re.fullmatch(r'pd:[0-9a-f]{32}', identity[6]) is None):
            raise ValueError('production_capture_identity_invalid')
        if identity in seen:
            raise ValueError('duplicate_production_capture')
        seen.add(identity)


def _alias_equivalent_scope(left: Any, right: Any) -> bool:
    """Exact scope, or the same scope under a configured agent rename.

    User-authorized policy (2026-10-16): evidence written under a legacy agent
    id (``legacy_agent_ids`` from settings) binds to its canonical agent. All
    other scope dimensions must still match exactly.
    """
    from eimemory.identity import canonical_agent_id

    try:
        a = left if isinstance(left, ScopeRef) else ScopeRef.from_dict(dict(left))
        b = right if isinstance(right, ScopeRef) else ScopeRef.from_dict(dict(right))
    except Exception:  # noqa: BLE001
        return False
    return (a.tenant_id == b.tenant_id and a.workspace_id == b.workspace_id and a.user_id == b.user_id
            and bool(a.agent_id) and canonical_agent_id(a.agent_id) == canonical_agent_id(b.agent_id))


def _decision_agent_ids(agent_id: str) -> list[str]:
    from eimemory.identity import canonical_agent_id, legacy_agent_ids

    ids = [agent_id]
    if canonical_agent_id(agent_id) == agent_id:
        ids.extend(legacy for legacy in legacy_agent_ids() if canonical_agent_id(legacy) == agent_id and legacy != agent_id)
    return ids


def pending_production_query_capture_validation_error(
    runtime: Any,
    pending: RecordEnvelope,
    *,
    exact_scope: ScopeRef,
    channel: str,
) -> str:
    """Bind a pending case to its authoritative proactive decision and items."""

    payload = pending.content if isinstance(pending.content, dict) else {}
    capture_ref = payload.get("capture_ref")
    query_digest = str(payload.get("capture_query_digest") or "").lower()
    source_id = str(payload.get("source_id") or "")
    candidate_refs = payload.get("candidate_refs")
    if (
        pending.kind != "evaluation_packet"
        or pending.status != "active"
        or pending.source != PENDING_SOURCE
        or pending.source_id != source_id
        or not _alias_equivalent_scope(pending.scope, exact_scope)
        or payload.get("schema") != PENDING_QUERY_SCHEMA
        or str(payload.get("channel") or "") != channel
        or not isinstance(payload.get("scope"), dict)
        or not _alias_equivalent_scope(payload["scope"], exact_scope)
        # ProactiveRecallService.decide produces pd: plus 32 lowercase hex.
        # Do not coerce, normalize, or substitute a label/pending record ID.
        or not isinstance(capture_ref, str)
        or re.fullmatch(r"pd:[0-9a-f]{32}", capture_ref) is None
        or re.fullmatch(r"[0-9a-f]{64}", query_digest) is None
        or not isinstance(candidate_refs, list)
        or not 0 <= len(candidate_refs) <= 5
        or len({str(item) for item in candidate_refs}) != len(candidate_refs)
        or payload.get("collector") != "proactive_audit_capture"
        or pending.meta.get("report_type") != "production_recall_pending_case"
        or pending.meta.get("schema") != PENDING_QUERY_SCHEMA
        or str(pending.meta.get("channel") or "") != channel
        or str(pending.meta.get("capture_ref") or "") != capture_ref
        or str(pending.meta.get("query_digest") or "").lower() != query_digest
        or [str(item) for item in pending.evidence] != [str(item) for item in candidate_refs]
    ):
        return "pending_capture_boundary_invalid"
    locked = getattr(runtime.store, "locked", None)
    if not callable(locked):
        return "pending_capture_authority_unavailable"
    rows = []
    for decision_agent in _decision_agent_ids(exact_scope.agent_id):
        rows = _capture_decision_rows(
            locked, capture_ref=capture_ref, scope=replace(exact_scope, agent_id=decision_agent),
            channel=channel, source_id=source_id,
        )
        if rows:
            break
    if not rows:
        return "pending_capture_decision_missing"
    return _validate_capture_rows(rows, payload=payload, exact_scope=exact_scope, channel=channel,
                                  source_id=source_id, capture_ref=capture_ref, query_digest=query_digest,
                                  candidate_refs=candidate_refs, pending=pending)


def _capture_decision_rows(locked: Any, *, capture_ref: str, scope: ScopeRef, channel: str, source_id: str) -> list:
    with locked() as sqlite:
        return sqlite.execute(
            "SELECT d.decision_id,d.channel,d.query_digest,d.task_type,d.source_ids_json,d.created_at,"
            "d.release_bound,d.control_cohort,d.tenant_id,d.agent_id,d.workspace_id,d.user_id,d.acceptance_generated,"
            "i.record_id,i.source_id,i.item_order "
            "FROM proactive_decisions d LEFT JOIN proactive_decision_items i ON i.decision_id=d.decision_id "
            "WHERE d.decision_id=? AND d.tenant_id=? AND d.agent_id=? AND d.workspace_id=? "
            "AND d.user_id=? AND d.channel=? AND json_valid(d.source_ids_json) "
            "AND json_array_length(d.source_ids_json)=1 AND json_extract(d.source_ids_json,'$[0]')=? "
            "ORDER BY i.item_order ASC,i.record_id ASC",
            (capture_ref, *asdict(scope).values(), channel, source_id),
        ).fetchall()


def _validate_capture_rows(rows: list, *, payload: dict, exact_scope: ScopeRef, channel: str, source_id: str,
                           capture_ref: str, query_digest: str, candidate_refs: Any, pending: Any) -> str:
    from eimemory.identity import canonical_agent_id

    first = dict(rows[0])
    if first.get('acceptance_generated') or payload.get('acceptance_generated'):
        return "maintenance_capture_not_natural"
    if channel == 'codex' and first.get('acceptance_generated') is None:
        return "pending_capture_provenance_unknown"
    try:
        source_ids = [str(item) for item in json.loads(str(first.get("source_ids_json") or "[]"))]
    except (TypeError, ValueError, json.JSONDecodeError):
        return "pending_capture_source_authority_invalid"
    authoritative_scope = (
        str(first.get("tenant_id") or ""),
        canonical_agent_id(str(first.get("agent_id") or "")),
        str(first.get("workspace_id") or ""),
        str(first.get("user_id") or ""),
    )
    expected_scope = (
        exact_scope.tenant_id,
        canonical_agent_id(exact_scope.agent_id),
        exact_scope.workspace_id,
        exact_scope.user_id,
    )
    task_type = str(first.get("task_type") or "").strip()[:80]
    expected_features = {
        "terms": [term for term in re.split(r"[^a-zA-Z0-9_.-]+", task_type) if term][:8] or ["unclassified"],
        "intent": "production recall",
    }
    authoritative_refs: list[str] = []
    for row_value in rows:
        row = dict(row_value)
        if row.get("record_id") is None and row.get("source_id") is None:
            continue
        if str(row.get("source_id") or "") != source_id:
            return "pending_capture_item_source_mismatch"
        ref = str(row.get("record_id") or "")
        if ref and ref not in authoritative_refs:
            authoritative_refs.append(ref)
        if len(authoritative_refs) == 5:
            break
    if (
        str(first.get("channel") or "") != channel
        or authoritative_scope != expected_scope
        or int(first.get("release_bound") or 0) != 1
        or int(first.get("control_cohort") or 0) != 0
        or str(first.get("query_digest") or "").lower() != query_digest
        or source_ids != [source_id]
        or not task_type
        or payload.get("suggested_query_features") != expected_features
        or [str(item) for item in candidate_refs] != authoritative_refs
        or str(payload.get("captured_at") or "") != str(first.get("created_at") or "")[:80]
    ):
        return "pending_capture_decision_mismatch"
    expected_case_id = "real-" + _stable_digest({"decision_id": capture_ref, "query_digest": query_digest})[:24]
    expected_pending_id = "prqp_" + _stable_digest(
        {"schema": PENDING_QUERY_SCHEMA, "decision_id": capture_ref, "query_digest": query_digest}
    )[:32]
    if str(payload.get("case_id") or "") != expected_case_id or pending.record_id != expected_pending_id:
        return "pending_capture_record_identity_invalid"
    return ""


def accepted_production_query_validation_error(
    runtime: Any,
    record: RecordEnvelope,
    *,
    exact_scope: ScopeRef,
    channel: str,
) -> str:
    """Validate the complete pending→label→accepted production authority chain."""

    content = record.content if isinstance(record.content, dict) else {}
    case = content.get("case") if isinstance(content.get("case"), dict) else {}
    if set(content) != {"schema", "case"} or content.get("schema") != ACCEPTED_QUERY_SCHEMA:
        return "accepted_schema_mismatch"
    expected_case_fields = {
        "case_id",
        "collection_window",
        "channel",
        "source_id",
        "scope",
        "query_features",
        "query_digest",
        "labels",
        "provenance",
    }
    if set(case) != expected_case_fields:
        return "accepted_case_fields_invalid"
    source_id = str(case.get("source_id") or "")
    case_id = str(case.get("case_id") or "")
    if (
        not source_id
        or not case_id
        or record.kind != "evaluation_packet"
        or record.status != "active"
        or record.source != ACCEPTED_SOURCE
        or record.source_id != source_id
        or not same_scope(record.scope, exact_scope)
        or str(case.get("channel") or "") != channel
        or not isinstance(case.get("scope"), dict)
        or not same_scope(ScopeRef.from_dict(case["scope"]), exact_scope)
        or record.meta.get("report_type") != "production_recall_accepted_case"
        or record.meta.get("schema") != ACCEPTED_QUERY_SCHEMA
        or str(record.meta.get("channel") or "") != channel
        or str(record.meta.get("case_id") or "") != case_id
    ):
        return "accepted_boundary_mismatch"
    features, feature_reason = _bounded_query_features(case.get("query_features"))
    if feature_reason or features != case.get("query_features") or str(case.get("query_digest") or "") != _stable_digest(features):
        return "accepted_query_identity_invalid"
    provenance = case.get("provenance") if isinstance(case.get("provenance"), dict) else {}
    if set(provenance) != {"collector", "capture_ref"} or provenance.get("collector") != "proactive_audit_capture":
        return "accepted_capture_provenance_invalid"
    window = case.get("collection_window") if isinstance(case.get("collection_window"), dict) else {}
    if set(window) != {"started_at", "ended_at"}:
        return "accepted_collection_window_invalid"
    try:
        started = datetime.fromisoformat(str(window["started_at"]).replace("Z", "+00:00"))
        ended = datetime.fromisoformat(str(window["ended_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "accepted_collection_window_invalid"
    if started.tzinfo is None or ended.tzinfo is None or started >= ended:
        return "accepted_collection_window_invalid"
    record_evidence = [str(item) for item in record.evidence]
    if len(record_evidence) < 2 or len(record_evidence) > 17:
        return "accepted_evidence_refs_invalid"
    pending_id = record_evidence[0]
    pending = runtime.store.get_by_id(pending_id, scope=exact_scope)
    if pending is None or pending.source != PENDING_SOURCE or pending.status != "active" or pending.source_id != source_id:
        return "accepted_pending_missing"
    pending_payload = pending.content if isinstance(pending.content, dict) else {}
    if (
        pending_payload.get("schema") != PENDING_QUERY_SCHEMA
        or str(pending_payload.get("case_id") or "") != case_id
        or str(pending_payload.get("channel") or "") != channel
        or str(pending_payload.get("source_id") or "") != source_id
        or not isinstance(pending_payload.get("scope"), dict)
        or not _alias_equivalent_scope(pending_payload["scope"], exact_scope)
        or str(pending_payload.get("capture_ref") or "") != str(provenance.get("capture_ref") or "")
    ):
        return "accepted_pending_identity_mismatch"
    expected_pending_id = "prqp_" + _stable_digest(
        {
            "schema": PENDING_QUERY_SCHEMA,
            "decision_id": str(pending_payload.get("capture_ref") or ""),
            "query_digest": str(pending_payload.get("capture_query_digest") or ""),
        }
    )[:32]
    if pending.record_id != expected_pending_id:
        return "accepted_pending_record_identity_invalid"
    pending_error = pending_production_query_capture_validation_error(
        runtime,
        pending,
        exact_scope=exact_scope,
        channel=channel,
    )
    if pending_error:
        return pending_error
    labels = case.get("labels")
    if not isinstance(labels, list) or not 1 <= len(labels) <= 16:
        return "accepted_labels_invalid"
    seen_refs: set[str] = set()
    for item in labels:
        if not isinstance(item, dict) or set(item) != {"record_ref", "grade", "accepted", "provenance"}:
            return "accepted_label_invalid"
        record_ref = str(item.get("record_ref") or "")
        grade = item.get("grade")
        label_provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
        labeler = str(label_provenance.get("labeler") or "")
        evidence_ref = str(label_provenance.get("evidence_ref") or "")
        if (
            not record_ref
            or record_ref in seen_refs
            or isinstance(grade, bool)
            or not isinstance(grade, int)
            or not 1 <= grade <= 3
            or item.get("accepted") is not True
            or set(label_provenance) != {"labeler", "labelled_at", "evidence_ref"}
            or labeler not in production_real_query_trusted_labelers()
            or not evidence_ref
        ):
            return "accepted_label_invalid"
        candidate = runtime.store.get_by_id(record_ref, scope=exact_scope)
        evidence = runtime.store.get_by_id(evidence_ref, scope=exact_scope)
        if (candidate is None or candidate.status != "active" or candidate.source_id != source_id
                or not projection_is_current(runtime.store, candidate)):
            return "accepted_candidate_boundary_invalid"
        if evidence is None:
            return "accepted_label_evidence_missing"
        from .label_authority import label_authority_error
        if label_authority_error(evidence, scope=exact_scope, source_id=source_id,
                pending_id=pending_id, record_ref=record_ref, grade=grade, labeler=labeler):
            return "accepted_label_evidence_invalid"
        evidence_payload = evidence.content if isinstance(evidence.content, dict) else {}
        if labeler == PRODUCTION_REAL_QUERY_AUTO_REVIEW_LABELER:
            from .production_query_auto_review import auto_review_revocation_reason
            expected_evidence_id = "prle_" + _stable_digest(
                {"pending_record_id": pending_id, "record_ref": record_ref, "grade": grade, "labeler": labeler}
            )[:32]
            if (
                evidence.record_id != expected_evidence_id
                or evidence.kind != "evaluation_packet"
                or evidence.status != "active"
                or evidence.source != LABEL_EVIDENCE_SOURCE
                or evidence.source_id != source_id
                or not same_scope(evidence.scope, exact_scope)
                or evidence_payload.get("evidence_class") != "auto_review_relevance_label"
                or record_ref not in [str(ref) for ref in pending_payload.get("candidate_refs") or []]
                or [str(ref) for ref in evidence.evidence] != [pending_id, record_ref]
            ):
                return "accepted_label_evidence_invalid"
            if auto_review_revocation_reason(runtime, pending_id=pending_id, scope=exact_scope):
                return "auto_review_label_revoked"
            seen_refs.add(record_ref)
            continue
        packet = evidence_payload.get("operator_packet_evidence") if isinstance(evidence_payload.get("operator_packet_evidence"), dict) else {}
        packet_digest = str(packet.get("digest") or "").lower()
        expected_evidence_id = "prle_" + _stable_digest(
            {"pending_record_id": pending_id, "record_ref": record_ref, "grade": grade, "labeler": labeler}
        )[:32]
        if (
            evidence.record_id != expected_evidence_id
            or evidence.kind != "evaluation_packet"
            or evidence.status != "active"
            or evidence.source != LABEL_EVIDENCE_SOURCE
            or evidence.source_id != source_id
            or not same_scope(evidence.scope, exact_scope)
            or evidence_payload.get("evidence_class") != "operator_relevance_label"
            or str(evidence_payload.get("labeler") or "") != labeler
            or str(evidence_payload.get("pending_record_id") or "") != pending_id
            or str(evidence_payload.get("record_ref") or "") != record_ref
            or evidence_payload.get("grade") != grade
            or packet.get("schema") != "secure_dataset_fingerprint.v1"
            or re.fullmatch(r"[0-9a-f]{64}", packet_digest) is None
            or isinstance(packet.get("size"), bool)
            or not isinstance(packet.get("size"), int)
            or int(packet.get("size") or 0) <= 0
            or isinstance(packet.get("device"), bool)
            or not isinstance(packet.get("device"), int)
            or isinstance(packet.get("inode"), bool)
            or not isinstance(packet.get("inode"), int)
            or evidence.meta.get("report_type") != "production_recall_label_evidence"
            or evidence.meta.get("authoritative") is not True
            or str(evidence.meta.get("operator_packet_digest") or "").lower() != packet_digest
            or [str(ref) for ref in evidence.evidence] != [pending_id, record_ref]
        ):
            return "accepted_label_evidence_invalid"
        seen_refs.add(record_ref)
    expected_refs = [pending_id, *[str(item["record_ref"]) for item in labels]]
    if record_evidence != expected_refs:
        return "accepted_evidence_refs_mismatch"
    expected_accepted_id = "prqa_" + _stable_digest({"schema": ACCEPTED_QUERY_SCHEMA, "case": case})[:32]
    if record.record_id != expected_accepted_id:
        return "accepted_record_identity_invalid"
    return ""


def build_production_query_dataset(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    limit: int = 500,
    max_cases_per_channel: int | None = None,
) -> dict[str, Any]:
    requested = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    resolved_scope, _scope_resolution = resolve_bound_logical_scope(requested)
    base = ScopeRef.from_dict(resolved_scope)
    channel_case_limit = (
        None
        if max_cases_per_channel is None
        else max(1, min(500, int(max_cases_per_channel)))
    )
    cases: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    seen: set[str] = set()
    skipped_low_signal = 0
    auto_review_enabled = production_recall_auto_review_enabled()
    by_authority = {"operator": 0, "auto_review": 0, "unknown": 0}
    labels_by_authority = {"operator": 0, "auto_review": 0, "unknown": 0}
    auto_review_excluded_by_policy = 0

    def _case_authority(record: RecordEnvelope) -> str:
        content = record.content if isinstance(record.content, dict) else {}
        case_value = content.get("case") if isinstance(content.get("case"), dict) else {}
        authorities = {
            production_real_query_label_authority((label.get("provenance") or {}).get("labeler"))
            for label in case_value.get("labels") or [] if isinstance(label, dict)
        }
        return "unknown" if not authorities or "unknown" in authorities else ("auto_review" if "auto_review" in authorities else "operator")

    for channel in sorted(SUPPORTED_RUNTIME_CHANNELS):
        exact = ScopeRef.from_dict(resolve_channel_scope(channel, asdict(base)))
        records = runtime.store.list_records_by_meta_value(
            kinds=["evaluation_packet"],
            scope=exact,
            meta_key="report_type",
            meta_value="production_recall_accepted_case",
            status="active",
            limit=max(1, min(500, int(limit))),
        ) or []
        # Human-accepted cases take precedence over an auto-reviewed case for the
        # same capture; auto-reviewed cases are excluded when the policy is off.
        records = sorted(records, key=lambda item: _case_authority(item) == "auto_review")
        for record in records:
            if record.source != ACCEPTED_SOURCE or not same_scope(record.scope, exact):
                continue
            authority = _case_authority(record)
            if authority == "auto_review" and not auto_review_enabled:
                auto_review_excluded_by_policy += 1
                continue
            case = record.content.get("case") if isinstance(record.content, dict) and isinstance(record.content.get("case"), dict) else {}
            case_id = str(case.get("case_id") or "")
            if case_id in seen or str(case.get("channel") or "") != channel:
                continue
            quality_reasons = production_real_query_feature_quality_reasons(case.get("query_features"))
            if quality_reasons:
                if "query_features_low_signal" in quality_reasons:
                    skipped_low_signal += 1
                continue
            if accepted_production_query_validation_error(
                runtime,
                record,
                exact_scope=exact,
                channel=channel,
            ):
                continue
            seen.add(case_id)
            cases.append(dict(case))
            by_authority[authority] += 1
            labels_by_authority[authority] += len(case.get("labels") or [])
            counts[channel] = counts.get(channel, 0) + 1
            if channel_case_limit is not None and counts[channel] >= channel_case_limit:
                break
    counts = {channel: counts.get(channel, 0) for channel in sorted(SUPPORTED_RUNTIME_CHANNELS)}
    active_contract = production_real_query_active_channel_contract(counts)
    ready = bool(active_contract["ok"])
    dataset = {
        "schema": PRODUCTION_REAL_QUERY_SCHEMA,
        "name": "production-redacted-real-query",
        "dataset_kind": "production",
        "scope": asdict(base),
        "cases": sorted(cases, key=lambda item: str(item.get("case_id") or "")),
        "baseline_report_id": "",
    }
    return {
        "ok": True,
        "ready": ready,
        "progress": {
            "accepted_case_count": len(cases),
            "required_case_count": int(active_contract["required_case_count"]),
            "required_channels": list(active_contract["required_channels"]),
            "required_per_channel": int(active_contract["required_per_channel"]),
            "active_channels": list(active_contract["active_channels"]),
            "blocked_reasons": list(active_contract["blocked_reasons"]),
            "skipped_low_signal": skipped_low_signal,
            "per_channel_accepted": counts,
            "accepted_by_authority": dict(by_authority),
            "accepted_labels_by_authority": dict(labels_by_authority),
            "auto_review_policy": {
                "flag": PRODUCTION_RECALL_AUTO_REVIEW_FLAG,
                "enabled": auto_review_enabled,
                "excluded_by_policy": auto_review_excluded_by_policy,
            },
        },
        "dataset": dataset,
    }


def write_production_query_dataset(dataset: dict[str, Any], path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser().absolute()
    if target.is_symlink():
        raise ValueError("production recall dataset target must not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    digest = sha256(raw).hexdigest()
    if target.exists():
        existing = target.read_bytes()
        if existing != raw:
            raise FileExistsError("immutable production recall dataset already exists with different content")
        if target.stat().st_nlink != 1:
            raise RuntimeError("production recall dataset publication is incomplete; retry after temporary-link cleanup")
        return {"ok": True, "path": str(target), "digest": digest, "size": len(raw), "unchanged": True}
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    unchanged = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != raw:
                raise FileExistsError("immutable production recall dataset already exists with different content")
            if target.stat().st_nlink != 1:
                raise RuntimeError("production recall dataset publication is incomplete; retry after temporary-link cleanup")
            unchanged = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise OSError(f"production recall dataset temporary cleanup failed: {temporary}") from exc
    return {"ok": True, "path": str(target), "digest": digest, "size": len(raw), "unchanged": unchanged}


def publish_production_query_dataset(dataset: dict[str, Any], evaluation_dir: str | Path) -> dict[str, Any]:
    """Publish one immutable snapshot and atomically advance its current pointer."""

    staged = stage_production_query_dataset(dataset, evaluation_dir)
    activated = activate_production_query_dataset(staged)
    return {**staged, **activated}


def stage_production_query_dataset(dataset: dict[str, Any], evaluation_dir: str | Path, *, runtime=None) -> dict[str, Any]:
    """Write an immutable content-addressed snapshot without changing current."""

    if runtime is not None:
        from .dataset_authority import dataset_authority_manifest
        dataset = {**dataset, 'authority_manifest':dataset_authority_manifest(runtime, dataset)}
    directory = Path(evaluation_dir).expanduser().absolute()
    _ensure_private_dataset_directory(directory)
    raw = (json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    digest = sha256(raw).hexdigest()
    relative_path = Path("production_recall.datasets") / f"production_recall.{digest}.json"
    snapshot = directory / relative_path
    _ensure_private_dataset_directory(snapshot.parent)
    written = write_production_query_dataset(dataset, snapshot)
    return {
        **written,
        "evaluation_dir": str(directory),
        "relative_path": relative_path.as_posix(),
    }


def activate_production_query_dataset(staged: dict[str, Any], *, runtime=None) -> dict[str, Any]:
    """Validate a staged snapshot and atomically make it the current dataset."""

    directory = Path(str(staged.get("evaluation_dir") or "")).expanduser().absolute()
    snapshot = Path(str(staged.get("path") or "")).expanduser().absolute()
    digest = str(staged.get("digest") or "").lower()
    relative_path = str(staged.get("relative_path") or "")
    size = staged.get("size")
    expected_relative = f"production_recall.datasets/production_recall.{digest}.json"
    geteuid = getattr(os, "geteuid", None)
    snapshot_metadata = snapshot.lstat() if snapshot.exists() and not snapshot.is_symlink() else None
    trusted_owner = (
        snapshot_metadata is not None
        and (not callable(geteuid) or int(snapshot_metadata.st_uid) == int(geteuid()))
    )
    if (
        relative_path != expected_relative
        or snapshot != directory / Path(relative_path)
        or directory.is_symlink()
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
        or snapshot.is_symlink()
        or snapshot_metadata is None
        or not stat.S_ISREG(snapshot_metadata.st_mode)
        or not trusted_owner
        or stat.S_IMODE(snapshot_metadata.st_mode) != 0o600
        or int(snapshot_metadata.st_nlink) != 1
        or int(snapshot_metadata.st_size) != size
    ):
        raise ValueError("staged production recall dataset is invalid")
    raw = snapshot.read_bytes()
    if len(raw) != size or sha256(raw).hexdigest() != digest:
        raise ValueError("staged production recall dataset digest mismatch")
    dataset = json.loads(raw)
    if runtime is not None:
        from .dataset_authority import dataset_authority_manifest
        if dataset.get('authority_manifest') != dataset_authority_manifest(runtime, dataset):
            raise ValueError('dataset_authority_stale')
    elif dataset.get('authority_manifest') is not None:
        raise ValueError('dataset_authority_runtime_required')
    pointer = directory / "production_recall.current.json"
    pointer_payload = {
        "schema": PRODUCTION_QUERY_DATASET_POINTER_SCHEMA,
        "digest": digest,
        "relative_path": relative_path,
        "size": size,
    }
    pointer_unchanged = _write_dataset_pointer(pointer, pointer_payload)
    return {
        "pointer_path": str(pointer),
        "pointer_unchanged": pointer_unchanged,
    }


def _ensure_private_dataset_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise ValueError("production recall dataset directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = directory.lstat()
    geteuid = getattr(os, "geteuid", None)
    trusted_owner = not callable(geteuid) or int(metadata.st_uid) == int(geteuid())
    if not stat.S_ISDIR(metadata.st_mode) or not trusted_owner:
        raise ValueError("production recall dataset directory is untrusted")
    os.chmod(directory, 0o700)
    if stat.S_IMODE(directory.lstat().st_mode) != 0o700:
        raise ValueError("production recall dataset directory is not private")


def _write_dataset_pointer(pointer: Path, payload: dict[str, Any]) -> bool:
    if pointer.is_symlink():
        raise ValueError("production recall dataset pointer must not be a symlink")
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if pointer.exists():
        metadata = pointer.lstat()
        geteuid = getattr(os, "geteuid", None)
        trusted_owner = not callable(geteuid) or int(metadata.st_uid) == int(geteuid())
        if (
            stat.S_ISREG(metadata.st_mode)
            and trusted_owner
            and stat.S_IMODE(metadata.st_mode) == 0o600
            and int(metadata.st_nlink) == 1
            and pointer.read_bytes() == raw
        ):
            return True
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{pointer.name}.",
        suffix=".tmp",
        dir=pointer.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, pointer)
        directory_flag = int(getattr(os, "O_DIRECTORY", 0))
        if directory_flag:
            directory_descriptor = os.open(pointer.parent, os.O_RDONLY | directory_flag)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
    return False
