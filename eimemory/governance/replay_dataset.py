from __future__ import annotations

import json
from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.metadata import business_metadata
from eimemory.models.records import ScopeRef

REPLAY_DATASET_REPORT_TYPE = "proactive_replay_dataset"
REAL_TASK_REPLAY_SCHEMA_VERSION = "real_task_replay.v1"


def build_replay_dataset(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 50,
    persist: bool = True,
    loop_id: str = "manual",
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    budget = max(1, int(limit or 50))
    cases = _cases_from_event_tables(runtime, scope=scope_ref, limit=budget)
    cases.extend(_cases_from_outcome_traces(runtime, scope=scope_ref, limit=budget))
    cases.extend(_cases_from_operator_corrections(runtime, scope=scope_ref, limit=budget))
    cases.extend(_cases_from_replay_results(runtime, scope=scope_ref, limit=budget))
    deduped_cases = _dedupe_cases(cases)[:budget]
    correction_count = sum(1 for case in deduped_cases if case.get("correction_from_user"))
    persisted_record_id = ""
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="replay_result",
            title="Proactive replay dataset",
            summary=f"Built {len(deduped_cases)} replay cases from outcomes and corrections.",
            scope=scope_ref,
            loop_id=loop_id,
            step_name="replay_dataset",
            semantic_key=stable_semantic_key(
                "proactive_replay_dataset",
                scope_ref.tenant_id,
                scope_ref.agent_id,
                scope_ref.workspace_id,
                scope_ref.user_id,
                budget,
                _case_fingerprint(deduped_cases[:5]),
            ),
            authority_tier="L0",
            status="active",
            content={"schema_version": REAL_TASK_REPLAY_SCHEMA_VERSION, "cases": deduped_cases},
            meta={
                "report_type": REPLAY_DATASET_REPORT_TYPE,
                "schema_version": REAL_TASK_REPLAY_SCHEMA_VERSION,
                "case_count": len(deduped_cases),
                "correction_count": correction_count,
                "limit": budget,
                "source_systems": _source_systems(deduped_cases),
            },
        )
        persisted_record_id = record.record_id
    return {
        "ok": True,
        "schema_version": REAL_TASK_REPLAY_SCHEMA_VERSION,
        "report_type": REPLAY_DATASET_REPORT_TYPE,
        "case_count": len(deduped_cases),
        "correction_count": correction_count,
        "source_systems": _source_systems(deduped_cases),
        "persisted": bool(persist),
        "persisted_record_id": persisted_record_id,
        "cases": deduped_cases,
    }


