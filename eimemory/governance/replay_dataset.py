from __future__ import annotations

import json
from typing import Any

from eimemory.capabilities.consumer_views import (
    capability_aliases_from_view,
    dynamic_evaluation_view,
    resolve_explicit_capability_attribution,
)
from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog
from eimemory.evaluation.regression_replay import REGRESSION_REPLAY_CASE_REPORT_TYPE, built_in_real_regression_cases
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.governance.replay_quality import govern_replay_cases
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
    include_built_in_regressions: bool = False,
    capability_scope: str = "global",
    profile_key: str = "",
    catalog: CapabilityEvaluationCatalog | None = None,
    at_time: str = "",
    include_catalog_cases: bool | None = None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    """Build a bounded replay dataset from an exact active catalog selection.

    Dynamic catalog cases are included by default.  Historical keyword
    classification and bundled regression fixtures have no implicit path; a
    caller must explicitly request ``legacy_compatibility=True`` to use them.
    """

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    budget = max(1, int(limit or 50))
    if legacy_compatibility and include_catalog_cases is True:
        return _blocked_dataset_report(
            {
                "reason": "legacy_mode_cannot_include_dynamic_catalog_cases",
                "errors": [],
            },
            include_built_in_regressions=include_built_in_regressions,
            include_catalog_cases=True,
            legacy_compatibility=True,
        )
    if include_built_in_regressions and not legacy_compatibility:
        return _blocked_dataset_report(
            {
                "reason": "legacy_regression_fixture_requires_explicit_legacy_compatibility",
                "errors": [],
            },
            include_built_in_regressions=True,
            include_catalog_cases=bool(include_catalog_cases),
            legacy_compatibility=False,
        )
    dynamic_requested = not legacy_compatibility
    resolved_include_catalog_cases = (
        dynamic_requested if include_catalog_cases is None else bool(include_catalog_cases)
    )
    evaluation_view: dict[str, Any] = {}
    attribution_context: dict[str, Any] | None = None
    if dynamic_requested:
        evaluation_view = dynamic_evaluation_view(
            runtime,
            scope=scope_ref,
            capability_scope=capability_scope,
            profile_key=profile_key,
            catalog=catalog,
            at_time=at_time,
            # Dataset output size must not silently narrow the authoritative
            # evaluation selection.  The catalog itself supplies the bounded
            # 256-case control-plane limit.
            max_cases=256,
        )
        if evaluation_view.get("ok") is not True:
            return _blocked_dataset_report(
                evaluation_view,
                include_built_in_regressions=include_built_in_regressions,
                include_catalog_cases=resolved_include_catalog_cases,
                legacy_compatibility=legacy_compatibility,
            )
        attribution_context = _attribution_context_from_evaluation_view(evaluation_view)
    cases = _cases_from_event_tables(
        runtime,
        scope=scope_ref,
        limit=budget,
        attribution_context=attribution_context,
    )
    cases.extend(
        _cases_from_outcome_traces(
            runtime,
            scope=scope_ref,
            limit=budget,
            attribution_context=attribution_context,
        )
    )
    cases.extend(
        _cases_from_regression_replay_records(
            runtime,
            scope=scope_ref,
            limit=budget,
            attribution_context=attribution_context,
        )
    )
    cases.extend(
        _cases_from_operator_corrections(
            runtime,
            scope=scope_ref,
            limit=budget,
            attribution_context=attribution_context,
        )
    )
    cases.extend(
        _cases_from_replay_results(
            runtime,
            scope=scope_ref,
            limit=budget,
            attribution_context=attribution_context,
        )
    )
    if resolved_include_catalog_cases:
        cases.extend(_cases_from_evaluation_catalog(evaluation_view))
    if include_built_in_regressions:
        # Reached only through the explicit legacy compatibility mode above.
        cases.extend(built_in_real_regression_cases())
    quality_report = govern_replay_cases(cases, limit=budget * 3)
    deduped_cases = _dedupe_cases(quality_report["cases"])[:budget]
    case_quality_breakdown = dict(quality_report["case_quality_breakdown"])
    case_quality_breakdown["accepted"] = len(deduped_cases)
    correction_count = sum(1 for case in deduped_cases if case.get("correction_from_user"))
    persisted_record_id = ""
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="replay_result",
            title="Proactive replay dataset",
            summary=(
                f"Built {len(deduped_cases)} replay cases from outcomes and corrections; "
                f"filtered {quality_report['filtered_count']} noisy cases."
            ),
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
            content={
                "schema_version": REAL_TASK_REPLAY_SCHEMA_VERSION,
                "cases": deduped_cases,
                "capability_evaluation_view": evaluation_view,
            },
            meta={
                "report_type": REPLAY_DATASET_REPORT_TYPE,
                "schema_version": REAL_TASK_REPLAY_SCHEMA_VERSION,
                "case_count": len(deduped_cases),
                "correction_count": correction_count,
                "filtered_count": quality_report["filtered_count"],
                "filter_reasons": quality_report["filter_reasons"],
                "quality_score": quality_report["quality_score"],
                "case_quality_breakdown": case_quality_breakdown,
                "target_pass_rate": quality_report["target_pass_rate"],
                "limit": budget,
                "source_systems": _source_systems(deduped_cases),
                "include_built_in_regressions": bool(include_built_in_regressions),
                "include_catalog_cases": bool(resolved_include_catalog_cases),
                "capability_scope": capability_scope if dynamic_requested else "",
                "profile_key": str(profile_key or "") if dynamic_requested else "",
                "evaluation_selection_status": str(evaluation_view.get("status") or "legacy_shadow"),
                "legacy_compatibility": bool(legacy_compatibility),
            },
        )
        persisted_record_id = record.record_id
    return {
        "ok": True,
        "schema_version": REAL_TASK_REPLAY_SCHEMA_VERSION,
        "report_type": REPLAY_DATASET_REPORT_TYPE,
        "case_count": len(deduped_cases),
        "correction_count": correction_count,
        "filtered_count": quality_report["filtered_count"],
        "filter_reasons": quality_report["filter_reasons"],
        "quality_score": quality_report["quality_score"],
        "case_quality_breakdown": case_quality_breakdown,
        "target_pass_rate": quality_report["target_pass_rate"],
        "source_systems": _source_systems(deduped_cases),
        "include_built_in_regressions": bool(include_built_in_regressions),
        "include_catalog_cases": bool(resolved_include_catalog_cases),
        "capability_evaluation_view": evaluation_view,
        "legacy_compatibility": bool(legacy_compatibility),
        "persisted": bool(persist),
        "persisted_record_id": persisted_record_id,
        "cases": deduped_cases,
    }


