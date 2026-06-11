from eimemory.api.runtime import Runtime
from eimemory.governance.rule_evolution import run_rule_evolution_loop
from eimemory.models.records import RecordEnvelope, ScopeRef


def _append_outcome_trace(
    runtime: Runtime,
    scope: dict,
    *,
    primary_label: str,
    signals: list[str],
    risk_level: str = "L1",
    query: str = "Inspect the console failure",
    confidence: float | None = None,
    correction: str | None = None,
) -> RecordEnvelope:
    diagnosis = {
        "expected_text": ["open console", "inspect failure"],
        "negative_expected_text": ["answered from memory"],
        "signals": signals,
    }
    if correction:
        diagnosis["correction"] = correction
    meta = {
        "report_type": "outcome_trace",
        "schema_version": "outcome_trace.v1",
        "task_type": "ops.inspect",
        "primary_label": primary_label,
        "diagnosis_signals": signals,
        "risk_level": risk_level,
    }
    if confidence is not None:
        meta["confidence"] = confidence
    trace = RecordEnvelope.create(
        kind="reflection",
        title="Outcome trace",
        summary="Outcome trace for failed task.",
        source="experience.outcome",
        scope=ScopeRef.from_dict(scope),
        content={
            "payload": {
                "query": query,
                "actual_response": "I answered from memory without checking the console.",
            },
            "diagnosis": diagnosis,
            "world_state": {"expected": "console inspected", "observed": "not inspected"},
            "visual_evidence": {"missing": "latest failure screenshot was not inspected"},
            "operator_gap": {
                "expected_behavior": correction or "Open console before answering.",
                "observed_behavior": "No console was opened.",
            },
        },
        meta=meta,
    )
    return runtime.store.append(trace)