def _cases_from_event_tables(runtime: Any, *, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    store = getattr(runtime, "store", None)
    conn = getattr(store, "conn", None) or getattr(getattr(store, "sqlite", None), "conn", None)
    if conn is None:
        return []
    budget = max(1, int(limit or 1))
    rows = _query_event_outcomes(conn, scope=scope, budget=budget * 3)
    cases: list[dict[str, Any]] = []
    for row in rows:
        event = _loads(row["event_payload"])
        outcome = _loads(row["outcome_payload"])
        outcome_label = str(row["outcome"] or outcome.get("outcome") or "").strip().lower()
        correction = _first_text(
            outcome.get("correction_from_user"),
            outcome.get("correction"),
            outcome.get("feedback"),
            event.get("correction"),
        )
        if outcome_label not in {"bad", "uncertain", "unknown_failure"} and not correction:
            continue
        input_text = _first_text(
            outcome.get("query"),
            event.get("user_phrase"),
            event.get("goal"),
            event.get("interpreted_intent"),
            event.get("input"),
        )
        expected_behavior = _first_text(
            outcome.get("policy_update"),
            correction,
            outcome.get("expected"),
            event.get("goal"),
        )
        expected_text = _coerce_string_list(
            [outcome.get("policy_update"), correction, outcome.get("expected"), outcome.get("reason"), outcome.get("feedback")]
        )
        cases.append(
            {
                "case_id": stable_semantic_key(
                    "event_case",
                    row["event_id"] or "",
                    input_text,
                    expected_behavior,
                ),
                "source": "event_outcome",
                "source_system": _source_system_from_task(_first_text(outcome.get("task_type"), event.get("event_type"), outcome.get("task_type"))),
                "event_id": str(row["event_id"] or ""),
                "query": input_text,
                "input": input_text,
                "expected": expected_behavior,
                "expected_text": expected_text,
                "labels": [outcome_label, _classify(event, outcome)],
                "target_capability": _classify(event, outcome),
                "task_type": _first_text(outcome.get("task_type"), event.get("event_type"), outcome.get("task_type")),
                "outcome": outcome_label or "unknown",
                "correction_from_user": correction,
                "evidence": [_first_text(event.get("id"), row["event_id"])],
            }
        )
    return cases


def _cases_from_outcome_traces(runtime: Any, *, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    records = runtime.store.list_records(kinds=["reflection"], scope=scope, limit=max(1, int(limit or 1)) * 3)
    cases: list[dict[str, Any]] = []
    for record in records:
        meta = business_metadata(record.meta)
        if str(meta.get("report_type") or "") != "outcome_trace":
            continue
        content = record.content if isinstance(record.content, dict) else {}
        primary_label = _first_text(meta.get("primary_label"), content.get("primary_label"))
        correction = _first_text(content.get("correction_from_user"), content.get("correction"), meta.get("correction"), content.get("feedback"))
        if primary_label.lower() in {"success", ""} and not correction:
            continue
        input_text = _first_text(content.get("input_summary"), record.title, record.summary, content.get("query"))
        expected_behavior = _first_text(content.get("policy_update"), correction, content.get("expected"), content.get("expected_text"))
        expected_text = _coerce_string_list(content.get("expected_text") or content.get("expected"))
        cases.append(
            {
                "case_id": stable_semantic_key("outcome_trace_case", record.record_id, input_text, expected_behavior),
                "source": "outcome_trace",
                "source_system": _source_system_from_task(_first_text(content.get("task_type"), content.get("payload", {}).get("task_type"), record.source)),
                "event_id": record.record_id,
                "query": input_text,
                "input": input_text,
                "expected": expected_behavior,
                "expected_text": expected_text,
                "labels": [primary_label, _classify(content, {})],
                "target_capability": _classify(content, {}),
                "task_type": _first_text(content.get("task_type"), content.get("payload", {}).get("task_type")),
                "outcome": primary_label.lower() or "unknown",
                "correction_from_user": correction,
                "evidence": [record.record_id],
            }
        )
    return cases


def _cases_from_operator_corrections(runtime: Any, *, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    records = runtime.store.list_records(kinds=["memory"], scope=scope, limit=max(1, int(limit or 1)) * 3)
    cases: list[dict[str, Any]] = []
    for record in records:
        if record.status != "active":
            continue
        meta = business_metadata(record.meta)
        content = record.content if isinstance(record.content, dict) else {}
        source = str(record.source or "")
        memory_type = _first_text(meta.get("memory_type"), content.get("memory_type"))
        is_operator_correction = source == "operator.correction" or memory_type == "operator.correction"
        if not is_operator_correction:
            continue
        correction = _first_text(content.get("correction"), record.summary, record.title, record.content.get("text"))
        if not correction:
            continue
        input_text = _first_text(
            content.get("query"),
            content.get("goal"),
            content.get("input"),
            record.title,
            record.summary,
        )
        expected_behavior = _first_text(content.get("policy_update"), content.get("expected"), content.get("expected_behavior"))
        cases.append(
            {
                "case_id": stable_semantic_key("operator_correction", record.record_id, correction, input_text),
                "source": "operator_correction",
                "source_system": _source_system_from_task(_first_text(meta.get("task_type"), content.get("task_type"), record.source)),
                "event_id": record.record_id,
                "query": input_text,
                "input": input_text,
                "expected": expected_behavior or correction,
                "expected_text": [expected_behavior or correction],
                "labels": ["operator.correction"],
                "target_capability": _classify_text(f"{record.title} {record.summary}"),
                "task_type": _first_text(meta.get("task_type"), content.get("task_type")),
                "outcome": "user_correction",
                "correction_from_user": correction,
                "evidence": [record.record_id],
            }
        )
    return cases


def _cases_from_replay_results(runtime: Any, *, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    records = runtime.store.list_records(kinds=["replay_result"], scope=scope, limit=max(1, int(limit or 1)) * 3)
    cases: list[dict[str, Any]] = []
    for record in records:
        content = record.content if isinstance(record.content, dict) else {}
        meta = record.meta if isinstance(record.meta, dict) else {}
        report_type = str(meta.get("report_type") or content.get("report_type") or "").strip()
        if report_type in {REPLAY_DATASET_REPORT_TYPE, "real_task_replay"}:
            continue
        verdict = _first_text(meta.get("verdict"))
        dataset = _coerce_list(content.get("suggested_replay_dataset"))
        if not dataset:
            continue
        source_case_count = 0
        for index, sample in enumerate(_coerce_list(dataset)):
            sample = dict(sample) if isinstance(sample, dict) else {}
            query = _first_text(sample.get("query"), sample.get("input"), sample.get("question"), sample.get("prompt"))
            if not query:
                continue
            expected = _first_text(sample.get("expected"), sample.get("expected_text"), sample.get("expected_behavior"))
            expected_text = _coerce_string_list(sample.get("expect_any_text") or sample.get("expected_text"))
            if not expected and expected_text:
                expected = expected_text[0]
            labels = _coerce_string_list(sample.get("labels"))
            if verdict:
                labels.append(verdict)
            target_capability = _classify_text(
                " ".join(
                    (
                        _first_text(sample.get("primary_label")),
                        _first_text(sample.get("query")),
                        _first_text(sample.get("task_type")),
                    )
                )
            )
            case_id = stable_semantic_key(
                "replay_result_case",
                record.record_id,
                query,
                str(index),
                expected,
            )
            cases.append(
                {
                    "case_id": case_id,
                    "source": "replay_result",
                    "source_system": _source_system_from_task(_first_text(sample.get("source_system"), sample.get("task_type"), content.get("task_type"), record.meta.get("task_type"), record.source)),
                    "event_id": record.record_id,
                    "query": query,
                    "input": query,
                    "expected": expected,
                    "expected_text": expected_text,
                    "labels": labels,
                    "target_capability": target_capability,
                    "task_type": _first_text(sample.get("task_type"), content.get("task_type"), record.meta.get("task_type")),
                    "outcome": verdict or "replay",
                    "correction_from_user": _first_text(meta.get("correction_from_user"), sample.get("correction")),
                    "evidence": [record.record_id, sample.get("case_id", "")],
                }
            )
            source_case_count += 1
        if source_case_count == 0:
            query = _first_text(record.title, record.summary, record.record_id)
            if not query:
                continue
            cases.append(
                {
                    "case_id": stable_semantic_key("replay_result_case", record.record_id, query),
                    "source": "replay_result",
                    "source_system": _source_system_from_task(_first_text(record.meta.get("task_type"), record.source)),
                    "event_id": record.record_id,
                    "query": query,
                    "input": query,
                    "expected": _first_text(record.summary),
                    "expected_text": [],
                    "labels": [verdict] if verdict else [],
                    "target_capability": _classify_text(" ".join([record.title, record.summary])),
                    "task_type": "replay_result",
                    "outcome": verdict or "replay",
                    "correction_from_user": _first_text(meta.get("correction_from_user"), content.get("correction")),
                    "evidence": [record.record_id],
                }
            )
    return cases


def _dedupe_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for case in cases:
        key = _case_identity_key(case)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(case)
    return deduped


def _case_identity_key(case: dict[str, Any]) -> str:
    return stable_semantic_key(
        case.get("source"),
        _first_text(case.get("query")),
        _first_text(case.get("expected")),
        _first_text(case.get("correction_from_user")),
    )


def _case_fingerprint(cases: list[dict[str, Any]]) -> str:
    return stable_semantic_key(*[case.get("case_id") for case in cases]) if cases else "empty"


def _source_systems(cases: list[dict[str, Any]]) -> list[str]:
    values = sorted({_first_text(case.get("source_system")) for case in cases if _first_text(case.get("source_system"))})
    return values


def _source_system_from_task(value: Any) -> str:
    text = _first_text(value).lower()
    if "uumit" in text:
        return "uumit"
    if "openclaw" in text or "feishu" in text or "agent" in text:
        return "openclaw"
    if "eimemory" in text or "memory" in text or "replay" in text:
        return "eimemory"
    return "unknown"


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _query_event_outcomes(conn: Any, *, scope: ScopeRef, budget: int) -> list[Any]:
    try:
        return list(
            conn.execute(
                """
                SELECT o.event_id, o.outcome, o.payload_json AS outcome_payload, e.id AS event_id_alias,
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
                (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id, budget),
            ).fetchall()
        )
    except Exception:
        return []


def _classify(event: dict[str, Any], outcome: dict[str, Any]) -> str:
    return _classify_text(" ".join(str(value) for value in [event.get("event_type"), event.get("user_phrase"), outcome.get("reason"), outcome.get("policy_update")]))


def _classify_text(text: str) -> str:
    value = str(text or "").lower()
    if any(term in value for term in ("recall", "memory", "检索", "召回")):
        return "memory.recall"
    if any(term in value for term in ("tool", "route", "hook", "工具")):
        return "tool.routing"
    if any(term in value for term in ("code", "patch", "test", "pytest", "代码")):
        return "code.implementation"
    if any(term in value for term in ("uu", "uumit", "order", "订单", "delivery", "交付")):
        return "operations.uumit"
    if any(term in value for term in ("audio", "song", "device", "播放", "设备")):
        return "device.control"
    if any(term in value for term in ("safety", "risk", "rollback", "边界")):
        return "safety.boundary"
    return "proactive.judgment"


def _coerce_string_list(values: Any) -> list[str]:
    if isinstance(values, str):
        split = [part.strip() for part in values.split("\n") if part.strip()]
        return split if split else [values.strip()]
    if isinstance(values, dict):
        values = values.get("text") or values.get("expected") or []
    if isinstance(values, (list, tuple, set)):
        return [_first_text(value) for value in values if _first_text(value)]
    return [_first_text(values)] if _first_text(values) else []


def _coerce_list(values: Any) -> list[Any]:
    if isinstance(values, list):
        return values
    if isinstance(values, tuple):
        return list(values)
    if isinstance(values, set):
        return list(values)
    return []


def _first_text(*values: Any) -> str:
    for value in values:
        text = " ".join(str(value or "").split())
        if text:
            return text
    return ""