def _cases_from_event_tables(
    runtime: Any,
    *,
    scope: ScopeRef,
    limit: int,
    attribution_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
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
        target_capability, capability_attribution = _target_capability(
            [outcome, event],
            attribution_context=attribution_context,
            legacy_text=" ".join(
                str(value)
                for value in [event.get("event_type"), event.get("user_phrase"), outcome.get("reason"), outcome.get("policy_update")]
            ),
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
                "labels": [outcome_label, target_capability],
                "target_capability": target_capability,
                "capability_attribution": capability_attribution,
                "task_type": _first_text(outcome.get("task_type"), event.get("event_type"), outcome.get("task_type")),
                "outcome": outcome_label or "unknown",
                "correction_from_user": correction,
                "evidence": [_first_text(event.get("id"), row["event_id"])],
            }
        )
    return cases


def _cases_from_outcome_traces(
    runtime: Any,
    *,
    scope: ScopeRef,
    limit: int,
    attribution_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    records = _records_by_meta_value(
        runtime,
        kinds=["reflection"],
        scope=scope,
        meta_key="report_type",
        meta_value="outcome_trace",
        limit=max(1, int(limit or 1)) * 3,
    )
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
        payload = content.get("payload") if isinstance(content.get("payload"), dict) else {}
        target_capability, capability_attribution = _target_capability(
            [content, payload, meta],
            attribution_context=attribution_context,
            legacy_text=" ".join(str(value) for value in [content.get("event_type"), content.get("input_summary"), content.get("reason")]),
        )
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
                "labels": [primary_label, target_capability],
                "target_capability": target_capability,
                "capability_attribution": capability_attribution,
                "task_type": _first_text(content.get("task_type"), payload.get("task_type")),
                "outcome": primary_label.lower() or "unknown",
                "correction_from_user": correction,
                "evidence": [record.record_id],
            }
        )
    return cases


def _cases_from_operator_corrections(
    runtime: Any,
    *,
    scope: ScopeRef,
    limit: int,
    attribution_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
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
        target_capability, capability_attribution = _target_capability(
            [content, meta],
            attribution_context=attribution_context,
            legacy_text=f"{record.title} {record.summary}",
        )
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
                "target_capability": target_capability,
                "capability_attribution": capability_attribution,
                "task_type": _first_text(meta.get("task_type"), content.get("task_type")),
                "outcome": "user_correction",
                "correction_from_user": correction,
                "evidence": [record.record_id],
            }
        )
    return cases


