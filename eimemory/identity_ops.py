from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime, timezone
import json
import re
from types import SimpleNamespace
from typing import Any

from eimemory.core import strict_json
from eimemory.identity import (
    build_identity_report,
    hongtu_query_scopes,
    is_hongtu_scope,
    needs_hongtu_identity_repair,
    normalize_hongtu_record,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.models.identity_aliases import (
    IDENTITY_ALIASES_VERSION,
    _LEGACY_CONTENT_ALIAS_KINDS,
    normalize_record_aliases,
)
from eimemory.models.source_partitions import DEFAULT_SOURCE_ID
from eimemory.adapters.runtime.channel import SUPPORTED_RUNTIME_CHANNELS, resolve_channel_scope
from eimemory.storage.jsonl import payload_digest


# Exact refs bind tenant/agent/workspace/user/source/id, never a bare record ID.
_REF_WHERE = (
    "tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ? "
    "AND source_id = ? AND record_id = ?"
)


class _RepairSkipped(Exception):
    def __init__(self, reason: str, *, mismatch_fields: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.mismatch_fields = mismatch_fields


def identity_report(runtime, *, limit: int | None = None, scope=None) -> dict[str, Any]:
    return build_identity_report(_iter_records(runtime, limit=limit, scope=scope))


def repair_hongtu_identity(
    runtime,
    *,
    apply: bool = False,
    limit: int | None = None,
    scope=None,
    skip_created_at_or_after: str | None = None,
) -> dict[str, Any]:
    """Repair identity using exact refs and content-version CAS.

    Legacy agent/workspace aliases use the existing transactional rewrite
    contract; tenant, user and source remain unchanged. A payload digest is a
    content version, not a monotonic generation:
    byte-identical delete/recreate ABA is outside this guarantee.
    """
    if skip_created_at_or_after:
        _utc_instant(skip_created_at_or_after)
    if scope is None:
        if apply:
            raise ValueError("identity_repair_scope_required")
        report = build_identity_report(
            _iter_records(runtime, limit=limit, skip_created_at_or_after=skip_created_at_or_after)
        )
        return {**report, "ok": True, "apply": False, "scoped": False,
                "candidate_count": report["repair_candidate_count"], "repaired_count": 0,
                "repaired_record_ids": [], "diagnostic_only": True}

    if not isinstance(scope, ScopeRef):
        keys = ("tenant_id", "agent_id", "workspace_id", "user_id")
        if not isinstance(scope, dict) or any(key not in scope or not isinstance(scope[key], str) for key in keys):
            raise ValueError("identity_repair_complete_scope_required")
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    # Legacy agent/workspace aliases are admitted explicitly, within the same
    # tenant/user, including the existing store-authorized shared blank user.
    admitted = {(_scope_tuple(item)) for item in hongtu_query_scopes(scope_ref)}
    if is_hongtu_scope(scope_ref):
        admitted.update(_scope_tuple(ScopeRef.from_dict(resolve_channel_scope(channel, asdict(scope_ref))))
                        for channel in SUPPORTED_RUNTIME_CHANNELS)
    if is_hongtu_scope(scope_ref):
        # A renamed agent's legacy rows also live in its channel subscopes.
        from eimemory.identity import legacy_agent_ids as _legacy_ids

        for legacy in _legacy_ids():
            legacy_base = asdict(scope_ref) | {"agent_id": legacy}
            admitted.update(_scope_tuple(ScopeRef.from_dict(resolve_channel_scope(channel, legacy_base)))
                            for channel in SUPPORTED_RUNTIME_CHANNELS)
    if scope_ref.user_id:
        admitted.update((tenant, agent, workspace, "") for tenant, agent, workspace, _ in list(admitted))

    def discover(sqlite):
        plans: list[dict[str, Any]] = []
        outcomes: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        discovered: set[tuple[str, ...]] = set()

        def capture():
            candidates = (candidate for exact in sorted(admitted)
                          for candidate in _iter_records(SimpleNamespace(store=sqlite), limit=limit, scope=ScopeRef(*exact)))
            for candidate in candidates:
                if _scope_tuple(candidate.scope) not in admitted:
                    continue
                identity = _exact_ref(candidate)
                if identity in discovered:
                    continue
                if limit is not None and limit > 0 and len(discovered) >= limit:
                    break
                discovered.add(identity)
                yield candidate
                if not needs_hongtu_identity_repair(candidate):
                    continue
                ref = _exact_ref(candidate)
                if ref in seen:
                    continue
                seen.add(ref)
                snapshot_diagnostics: dict[str, tuple[str, ...]] = {}
                try:
                    current, version = _repair_snapshot(sqlite, ref, diagnostics=snapshot_diagnostics)
                    _require_eligible(current, skip_created_at_or_after)
                    proposed = normalize_hongtu_record(current)
                    target = _exact_ref(proposed)
                    _require_identity_transition(current, proposed)
                    storage = sqlite.identity_repair_preflight(proposed)
                    if target != ref:
                        # Query the physical key WITHOUT source_id: its current
                        # uniqueness constraint does not include the source.
                        occupied = sqlite.execute(
                            "SELECT source_id FROM records WHERE storage_key = ? OR (tenant_id = ? AND agent_id = ? "
                            "AND workspace_id = ? AND user_id = ? AND record_id = ?)",
                            (storage["storage_key"], *target[:4], target[5]),
                        ).fetchone()
                        if occupied is not None:
                            raise _RepairSkipped("target_conflict")
                    if not storage.get("ok"):
                        raise _RepairSkipped(str(storage["reason"]))
                    if target == ref and storage["storage_key"] != version[2]:
                        raise _RepairSkipped("source_storage_key_mismatch")
                    plans.append({"source_ref": ref, "target_ref": target, "version": version,
                                  "snapshot_diagnostics": snapshot_diagnostics})
                except _RepairSkipped as exc:
                    outcomes.append(_outcome(ref, exc.reason, mismatch_fields=exc.mismatch_fields,
                                             snapshot_diagnostics=snapshot_diagnostics))

        report = build_identity_report(capture())
        return report, plans, outcomes

    report, plans, outcomes = runtime.store.read_consistent(discover)
    repaired_ids: list[str] = []
    if apply:
        for plan in plans:
            ref = plan["source_ref"]
            snapshot_diagnostics = plan["snapshot_diagnostics"]

            def mutate(sqlite):
                current, version = _repair_snapshot(sqlite, ref, diagnostics=snapshot_diagnostics)
                if version != plan["version"]:
                    raise _RepairSkipped("source_version_changed")
                if _scope_tuple(current.scope) not in admitted:
                    raise _RepairSkipped("source_scope_not_admitted")
                _require_eligible(current, skip_created_at_or_after)
                proposed = normalize_hongtu_record(current)
                _require_identity_transition(current, proposed)
                if _exact_ref(proposed) != plan["target_ref"]:
                    raise _RepairSkipped("scope_move_unsupported")
                storage = sqlite.identity_repair_preflight(proposed, for_write=True)
                if not storage.get("ok"):
                    raise _RepairSkipped(str(storage["reason"]))
                if plan["target_ref"] == ref and storage["storage_key"] != version[2]:
                    raise _RepairSkipped("source_storage_key_mismatch")
                # The transaction owner has BEGIN IMMEDIATE. Guard the exact
                # snapshot version; do not fall back to updated_at-only CAS.
                changed = sqlite.execute(
                    "UPDATE records SET payload_digest = payload_digest WHERE storage_key = ? AND " + _REF_WHERE
                    + " AND payload_digest = ?",
                    (version[2], *ref, version[0]),
                ).rowcount
                if changed != 1:
                    raise _RepairSkipped("source_version_changed")
                if plan["target_ref"] != ref:
                    occupied = sqlite.execute("SELECT 1 FROM records WHERE storage_key = ?",
                                              (storage["storage_key"],)).fetchone()
                    if occupied is not None:
                        raise _RepairSkipped("target_conflict")
                sqlite.rewrite(proposed, previous_scope=current.scope, commit=False)
                return _outcome(ref, "applied", snapshot_diagnostics=snapshot_diagnostics), [proposed], []

            try:
                outcome = runtime.store.mutate_records_atomically(mutate)
            except _RepairSkipped as exc:
                # Raising within the owner rolls back and avoids post-commit
                # projection/outbox flushing for a rejected/no-op candidate.
                outcome = _outcome(ref, exc.reason, mismatch_fields=exc.mismatch_fields,
                                   snapshot_diagnostics=snapshot_diagnostics)
            except Exception as exc:
                outcome = _outcome(ref, "transaction_failed:" + type(exc).__name__,
                                   snapshot_diagnostics=snapshot_diagnostics)
            outcomes.append(outcome)
            if outcome["reason"] == "applied":
                repaired_ids.append(ref[5])

    benign = {"fresh", "ingest_stamped", "no_longer_eligible"}
    blocked = [item for item in outcomes if item["reason"] not in benign | {"applied"}]
    counts: dict[str, int] = {}
    mismatch_counts: dict[str, int] = {}
    for item in outcomes:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
        for field in item.get("mismatch_fields", ()):
            mismatch_counts[field] = mismatch_counts.get(field, 0) + 1
    diagnostic_items = outcomes + ([] if apply else [plan["snapshot_diagnostics"] for plan in plans])
    diagnostic_counts: dict[str, dict[str, int]] = {}
    for key in ("roundtrip_fields", "roundtrip_missing_fields", "legacy_normalization"):
        diagnostic_counts[key] = {}
        for item in diagnostic_items:
            for field in item.get(key, ()):
                diagnostic_counts[key][field] = diagnostic_counts[key].get(field, 0) + 1
    status = "partial" if blocked and repaired_ids else "blocked" if blocked else "complete" if apply else "preview"
    return {
        **report,
        "report_phase": "discovery_snapshot",
        "ok": not blocked,
        "status": status,
        "apply": bool(apply),
        "scoped": True,
        "candidate_count": report["repair_candidate_count"],
        "eligible_in_place_count": len(plans),
        "repaired_count": len(repaired_ids),
        "repaired_record_ids": repaired_ids[:100],
        "skipped_fresh_count": counts.get("fresh", 0) + counts.get("ingest_stamped", 0),
        "blocked_count": len(blocked),
        "outcome_counts": counts,
        "mismatch_field_counts": mismatch_counts,
        "roundtrip_field_counts": diagnostic_counts["roundtrip_fields"],
        "roundtrip_missing_field_counts": diagnostic_counts["roundtrip_missing_fields"],
        "legacy_normalization_counts": diagnostic_counts["legacy_normalization"],
        "repair_outcomes": outcomes[:100],
        "repair_outcomes_truncated": len(outcomes) > 100,
    }


def _scope_tuple(scope: ScopeRef) -> tuple[str, str, str, str]:
    return scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id


def _exact_ref(record: RecordEnvelope) -> tuple[str, ...]:
    return (*_scope_tuple(record.scope), record.source_id, record.record_id)


def _repair_snapshot(
    sqlite, ref: tuple[str, ...], *, diagnostics: dict[str, tuple[str, ...]] | None = None,
) -> tuple[RecordEnvelope, tuple[str, ...]]:
    if diagnostics is not None:
        diagnostics.clear()
    rows = sqlite.execute(
        "SELECT * FROM records WHERE tenant_id = ? AND agent_id = ? "
        "AND workspace_id = ? AND user_id = ? AND record_id = ?",
        (*ref[:4], ref[5]),
    ).fetchall()
    if len(rows) != 1:
        raise _RepairSkipped("source_missing" if not rows else "source_ambiguous")
    row = rows[0]
    if row["source_id"] != ref[4]:
        raise _RepairSkipped("source_identity_changed")
    digest = str(row["payload_digest"] or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise _RepairSkipped("source_version_unknown")
    # Archived compact projections need their own metadata-rewrite contract.
    # This bounded phase does not rewrite payload segments or archive pointers.
    if str(row["payload_pointer_json"] or ""):
        raise _RepairSkipped("archived_payload_unsupported")
    record = sqlite.get_by_exact_ref(
        ref[5], scope=ScopeRef(*ref[:4]), source_id=ref[4],
    )
    try:
        raw_text = str(row["payload_json"])
        raw_payload = strict_json.loads(raw_text, max_bytes=max(1, len(raw_text.encode("utf-8"))))
        if not isinstance(raw_payload, dict):
            raise ValueError("payload_not_object")
    except (TypeError, ValueError, UnicodeError):
        raise _RepairSkipped("source_projection_or_digest_mismatch", mismatch_fields=("payload_json",)) from None
    # The content-version token authenticates the persisted envelope, before
    # any reader defaults or legacy source-partition overlays are applied.
    if payload_digest(raw_payload) != digest:
        fields = ("hydration", "payload_stored_digest") if record is None else ("payload_stored_digest",)
        raise _RepairSkipped("source_projection_or_digest_mismatch", mismatch_fields=fields)
    if record is not None and diagnostics is not None:
        diagnostics.update(_roundtrip_diagnostics(raw_payload, record))
    if (record is None or _exact_ref(record) != ref
            or record.kind != row["kind"] or record.status != row["status"]
            or record.source != row["source"]
            or record.time.created_at != row["created_at"]
            or record.time.updated_at != row["updated_at"]
            or any(getattr(record, key) != row[key] for key in ("title", "summary", "detail"))):
        raise _RepairSkipped(
            "source_projection_or_digest_mismatch",
            mismatch_fields=_projection_mismatch_fields(record, row, ref, digest),
        )
    try:
        meta_text = str(row["meta_json"])
        projected_meta = strict_json.loads(meta_text, max_bytes=max(1, len(meta_text.encode("utf-8"))))
        if not isinstance(projected_meta, dict) or payload_digest(projected_meta) != payload_digest(record.meta):
            raise _RepairSkipped("source_projection_or_digest_mismatch", mismatch_fields=("meta_json",))
    except (TypeError, ValueError) as exc:
        raise _RepairSkipped("source_projection_or_digest_mismatch", mismatch_fields=("meta_json",)) from exc
    if payload_digest(record.to_dict()) != digest:
        normalization = _legacy_roundtrip_normalization(sqlite, raw_payload, record, row)
        if normalization is None:
            raise _RepairSkipped("source_projection_or_digest_mismatch", mismatch_fields=("payload_roundtrip_digest",))
        if diagnostics is not None:
            diagnostics["legacy_normalization"] = normalization
    physical_key = str(row["storage_key"] or "")
    if sqlite.identity_repair_preflight(record)["storage_key"] != physical_key:
        raise _RepairSkipped("source_storage_key_mismatch")
    # The second token binds every captured SQL projection column, including
    # metadata and all exact refs. Compare it inside BEGIN IMMEDIATE; the SQL
    # payload-CAS then cannot race another writer before the in-place upsert.
    return record, (digest, payload_digest(dict(row)), physical_key)


def _legacy_roundtrip_normalization(sqlite, raw, record, row) -> tuple[str, ...] | None:
    """Prove only known, lossless legacy reader projections changed the raw envelope."""
    expected = dict(raw)
    normalization: list[str] = []
    content = raw.get("content")
    if ("aliases" not in raw and record.kind in _LEGACY_CONTENT_ALIAS_KINDS
            and isinstance(content, dict) and "aliases" in content):
        legacy_aliases = content["aliases"]
        if not (isinstance(legacy_aliases, str)
                or isinstance(legacy_aliases, list) and all(isinstance(item, str) for item in legacy_aliases)):
            # General readers may coerce arbitrary legacy values. Identity
            # compatibility cannot turn malformed alias evidence into text.
            return None
    defaults = {
        "source_id": DEFAULT_SOURCE_ID,
        "aliases_version": IDENTITY_ALIASES_VERSION,
        "aliases": normalize_record_aliases(None, kind=record.kind, content=raw.get("content")),
    }
    for field, value in defaults.items():
        if field not in raw:
            expected[field] = value
            normalization.append("missing_" + field)
    if (raw.get("kind") == "knowledge_page" and expected.get("source_id") == DEFAULT_SOURCE_ID
            and record.source_id != DEFAULT_SOURCE_ID):
        # Re-derive the existing storage migration's source partition from the
        # verified original envelope. The SQL source column alone is no proof.
        original = sqlite._record_from_payload_dict(raw)
        if (original is None or sqlite._legacy_source_partition_from_record(original) != record.source_id
                or record.source_id != row["source_id"]):
            return None
        expected["source_id"] = record.source_id
        normalization.append("source_partition_overlay")
    # Preserve every other key and JSON type, including unknown nested keys.
    # A permissive model round trip is not evidence that dropping/coercing
    # those fields was safe. Hashing also distinguishes bool/int/float values.
    if not normalization or payload_digest(expected) != payload_digest(record.to_dict()):
        return None
    return tuple(normalization)


def _roundtrip_diagnostics(raw: dict[str, Any], record: RecordEnvelope) -> dict[str, tuple[str, ...]]:
    """Return schema field names only; never echo unknown keys or their values."""
    try:
        hydrated = record.to_dict()
        known = set(RecordEnvelope.__dataclass_fields__)
        changed = [field for field in sorted(known)
                   if field not in raw or payload_digest({"value": raw[field]}) != payload_digest({"value": hydrated[field]})]
        if set(raw) - known:
            changed.append("unknown_fields")
        return {"roundtrip_fields": tuple(changed),
                "roundtrip_missing_fields": tuple(field for field in sorted(known) if field not in raw)}
    except Exception:
        return {"roundtrip_fields": ("diagnostic_unavailable",)}


def _projection_mismatch_fields(
    record: RecordEnvelope | None, row: Any, ref: tuple[str, ...], digest: str,
) -> tuple[str, ...]:
    """Explain an already rejected snapshot using fixed field names only.

    Keep this diagnostic behind the existing fail-closed predicate: collecting
    another comparison must never turn a rejection into a new exception or
    admit a record. A model round trip can differ from a valid legacy payload;
    that is distinct from a failed checksum on the stored payload itself.
    """
    fields: list[str] = []
    try:
        if record is None:
            fields.append("hydration")
        else:
            if _exact_ref(record) != ref:
                fields.append("exact_ref")
            for field in ("kind", "status", "source", "title", "summary", "detail"):
                if getattr(record, field) != row[field]:
                    fields.append(field)
            for field in ("created_at", "updated_at"):
                if getattr(record.time, field) != row[field]:
                    fields.append(field)
            if payload_digest(record.to_dict()) != digest:
                fields.append("payload_roundtrip_digest")
            try:
                meta_text = str(row["meta_json"])
                projected_meta = strict_json.loads(meta_text, max_bytes=max(1, len(meta_text.encode("utf-8"))))
                if not isinstance(projected_meta, dict) or payload_digest(projected_meta) != payload_digest(record.meta):
                    fields.append("meta_json")
            except (TypeError, ValueError):
                fields.append("meta_json")
        try:
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                fields.append("payload_json")
            elif payload_digest(payload) != digest:
                fields.append("payload_stored_digest")
        except (TypeError, ValueError):
            fields.append("payload_json")
    except Exception:
        # The original predicate has already rejected this snapshot. Optional
        # diagnostics must preserve that outcome even for an unusual envelope.
        fields.append("diagnostic_unavailable")
    return tuple(fields)


def _require_identity_transition(before: RecordEnvelope, after: RecordEnvelope) -> None:
    old = before.to_dict()
    new = after.to_dict()
    # Identity repair may alter agent/workspace, identity metadata and touch
    # updated_at, but cannot become a tenant/user/source/content mutation.
    for payload in (old, new):
        payload.pop("meta", None)
        payload["time"] = {key: value for key, value in payload["time"].items() if key != "updated_at"}
        payload["scope"] = {key: value for key, value in payload["scope"].items() if key not in {"agent_id", "workspace_id"}}
    if payload_digest(old) != payload_digest(new):
        raise _RepairSkipped("identity_transition_forbidden")


def _renamed_legacy_agent(record: RecordEnvelope) -> bool:
    """An ingest stamp under a configured legacy agent id is the old name, not a pin.

    After an agent rename the ingest stamp records the previous canonical id;
    those rows must follow the rename like unstamped ones. Explicit scope pins
    (provenance ``identity_scope_preserved``) and deployment receipts stay
    excluded by ``needs_hongtu_identity_repair``; the transition guard still
    permits only agent/workspace/identity-metadata changes.
    """
    from eimemory.identity import legacy_agent_ids

    return str(record.scope.agent_id or "") in legacy_agent_ids()


def _require_eligible(record: RecordEnvelope, bound: str | None) -> None:
    if not needs_hongtu_identity_repair(record):
        raise _RepairSkipped("no_longer_eligible")
    if bool((record.meta or {}).get("identity_stamped_on_ingest")) and not _renamed_legacy_agent(record):
        raise _RepairSkipped("ingest_stamped")
    if bound:
        try:
            if _record_at_or_after(record, bound):
                raise _RepairSkipped("fresh")
        except ValueError as exc:
            raise _RepairSkipped("source_timestamp_unknown") from exc


def _outcome(
    ref: tuple[str, ...], reason: str, *, mismatch_fields: tuple[str, ...] = (),
    snapshot_diagnostics: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "source_ref": {"tenant_id": ref[0], "agent_id": ref[1], "workspace_id": ref[2],
                       "user_id": ref[3], "source_id": ref[4], "record_id": ref[5]},
        "reason": reason,
    }
    if mismatch_fields:
        outcome["mismatch_fields"] = list(mismatch_fields)
    for key, fields in (snapshot_diagnostics or {}).items():
        if fields:
            outcome[key] = list(fields)
    return outcome


def _utc_instant(value: str) -> datetime:
    try:
        instant = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("identity_repair_invalid_timestamp") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("identity_repair_timestamp_timezone_required")
    return instant.astimezone(timezone.utc)


def _record_at_or_after(record: RecordEnvelope, bound: str) -> bool:
    time_ref = getattr(record, "time", None)
    stamp = str(getattr(time_ref, "created_at", "") or getattr(time_ref, "updated_at", "") or "")
    return _utc_instant(stamp) >= _utc_instant(bound)


def _iter_records(
    runtime,
    *,
    limit: int | None = None,
    scope=None,
    skip_created_at_or_after: str | None = None,
) -> Iterator[RecordEnvelope]:
    page_size = 500
    offset = 0
    yielded_count = 0
    target_limit = None if limit is None or limit <= 0 else int(limit)
    while True:
        remaining = page_size if target_limit is None else max(0, min(page_size, target_limit - yielded_count))
        if remaining <= 0:
            break
        page = runtime.store.list_records(limit=remaining, offset=offset, scope=scope)
        if not page:
            break
        page_count = len(page)
        for record in page:
            if skip_created_at_or_after and _record_at_or_after(record, skip_created_at_or_after):
                continue
            if bool((getattr(record, "meta", {}) or {}).get("identity_stamped_on_ingest")):
                if not needs_hongtu_identity_repair(record):
                    continue
            yield record
            yielded_count += 1
        del record
        page.clear()
        offset += page_count
