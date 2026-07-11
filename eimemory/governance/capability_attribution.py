from __future__ import annotations

import json
from statistics import mean
from typing import Any

from eimemory.experience.capability_contract import (
    contract_source_ids,
    normalize_capability_contract,
    validate_capability_contract,
)
from eimemory.governance.capability_ledger import SEEDED_LEDGER_CAPABILITIES, record_capability_score
from eimemory.governance.learning_state import stable_semantic_key
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef


BUSINESS_CAPABILITIES = {
    "operations.uumit",
    "research.synthesis",
    "search.discovery",
    "office.daily_task",
    "device.control",
}

CAPABILITY_TERMS: dict[str, tuple[str, ...]] = {
    "operations.uumit": (
        "uumit",
        "business delivery",
        "delivery plan",
        "customer delivery",
        "operational_check",
        "operations",
    ),
    "research.synthesis": (
        "research",
        "synthesis",
        "synthesize",
        "paper",
        "brief",
        "claim",
        "knowledge",
    ),
    "search.discovery": (
        "search",
        "discovery",
        "discover",
        "trending_search",
        "github_star_ranking",
        "github",
        "web search",
        "source verification",
        "搜索",
        "查找",
        "热门",
        "趋势",
        "最高星",
    ),
    "office.daily_task": (
        "office",
        "daily",
        "meeting",
        "calendar",
        "notes",
        "document",
        "email",
        "todo",
    ),
    "device.control": (
        "device",
        "control",
        "media_playback",
        "playback",
        "speaker",
        "audio",
        "browser",
        "physical",
    ),
}


def collect_capability_evidence(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 500,
) -> dict[str, list[dict[str, Any]]]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    grouped: dict[str, list[dict[str, Any]]] = {capability: [] for capability in BUSINESS_CAPABILITIES}
    seen: set[tuple[str, str]] = set()
    for evidence in [*_evidence_from_outcome_traces(runtime, scope=scope_ref, limit=limit), *_evidence_from_event_outcomes(runtime, scope=scope_ref, limit=limit)]:
        for capability in evidence.get("capabilities") or []:
            if capability not in BUSINESS_CAPABILITIES:
                continue
            key = (capability, str(evidence.get("source_id") or ""))
            if key in seen:
                continue
            seen.add(key)
            grouped.setdefault(capability, []).append({**evidence, "capability": capability})
    return {capability: items for capability, items in grouped.items() if items}