def test_rule_evolution_creates_shadow_candidate_from_repeated_operator_gap(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    first = _append_outcome_trace(
        runtime,
        scope,
        primary_label="missing_tool_call",
        signals=["operator_gap"],
    )
    second = _append_outcome_trace(
        runtime,
        scope,
        primary_label="missing_tool_call",
        signals=["operator_gap"],
    )

    report = run_rule_evolution_loop(runtime, scope, apply=True)
    rules = runtime.store.list_records(kinds=["rule"], scope=scope, limit=10)

    assert report["candidate_count"] == 2
    assert report["source_counts"]["operator_gap"] == 1
    assert set(report["record_ids"]["source_outcome_traces"]) == {first.record_id, second.record_id}
    assert set(report["record_ids"]["source_operator_gaps"]) == {first.record_id, second.record_id}
    operator_rule = next(rule for rule in rules if rule.meta["candidate_source"] == "operator_gap")
    operator_report = next(candidate for candidate in report["candidates"] if candidate["candidate_source"] == "operator_gap")
    assert operator_rule.status == "active"
    assert operator_rule.meta["search_stage"] == "seed"
    assert operator_rule.meta["evolution_source_type"] == "operator_gap"
    assert set(operator_rule.meta["source_outcome_trace_ids"]) == {first.record_id, second.record_id}
    assert set(operator_rule.meta["evolution_source_record_ids"]) == {first.record_id, second.record_id}
    assert {
        item["source_outcome_trace_id"] for item in operator_rule.meta["suggested_replay_dataset"]
    } == {first.record_id, second.record_id}
    assert operator_rule.meta["proxy_eval"]["matched_replay_count"] == 2
    assert operator_rule.meta["promotion_gate"]["allow_auto_promote"] is True
    assert operator_rule.meta["promotion_gate"]["requires_review"] is False
    assert operator_report["proxy_eval"]["matched_replay_count"] == 2
    assert report["outcome_replay_count"] == 2
    assert report["promoted_count"] == 2
    assert report["active_rule_count"] == 2
    replay_results = runtime.store.list_records(kinds=["replay_result"], scope=scope, limit=10)
    assert len(replay_results) == 2
    assert all(result.meta["replay_source"] == "outcome_trace_suggested_replay" for result in replay_results)
    assert all(result.meta["verdict"] == "pass" for result in replay_results)

    second_report = run_rule_evolution_loop(runtime, scope, apply=True)
    assert second_report["candidate_count"] == 0
    assert second_report["replay_count"] == 2
    assert second_report["record_ids"]["source_outcome_traces"] == []


def test_rule_evolution_creates_candidate_from_single_high_confidence_operator_correction(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    trace = _append_outcome_trace(
        runtime,
        scope,
        primary_label="user_correction",
        signals=["operator_correction"],
        confidence=0.91,
        correction="Open the console and inspect the failure before answering.",
    )

    report = run_rule_evolution_loop(runtime, scope, apply=True)
    rules = runtime.store.list_records(kinds=["rule"], scope=scope, limit=10)

    assert report["candidate_count"] == 1
    assert report["source_counts"]["operator_gap"] == 1
    assert report["record_ids"]["source_outcome_traces"] == [trace.record_id]
    assert report["record_ids"]["source_operator_gaps"] == [trace.record_id]
    assert report["outcome_replay_count"] == 1
    assert report["promoted_count"] == 1
    assert rules[0].meta["candidate_source"] == "operator_gap"
    assert rules[0].meta["source_outcome_trace_ids"] == [trace.record_id]
    assert rules[0].meta["suggested_replay_dataset"][0]["source_outcome_trace_id"] == trace.record_id
    assert set(report["candidates"][0]["suggested_replay_dataset"][0]["expect_any_text"]) >= {
        "open console",
        "inspect failure",
        "Open the console and inspect the failure before answering.",
    }


def test_rule_evolution_blocks_single_low_confidence_generic_outcome(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    _append_outcome_trace(
        runtime,
        scope,
        primary_label="missing_tool_call",
        signals=["generic_failure"],
        confidence=0.31,
    )

    report = run_rule_evolution_loop(runtime, scope, apply=True)

    assert report["candidate_count"] == 0
    assert report["outcome_replay_count"] == 0
    assert runtime.store.list_records(kinds=["rule"], scope=scope, limit=10) == []


def test_rule_evolution_high_risk_visual_and_world_candidates_never_active(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    first = _append_outcome_trace(
        runtime,
        scope,
        primary_label="unsafe_or_high_risk",
        signals=["missing_visual_evidence", "world_state_mismatch"],
        risk_level="high",
        query="Move the device after checking the camera",
    )
    second = _append_outcome_trace(
        runtime,
        scope,
        primary_label="unsafe_or_high_risk",
        signals=["missing_visual_evidence", "world_state_mismatch"],
        risk_level="high",
        query="Move the device after checking the camera",
    )

    report = run_rule_evolution_loop(runtime, scope, apply=True)
    rules = runtime.store.list_records(kinds=["rule"], scope=scope, limit=10)
    statuses = {rule.status for rule in rules}

    assert report["source_counts"]["visual_evidence_gap"] == 1
    assert report["source_counts"]["world_state_mismatch"] == 1
    assert set(report["record_ids"]["source_visual_evidence_gaps"]) == {first.record_id, second.record_id}
    assert set(report["record_ids"]["source_world_state_mismatches"]) == {first.record_id, second.record_id}
    assert statuses == {"candidate"}
    assert all(rule.meta["risk_level"] == "high" for rule in rules)
    assert all(rule.meta["promotion_gate"]["allow_auto_promote"] is False for rule in rules)
    assert report["outcome_replay_count"] == 4
    assert report["promoted_count"] == 0


def test_rule_evolution_does_not_generate_from_success_or_unknown_outcomes(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    _append_outcome_trace(runtime, scope, primary_label="success", signals=["operator_gap"])
    _append_outcome_trace(runtime, scope, primary_label="unknown_failure", signals=["operator_gap"])

    report = run_rule_evolution_loop(runtime, scope, apply=True)

    assert report["candidate_count"] == 0
    assert runtime.store.list_records(kinds=["rule"], scope=scope, limit=10) == []


def test_rule_evolution_finds_outcome_traces_beyond_first_reflection_page(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    first = _append_outcome_trace(
        runtime,
        scope,
        primary_label="missing_tool_call",
        signals=["operator_gap"],
    )
    second = _append_outcome_trace(
        runtime,
        scope,
        primary_label="missing_tool_call",
        signals=["operator_gap"],
    )
    scope_ref = ScopeRef.from_dict(scope)
    for index in range(550):
        runtime.store.append(
            RecordEnvelope.create(
                kind="reflection",
                title=f"Newer reflection noise {index}",
                summary="not an outcome trace",
                scope=scope_ref,
            )
        )

    report = run_rule_evolution_loop(runtime, scope, apply=True)

    assert report["candidate_count"] == 2
    assert set(report["record_ids"]["source_outcome_traces"]) == {first.record_id, second.record_id}
    assert report["outcome_replay_count"] == 2
