from eimemory.governance.candidate_search import generate_candidate_policies, score_proxy_candidates
from eimemory.governance.outcome_replay import build_replay_case_from_outcome
from eimemory.models.records import RecordEnvelope, ScopeRef


def _outcome_trace(*, primary_label: str = "missing_tool_call", signals: list[str] | None = None) -> RecordEnvelope:
    scope = ScopeRef.from_dict({"agent_id": "eibrain", "workspace_id": "robot"})
    return RecordEnvelope.create(
        kind="reflection",
        title="Outcome trace",
        summary="The task ended without opening the required tool.",
        source="experience.outcome",
        scope=scope,
        content={
            "payload": {
                "query": "Open the console and inspect the latest failed job",
                "actual_response": "I can explain the console workflow instead.",
            },
            "diagnosis": {
                "expected_text": ["open the console", "inspect the failed job"],
                "negative_expected_text": ["explain the workflow instead"],
                "signals": signals or ["missing_required_tool_call", "operator_gap"],
            },
            "operator_gap": {
                "expected_behavior": "Open the console before answering.",
                "observed_behavior": "Answered without tool use.",
            },
        },
        meta={
            "report_type": "outcome_trace",
            "schema_version": "outcome_trace.v1",
            "task_type": "ops.inspect",
            "primary_label": primary_label,
            "diagnosis_signals": signals or ["missing_required_tool_call", "operator_gap"],
            "risk_level": "L1",
        },
    )


def test_build_replay_case_from_bad_outcome_trace() -> None:
    trace = _outcome_trace()

    replay_case = build_replay_case_from_outcome(trace)

    assert replay_case["query"] == "Open the console and inspect the latest failed job"
    assert replay_case["expected_text"][:2] == ["open the console", "inspect the failed job"]
    assert replay_case["negative_expected_text"] == ["explain the workflow instead"]
    assert replay_case["risk_level"] == "L1"
    assert replay_case["source_outcome_trace_id"] == trace.record_id
    assert replay_case["primary_label"] == "missing_tool_call"
    assert replay_case["signals"] == ["missing_required_tool_call", "operator_gap"]


def test_build_replay_case_skips_success_and_unknown_outcomes() -> None:
    assert build_replay_case_from_outcome(_outcome_trace(primary_label="success")) == {}
    assert build_replay_case_from_outcome(_outcome_trace(primary_label="unknown_failure")) == {}


def test_candidate_search_scores_deterministic_seed_candidates() -> None:
    replay_cases = [
        build_replay_case_from_outcome(_outcome_trace()),
        build_replay_case_from_outcome(_outcome_trace()),
    ]

    candidates = generate_candidate_policies(replay_cases)
    scored = score_proxy_candidates(candidates, replay_cases)

    assert 2 <= len(candidates) <= 5
    assert scored["top_candidate"]["candidate_source"] == "operator_gap"
    assert scored["top_candidate"]["audit_meta"]["search_stage"] == "seed"
    assert scored["proxy_eval"]["matched_replay_count"] == 2
    assert scored["proxy_eval"]["score"] > 0