def attribute_capability_outcomes(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "outcome_attribution",
    limit: int = 500,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    evidence_by_capability = collect_capability_evidence(runtime, scope=scope_ref, limit=limit)
    record_ids: list[str] = []
    capabilities: dict[str, dict[str, Any]] = {}
    for capability in sorted(evidence_by_capability):
        evidence_items = evidence_by_capability[capability]
        source_ids = [str(item.get("source_id") or "") for item in evidence_items if str(item.get("source_id") or "")]
        score = round(mean(float(item.get("score") or 0.0) for item in evidence_items), 3)
        record_id = record_capability_score(
            runtime,
            scope=scope_ref,
            loop_id=loop_id,
            capability=capability,
            score=score,
            evidence_record_ids=source_ids,
            regression_count=sum(1 for item in evidence_items if bool(item.get("regression"))),
            evidence_items=evidence_items,
            evidence_tiers=sorted({str(item.get("evidence_tier") or "") for item in evidence_items if str(item.get("evidence_tier") or "")}),
            evidence_sources=sorted({str(item.get("source_kind") or "") for item in evidence_items if str(item.get("source_kind") or "")}),
        )
        record_ids.append(record_id)
        capabilities[capability] = {
            "score": score,
            "evidence_count": len(evidence_items),
            "evidence_record_ids": source_ids,
        }
    return {
        "ok": True,
        "capabilities": capabilities,
        "record_count": len(record_ids),
        "record_ids": record_ids,
    }


def _evidence_from_outcome_traces(runtime: Any, *, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for record in _records_by_meta_value(
        runtime,
        kinds=["reflection"],
        scope=scope,
        meta_key="report_type",
        meta_value="outcome_trace",
        limit=limit,
    ):
        meta = business_metadata(record.meta)
        if str(meta.get("report_type") or "") != "outcome_trace":
            continue
        payload = record.content.get("payload") if isinstance(record.content, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        verifier = payload.get("verifier") if isinstance(payload.get("verifier"), dict) else {}
        outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
        outcome_status = str(meta.get("outcome_status") or outcome.get("status") or "").strip().lower()
        if outcome_status in {"success", "good", "passed", "pass", "completed"} and verifier.get("passed") is not True:
            continue
        policy_attribution = _dict_value(record.content.get("policy_attribution") if isinstance(record.content, dict) else None) or _dict_value(payload.get("policy_attribution"))
        contract = normalize_capability_contract(payload.get("capability_contract"))
        contract_error = validate_capability_contract(contract) if contract else "capability contract missing"
        contract_verified = not contract_error
        if contract_verified:
            capabilities = [str(contract["capability"])]
            case_id = str(contract["case_id"])
            source_record_ids = contract_source_ids(contract)
        else:
            text = _join_text(
                record.title,
                record.summary,
                meta.get("task_type"),
                meta.get("primary_label"),
                payload.get("task_type"),
                payload.get("input_summary"),
                payload.get("feedback"),
                payload.get("outcome"),
                policy_attribution,
            )
            capabilities = _capabilities_from_text(text)
            case_id = ""
            source_record_ids = []
        if not capabilities:
            continue
        evidence.append(
            {
                "source_id": record.record_id,
                "source_kind": "outcome_trace",
                "evidence_tier": "T0",
                "score": _score_outcome(status=str(meta.get("outcome_status") or ""), primary_label=str(meta.get("primary_label") or "")),
                "summary": record.summary,
                "capabilities": capabilities,
                "contract_verified": contract_verified,
                "case_id": case_id,
                "source_record_ids": source_record_ids,
                "contract_schema": str(contract.get("schema_version") or "") if contract_verified else "",
                "observation": dict(contract.get("observations") or {}) if contract_verified else {},
                "policy_attribution": policy_attribution,
                "regression": str(meta.get("primary_label") or "") not in {"", "success"},
                "semantic_key": stable_semantic_key("capability_evidence", record.record_id),
            }
        )
    return evidence


def _evidence_from_event_outcomes(runtime: Any, *, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    store = getattr(runtime, "store", None)
    conn = getattr(store, "conn", None) or getattr(getattr(store, "sqlite", None), "conn", None)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT
                o.id AS outcome_id,
                o.event_id AS event_id,
                o.outcome AS outcome_name,
                o.reason AS reason,
                o.correction_from_user AS correction_from_user,
                o.policy_update AS policy_update,
                o.payload_json AS outcome_payload,
                e.payload_json AS event_payload
            FROM event_outcomes o
            LEFT JOIN events e
              ON e.id = o.event_id
             AND e.tenant_id = o.tenant_id
             AND e.agent_id = o.agent_id
             AND e.workspace_id = o.workspace_id
             AND e.user_id = o.user_id
            WHERE o.tenant_id = ?
              AND o.agent_id = ?
              AND o.workspace_id = ?
              AND o.user_id = ?
            ORDER BY o.recorded_at DESC
            LIMIT ?
            """,
            (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id, max(1, int(limit))),
        ).fetchall()
    except Exception:
        return []
    evidence: list[dict[str, Any]] = []
    for row in rows:
        outcome = _json_dict(row["outcome_payload"])
        event = _json_dict(row["event_payload"])
        policy_attribution = _dict_value(outcome.get("policy_attribution")) or _dict_value(event.get("policy_attribution"))
        text = _join_text(
            row["outcome_name"],
            row["reason"],
            row["correction_from_user"],
            row["policy_update"],
            outcome,
            event,
            policy_attribution,
        )
        capabilities = _capabilities_from_text(text)
        if not capabilities:
            continue
        outcome_name = str(outcome.get("outcome") or row["outcome_name"] or "")
        source_id = str(row["outcome_id"] or row["event_id"] or "")
        evidence.append(
            {
                "source_id": source_id,
                "source_kind": "event_outcome",
                "evidence_tier": "T0",
                "score": _score_outcome(status=outcome_name, primary_label=""),
                "summary": str(row["reason"] or row["correction_from_user"] or row["policy_update"] or event.get("user_phrase") or ""),
                "capabilities": capabilities,
                "contract_verified": False,
                "case_id": "",
                "source_record_ids": [],
                "contract_schema": "",
                "observation": {},
                "policy_attribution": policy_attribution,
                "regression": outcome_name.lower() in {"bad", "failure", "failed", "verification_missing"},
                "semantic_key": stable_semantic_key("capability_evidence", source_id),
            }
        )
    return evidence


def _capabilities_from_text(text: str) -> list[str]:
    value = str(text or "").lower()
    matched = [
        capability
        for capability in SEEDED_LEDGER_CAPABILITIES
        if capability in BUSINESS_CAPABILITIES
        and any(term in value for term in CAPABILITY_TERMS.get(capability, (capability,)))
    ]
    return sorted(set(matched))


def _score_outcome(*, status: str, primary_label: str) -> float:
    status_value = str(status or "").strip().lower()
    label = str(primary_label or "").strip().lower()
    if status_value in {"success", "good", "passed", "pass"} or label == "success":
        return 0.82
    if status_value in {"verification_missing", "uncertain"}:
        return 0.48
    if status_value in {"bad", "failure", "failed", "error"} or (label and label != "success"):
        return 0.32
    return 0.55


def _join_text(*values: Any) -> str:
    return " ".join(_text_value(value) for value in values if _text_value(value))


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _records_by_meta_value(
    runtime: Any,
    *,
    kinds: list[str],
    scope: ScopeRef,
    meta_key: str,
    meta_value: Any,
    limit: int,
) -> list[Any]:
    lookup = getattr(runtime.store, "list_records_by_meta_value", None)
    if callable(lookup):
        records = lookup(
            kinds=kinds,
            scope=scope,
            meta_key=meta_key,
            meta_value=meta_value,
            limit=limit,
        )
        if records is not None:
            return list(records)
    return runtime.store.list_records(kinds=kinds, scope=scope, limit=limit)
