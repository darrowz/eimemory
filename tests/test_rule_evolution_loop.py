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


def test_rule_evolution_treats_malformed_replay_pass_rate_as_zero_without_promoting(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}
    rule = runtime.evolution.store_rule(
        title="Malformed replay rule",
        summary="Do not promote without numeric replay evidence",
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
    runtime.store.append(
        RecordEnvelope.create(
            kind="replay_result",
            title="Malformed replay pass rate",
            summary="Replay verdict text is pass but pass_rate is malformed.",
            source="test.rule_evolution_loop",
            scope=ScopeRef.from_dict(scope),
            content={"target_rule_id": rule.record_id, "verdict": "pass", "pass_rate": "bad"},
            meta={"target_rule_id": rule.record_id, "verdict": "pass", "pass_rate": "bad"},
        )
    )

    report = run_rule_evolution_loop(runtime, scope, apply=True, min_roi=0.0)

    persisted = runtime.store.get_by_id(rule.record_id)
    assert report["replay_count"] == 1
    assert report["roi_summary"]["replay_pass_rate"] == 0.0
    assert report["roi_summary"]["average_pass_rate"] == 0.0
    assert report["promoted_count"] == 0
    assert persisted.status == "accepted"


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


def test_rule_evolution_creates_candidate_from_eval_incident_repair_hint(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    incident = runtime.evolution.observe(
        signal_type="incident",
        payload={
            "title": "Memory eval failure: official-channel",
            "summary": "Memory evaluation sample failed.",
            "incident_type": "memory_eval_failure",
            "severity": "medium",
            "eval_failure": True,
            "eval_phase": "usage",
            "repair_hint": "Prefer Feishu as the official coordination channel.",
            "suggested_replay_dataset": [
                {
                    "query": "official coordination channel",
                    "scope": scope,
                    "expect_any_text": ["Feishu"],
                    "limit": 3,
                }
            ],
        },
        scope=scope,
    )

    report = run_rule_evolution_loop(runtime, scope, apply=True)
    rules = runtime.store.list_records(kinds=["rule"], scope=scope, limit=10)

    assert report["source_counts"]["incident_repair"] == 1
    assert report["record_ids"]["source_incidents"] == [incident.record_id]
    assert rules[0].summary == "Prefer Feishu as the official coordination channel."
    assert rules[0].meta["evolution_source_type"] == "incident_repair"
    assert rules[0].meta["evolution_source_record_ids"] == [incident.record_id]
    assert rules[0].meta["incident_record_id"] == incident.record_id
    assert rules[0].meta["eval_phase"] == "usage"
    assert rules[0].summary == report["candidates"][0]["summary"]
    assert report["candidates"][0]["source_type"] == "incident_repair"
    assert incident.record_id in report["candidates"][0]["source_record_ids"]


def test_rule_evolution_derives_repair_hint_from_eval_incident_summary(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    incident = runtime.evolution.observe(
        signal_type="incident",
        payload={
            "title": "Memory eval failure: stale-source",
            "summary": "Memory evaluation cited stale project status.",
            "detail": "The answer should have checked the latest project tracker before claiming completion.",
            "incident_type": "memory_eval_failure",
            "severity": "medium",
            "eval_failure": True,
            "eval_phase": "usage",
            "suggested_replay_dataset": [
                {
                    "query": "latest project status",
                    "scope": scope,
                    "expect_any_text": ["latest project tracker"],
                    "limit": 3,
                }
            ],
        },
        scope=scope,
    )

    report = run_rule_evolution_loop(runtime, scope, apply=True)
    rules = runtime.store.list_records(kinds=["rule"], scope=scope, limit=10)

    assert report["candidate_count"] == 1
    assert report["source_counts"]["incident_repair"] == 1
    assert report["record_ids"]["source_incidents"] == [incident.record_id]
    assert rules[0].summary == "Prevent recurrence of: Memory evaluation cited stale project status."
    assert rules[0].meta["repair_hint_source"] == "derived"
    assert rules[0].meta["suggested_replay_dataset"][0]["query"] == "latest project status"
    assert report["candidates"][0]["suggested_replay_dataset"][0]["expect_any_text"] == ["latest project tracker"]


def test_rule_evolution_activates_rule_from_operator_preference_memory(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    memory = runtime.memory.ingest(
        text="鸿哥 沟通风格：极简、直接，讨厌废话；先给结论，少解释。",
        memory_type="preference",
        title="Hongtu operator communication style",
        source="operator.correction",
        force_capture=True,
        scope=scope,
    )

    report = run_rule_evolution_loop(runtime, scope, apply=True)
    rules = runtime.store.list_records(kinds=["rule"], scope=scope, status="active", limit=10)

    assert report["candidate_count"] == 1
    assert report["created_rule_count"] == 1
    assert report["active_rule_count"] == 1
    assert report["source_counts"]["memory_preference"] == 1
    assert report["record_ids"]["source_memories"] == [memory.record_id]
    assert rules[0].status == "active"
    assert rules[0].summary == "鸿哥 沟通风格：极简、直接，讨厌废话；先给结论，少解释。"
    assert rules[0].meta["evolution_source_type"] == "memory_preference"
    assert rules[0].meta["evolution_source_record_ids"] == [memory.record_id]

    second_report = run_rule_evolution_loop(runtime, scope, apply=True)
    assert second_report["candidate_count"] == 0
    assert second_report["active_rule_count"] == 1
    assert second_report["steady_state"] is True
    assert second_report["no_op_reason"] == "all_candidate_sources_already_materialized"
    assert second_report["skipped_source_counts"]["memory_preference"] == 1
    assert second_report["record_ids"]["existing_source_memories"] == [memory.record_id]
