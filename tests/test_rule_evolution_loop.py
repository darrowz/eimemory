from eimemory.api.runtime import Runtime
from eimemory.governance.rule_evolution import run_rule_evolution_loop
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_feedback_creates_accepted_rule_candidate(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    feedback = runtime.evolution.feedback(
        target_ref={"kind": "memory", "record_id": "mem_123"},
        decision="accept",
        reason="Always prefer concise embodied replies",
        reviewed_by="operator",
        scope=scope,
    )
    reflection = runtime.evolution.log_reflection(
        tag="brain.respond",
        miss="Reply was too long",
        fix="Use concise embodied replies",
        scope=scope,
    )

    report = run_rule_evolution_loop(runtime, scope, apply=True)
    rules = runtime.evolution.list_rules(scope=scope, status="accepted")

    assert report["candidate_count"] == 1
    assert report["promoted_count"] == 0
    assert report["record_ids"]["source_feedback"] == [feedback.record_id]
    assert report["record_ids"]["source_reflections"] == [reflection.record_id]
    assert report["record_ids"]["created_rules"] == [rules[0].record_id]
    assert rules[0].status == "accepted"
    assert rules[0].title == "Rule: Always prefer concise embodied replies"
    assert rules[0].meta["evolution_source_feedback_id"] == feedback.record_id
    assert rules[0].meta["evolution_source_reflection_id"] == reflection.record_id


def test_replay_result_promotes_accepted_rule_when_roi_passes(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    runtime.memory.ingest(
        text="Prefer short embodied replies",
        memory_type="preference",
        title="Short embodied replies",
        scope=scope,
    )
    rule = runtime.evolution.store_rule(
        title="Concise reply rule",
        summary="Prefer short embodied replies",
        task_type="brain.respond",
        retrieval_policy={"route_hint": "task_context_first"},
        scope=scope,
        status="accepted",
    )
    runtime.evolution.feedback(
        target_ref={"kind": "rule", "record_id": rule.record_id},
        decision="accept",
        reason="Accepted after review",
        reviewed_by="operator",
        scope=scope,
    )
    replay = runtime.evolution.replay_rule(
        record_id=rule.record_id,
        dataset=[
            {
                "query": "short embodied replies",
                "scope": scope,
                "task_context": {"task_type": "brain.respond"},
                "expect_any_title": ["Short embodied replies"],
            }
        ],
    )

    report = run_rule_evolution_loop(runtime, scope, apply=True, min_roi=0.0)

    promoted = runtime.store.get_by_id(rule.record_id)
    assert promoted.status == "active"
    assert report["candidate_count"] == 0
    assert report["promoted_count"] == 1
    assert report["replay_count"] == 1
    assert report["roi_summary"]["replay_pass_rate"] == 1.0
    assert report["record_ids"]["replay_results"] == [replay.record_id]
    assert report["record_ids"]["promoted_rules"] == [rule.record_id]


def test_apply_false_reports_without_writing(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    runtime.evolution.feedback(
        target_ref={"kind": "memory", "record_id": "mem_123"},
        decision="accept",
        reason="Always mention uncertainty when evidence is thin",
        reviewed_by="operator",
        scope=scope,
    )

    report = run_rule_evolution_loop(runtime, scope, apply=False)

    assert report["candidate_count"] == 1
    assert report["promoted_count"] == 0
    assert report["record_ids"]["created_rules"] == []
    assert report["record_ids"]["promoted_rules"] == []
    assert runtime.evolution.list_rules(scope=scope) == []


def test_rule_evolution_ignores_non_replay_report_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    runtime.store.append(
        RecordEnvelope.create(
            kind="replay_result",
            title="Historical rule evolution report",
            summary="This report should not count as a replay.",
            source="eimemory.rule_evolution_loop",
            scope=ScopeRef.from_dict(scope),
            content={"report": {"candidate_count": 1}},
            meta={"report_type": "rule_evolution"},
        )
    )

    report = run_rule_evolution_loop(runtime, scope, apply=False)

    assert report["replay_count"] == 0
    assert report["roi_summary"]["replay_pass_rate"] == 0.0


def test_rule_evolution_ignores_daily_brief_reflections_as_rule_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    runtime.store.append(
        RecordEnvelope.create(
            kind="reflection",
            title="Daily experience brief",
            summary="This report should not become rule evidence.",
            source="eimemory.daily_brief",
            scope=ScopeRef.from_dict(scope),
            content={"brief": {"date": "2026-04-29"}},
            meta={"report_type": "daily_brief"},
        )
    )
    runtime.evolution.feedback(
        target_ref={"kind": "memory", "record_id": "mem_123"},
        decision="accept",
        reason="Always cite uncertainty for thin evidence",
        reviewed_by="operator",
        scope=scope,
    )

    report = run_rule_evolution_loop(runtime, scope, apply=False)

    assert report["candidate_count"] == 1
    assert report["record_ids"]["source_reflections"] == []
    assert report["candidates"][0]["source_reflection_id"] == ""