def _cases_from_regression_replay_records(
    runtime: Any,
    *,
    scope: ScopeRef,
    limit: int,
    attribution_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    records = _records_by_meta_value(
        runtime,
        kinds=["reflection"],
        scope=scope,
        meta_key="report_type",
        meta_value=REGRESSION_REPLAY_CASE_REPORT_TYPE,
        limit=max(1, int(limit or 1)) * 3,
    )
    cases: list[dict[str, Any]] = []
    for record in records:
        meta = business_metadata(record.meta)
        if str(meta.get("report_type") or "") != REGRESSION_REPLAY_CASE_REPORT_TYPE:
            continue
        content = record.content if isinstance(record.content, dict) else {}
        payload = content.get("case") if isinstance(content.get("case"), dict) else content
        mistake_type = _first_text(payload.get("mistake_type"), meta.get("mistake_type"))
        query = _first_text(payload.get("query"), payload.get("input"), payload.get("prompt"), record.summary, record.title)
        expected_text = _coerce_string_list(payload.get("expected_text") or payload.get("expect_any_text") or payload.get("expected"))
        expected = _first_text(payload.get("expected"), expected_text[0] if expected_text else "")
        target_capability, capability_attribution = _target_capability(
            [payload, meta],
            attribution_context=attribution_context,
            legacy_text=" ".join([mistake_type, query, expected]),
            prefer_legacy_explicit=True,
        )
        case_id = _first_text(payload.get("case_id"), payload.get("id")) or stable_semantic_key(
            "regression_replay_case",
            record.record_id,
            mistake_type,
            query,
            expected,
        )
        labels = _coerce_string_list(payload.get("labels"))
        if REGRESSION_REPLAY_CASE_REPORT_TYPE not in labels:
            labels.append(REGRESSION_REPLAY_CASE_REPORT_TYPE)
        if mistake_type and mistake_type not in labels:
            labels.append(mistake_type)
        cases.append(
            {
                "case_id": case_id,
                "source": REGRESSION_REPLAY_CASE_REPORT_TYPE,
                "source_system": _source_system_from_task(_first_text(payload.get("source_system"), target_capability, record.source)),
                "event_id": record.record_id,
                "query": query,
                "input": query,
                "expected": expected,
                "expected_text": expected_text,
                "labels": labels,
                "target_capability": target_capability,
                "capability_attribution": capability_attribution,
                "task_type": _first_text(payload.get("task_type"), target_capability),
                "outcome": "regression_replay",
                "correction_from_user": _first_text(payload.get("correction_from_user"), payload.get("correction")),
                "evidence": [record.record_id],
                "mistake_type": mistake_type,
            }
        )
    return cases


def _cases_from_replay_results(
    runtime: Any,
    *,
    scope: ScopeRef,
    limit: int,
    attribution_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
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
            target_capability, capability_attribution = _target_capability(
                [sample, content, meta],
                attribution_context=attribution_context,
                legacy_text=" ".join(
                    (
                        _first_text(sample.get("primary_label")),
                        _first_text(sample.get("query")),
                        _first_text(sample.get("task_type")),
                    )
                ),
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
                    "capability_attribution": capability_attribution,
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
            target_capability, capability_attribution = _target_capability(
                [content, meta],
                attribution_context=attribution_context,
                legacy_text=" ".join([record.title, record.summary]),
            )
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
                    "target_capability": target_capability,
                    "capability_attribution": capability_attribution,
                    "task_type": "replay_result",
                    "outcome": verdict or "replay",
                    "correction_from_user": _first_text(meta.get("correction_from_user"), content.get("correction")),
                    "evidence": [record.record_id],
                }
            )
    return cases


def _attribution_context_from_evaluation_view(evaluation_view: dict[str, Any]) -> dict[str, Any]:
    capability_view = (
        evaluation_view.get("capability_view")
        if isinstance(evaluation_view.get("capability_view"), dict)
        else {}
    )
    capabilities = capability_view.get("capabilities") if isinstance(capability_view.get("capabilities"), list) else []
    allowed = [
        str(item.get("capability_id") or "").strip()
        for item in capabilities
        if isinstance(item, dict) and str(item.get("capability_id") or "").strip()
    ]
    return {
        "allowed_capability_ids": tuple(sorted(set(allowed))),
        "aliases": capability_aliases_from_view(capability_view),
    }


def _target_capability(
    payloads: list[dict[str, Any]],
    *,
    attribution_context: dict[str, Any] | None,
    legacy_text: str,
    prefer_legacy_explicit: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Return a data-backed target or an explicit unclassified result.

    No dynamic path reaches the legacy text classifier.  That classifier is
    retained only for old callers that do not request a catalog/profile view,
    which keeps the v2 replay-dataset shadow contract intact through WP15.
    """

    if attribution_context is None:
        if prefer_legacy_explicit:
            for payload in payloads:
                for key in ("target_capability", "capability_id", "capability", "capability_domain"):
                    value = str(payload.get(key) or "").strip()
                    if value:
                        return value, {
                            "schema": "capability.consumer_attribution.v1",
                            "status": "legacy_shadow",
                            "capability_id": value,
                            "reason": "legacy_explicit_field",
                            "source": f"explicit_field:{key}",
                            "rule_id": "",
                            "migration_id": "",
                        }
        capability = _legacy_classify_text(legacy_text)
        return capability, {
            "schema": "capability.consumer_attribution.v1",
            "status": "legacy_shadow",
            "capability_id": capability,
            "reason": "legacy_keyword_classifier",
            "source": "",
            "rule_id": "",
            "migration_id": "",
        }
    attribution = resolve_explicit_capability_attribution(
        payloads,
        allowed_capability_ids=attribution_context.get("allowed_capability_ids") or (),
        aliases=attribution_context.get("aliases") or {},
    )
    return str(attribution.get("capability_id") or "unclassified"), attribution


def _cases_from_evaluation_catalog(evaluation_view: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose selected catalog cases as replay candidates without executing them."""

    cases: list[dict[str, Any]] = []
    for entry in evaluation_view.get("cases") or ():
        if not isinstance(entry, dict):
            continue
        artifact = entry.get("artifact") if isinstance(entry.get("artifact"), dict) else {}
        target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
        case_id = str(artifact.get("case_id") or "").strip()
        capability_id = str(artifact.get("capability") or "").strip()
        case_digest = str(artifact.get("evaluation_case_digest") or "").strip()
        revision_id = str(target.get("capability_revision_id") or "").strip()
        binding_id = str(target.get("provider_binding_id") or "").strip()
        if not case_id or not capability_id or not case_digest or not revision_id or not binding_id:
            continue
        input_data = artifact.get("input") if isinstance(artifact.get("input"), dict) else {}
        fixture = artifact.get("fixture") if isinstance(artifact.get("fixture"), dict) else {}
        query = _first_text(
            input_data.get("query"),
            input_data.get("input"),
            input_data.get("prompt"),
            input_data.get("task"),
            json.dumps(input_data, ensure_ascii=False, sort_keys=True),
        )
        expected = _first_text(
            fixture.get("expected"),
            fixture.get("expected_behavior"),
            fixture.get("expectation"),
            f"Run immutable evaluation case {case_id} and satisfy its declared invariants.",
        )
        cases.append(
            {
                "case_id": case_id,
                "source": "capability_evaluation_catalog",
                "source_system": "eimemory",
                "event_id": "",
                "query": query,
                "input": query,
                "expected": expected,
                "expected_text": [expected],
                "labels": ["capability_evaluation_catalog", case_id],
                "target_capability": capability_id,
                "capability_attribution": {
                    "schema": "capability.consumer_attribution.v1",
                    "status": "classified",
                    "capability_id": capability_id,
                    "reason": "catalog_case_target",
                    "source": "evaluation_catalog",
                    "rule_id": "catalog_case_target",
                    "migration_id": "",
                },
                "capability_revision_id": revision_id,
                "provider_binding_id": binding_id,
                "eval_spec_id": str(artifact.get("eval_spec_id") or ""),
                "evaluation_case_digest": case_digest,
                "task_type": capability_id,
                "outcome": "catalog_declared",
                "correction_from_user": "",
                "evidence": [f"evaluation-case:{case_digest}"],
            }
        )
    return cases


def _blocked_dataset_report(
    evaluation_view: dict[str, Any],
    *,
    include_built_in_regressions: bool,
    include_catalog_cases: bool,
    legacy_compatibility: bool,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "blocked",
        "schema_version": REAL_TASK_REPLAY_SCHEMA_VERSION,
        "report_type": REPLAY_DATASET_REPORT_TYPE,
        "reason": str(evaluation_view.get("reason") or "capability_evaluation_selection_blocked"),
        "errors": [str(item) for item in evaluation_view.get("errors") or ()],
        "case_count": 0,
        "correction_count": 0,
        "filtered_count": 0,
        "filter_reasons": {},
        "quality_score": 0.0,
        "case_quality_breakdown": {},
        "target_pass_rate": 0.0,
        "source_systems": [],
        "include_built_in_regressions": bool(include_built_in_regressions),
        "include_catalog_cases": bool(include_catalog_cases),
        "capability_evaluation_view": evaluation_view,
        "legacy_compatibility": bool(legacy_compatibility),
        "persisted": False,
        "persisted_record_id": "",
        "cases": [],
    }


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


def _legacy_classify_text(text: str) -> str:
    """Historical keyword classifier; reachable only from legacy mode."""

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
    return "unclassified"


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


def _first_text(*values: Any) -> str:
    for value in values:
        text = " ".join(str(value or "").split())
        if text:
            return text
    return ""
