import json
from pathlib import Path

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.api.runtime import Runtime
from eimemory.knowledge.compiler import compile_paper_knowledge
from eimemory.models.claim_cards import ClaimCard
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef
from eimemory.scheduler.jobs import run_nightly_jobs


def test_rule_lifecycle_review_and_promote_updates_active_policy(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    rule = runtime.evolution.store_rule(
        title="Prefer task context",
        summary="Use task context first",
        task_type="brain.respond",
        retrieval_policy={"route_hint": "task_context_first"},
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
        status="candidate",
    )

    reviewed = runtime.evolution.review_rule(
        record_id=rule.record_id,
        decision="accepted",
        reviewer="operator",
        note="Looks good",
    )
    promoted = runtime.evolution.promote_rule(
        record_id=rule.record_id,
        promoter="operator",
        note="Enable in runtime",
    )
    policy = runtime.evolution.get_active_policy(
        task_type="brain.respond",
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
    )

    assert reviewed.status == "accepted"
    assert promoted.status == "active"
    assert policy["retrieval_policy"]["route_hint"] == "task_context_first"


def test_replay_and_roi_reports_capture_evolution_value(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.memory.ingest(
        text="Use short replies for embodied output",
        memory_type="preference",
        title="Short replies",
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
    )
    rule = runtime.evolution.store_rule(
        title="Reply style rule",
        summary="Prefer concise style",
        task_type="brain.respond",
        retrieval_policy={"route_hint": "task_context_first"},
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
        status="active",
    )
    runtime.evolution.observe(
        signal_type="incident",
        payload={
            "incident_type": "reply_too_long",
            "severity": "medium",
            "title": "Long reply incident",
            "summary": "Reply length exceeded desired style",
        },
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
    )
    runtime.evolution.feedback(
        target_ref={"kind": "rule", "record_id": rule.record_id},
        decision="accept",
        reason="Improved retrieval behavior",
        reviewed_by="operator",
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
    )

    replay = runtime.evolution.replay_rule(
        record_id=rule.record_id,
        dataset=[
            {
                "query": "short embodied output",
                "scope": {"agent_id": "eibrain", "workspace_id": "robot"},
                "task_context": {"task_type": "brain.respond"},
                "expect_any_title": ["Short replies"],
            }
        ],
    )
    roi = runtime.evolution.build_roi_report(
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
    )

    assert replay.kind == "replay_result"
    assert replay.meta["verdict"] == "pass"
    assert roi["incident_count"] == 1
    assert roi["accepted_feedback_count"] == 1


def test_evaluate_recall_dataset_reports_hits_misses_and_profile(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.memory.ingest(
        text="Prefer concise replies for operator-facing prompts",
        memory_type="preference",
        title="Concise replies",
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
    )

    report = runtime.evolution.evaluate_recall_dataset(
        dataset=[
            {
                "query": "concise replies",
                "scope": {"agent_id": "eibrain", "workspace_id": "robot"},
                "task_context": {"task_type": "brain.respond"},
                "expect_any_title": ["Concise replies"],
            },
            {
                "query": "missing operator preference",
                "scope": {"agent_id": "eibrain", "workspace_id": "robot"},
                "task_context": {"task_type": "brain.respond"},
                "expect_any_title": ["Missing memory"],
            },
        ],
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
        task_type="brain.respond",
        profile="balanced",
    )

    assert report["sample_count"] == 2
    assert report["hit_count"] == 1
    assert report["miss_count"] == 1
    assert report["pass_rate"] == 0.5
    assert report["profile"] == "balanced"
    assert report["task_type"] == "brain.respond"
    assert len(report["misses"]) == 1
    assert report["misses"][0]["query"] == "missing operator preference"


def test_evaluate_recall_dataset_handles_malformed_rows_without_crashing(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = runtime.evolution.evaluate_recall_dataset(
        dataset=[None, "not-a-sample"],
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
        task_type="brain.respond",
    )

    assert report["sample_count"] == 2
    assert report["hit_count"] == 0
    assert report["miss_count"] == 2
    assert [item["error"] for item in report["misses"]] == ["invalid_sample", "invalid_sample"]


def test_promotion_candidates_require_accepted_rules_and_passing_replays(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "robot"}

    runtime.memory.ingest(
        text="Passing target memory for promotion evaluation",
        memory_type="preference",
        title="Passing target",
        scope=scope,
    )

    passing_rule = runtime.evolution.store_rule(
        title="Passing rule",
        summary="Promote when replay passes",
        task_type="brain.respond",
        retrieval_policy={"route_hint": "task_context_first"},
        scope=scope,
        status="accepted",
    )
    runtime.evolution.feedback(
        target_ref={"kind": "rule", "record_id": passing_rule.record_id},
        decision="accept",
        reason="Reviewer approved the rule",
        reviewed_by="reviewer",
        scope=scope,
    )
    runtime.evolution.replay_rule(
        record_id=passing_rule.record_id,
        dataset=[
            {
                "query": "passing target memory",
                "scope": scope,
                "task_context": {"task_type": "brain.respond"},
                "expect_any_title": ["Passing target"],
            }
        ],
    )

    failing_rule = runtime.evolution.store_rule(
        title="Failing rule",
        summary="Stay blocked when replay misses",
        task_type="brain.respond",
        retrieval_policy={"route_hint": "semantic_first"},
        scope=scope,
        status="accepted",
    )
    runtime.evolution.feedback(
        target_ref={"kind": "rule", "record_id": failing_rule.record_id},
        decision="accept",
        reason="Reviewer approved the rule",
        reviewed_by="reviewer",
        scope=scope,
    )
    runtime.evolution.replay_rule(
        record_id=failing_rule.record_id,
        dataset=[
            {
                "query": "missing target memory",
                "scope": scope,
                "task_context": {"task_type": "brain.respond"},
                "expect_any_title": ["Definitely absent"],
            }
        ],
    )

    report = runtime.evolution.promotion_candidates(scope=scope, min_pass_rate=0.8)

    assert report["candidate_count"] == 1
    assert report["blocked_count"] == 1
    assert report["candidates"][0]["record_id"] == passing_rule.record_id
    assert report["candidates"][0]["status"] == "accepted"
    assert report["candidates"][0]["latest_replay_result"]["pass_rate"] >= 0.8
    assert report["blocked"][0]["record_id"] == failing_rule.record_id
    assert "pass_rate" in report["blocked"][0]["blocked_reason"]
    assert runtime.store.get_by_id(passing_rule.record_id).status == "accepted"


def test_eibrain_rpc_bridge_handles_recall_and_observe(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.memory.ingest(
        text="Prefer concise replies",
        memory_type="preference",
        title="Concise replies",
        scope={"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
    )
    bridge = EIBrainRPCBridge(runtime)

    recall_response = bridge.handle(
        {
            "method": "memory.recall",
            "params": {
                "query": "concise replies",
                "scope": {"agent_id": "eibrain", "workspace_id": "robot"},
                "task_context": {"task_type": "brain.respond"},
            },
        }
    )
    observe_response = bridge.handle(
        {
            "method": "evolution.observe",
            "params": {
                "signal_type": "incident",
                "payload": {
                    "incident_type": "noise",
                    "title": "Noise incident",
                    "summary": "Ignore noise",
                },
                "scope": {"agent_id": "eibrain", "workspace_id": "robot"},
            },
        }
    )

    assert recall_response["ok"] is True
    assert recall_response["result"]["items"]
    assert observe_response["ok"] is True
    assert observe_response["result"]["kind"] == "incident"


def test_openclaw_plugin_manifest_points_to_bridge_plugin() -> None:
    manifest_path = Path("integrations/openclaw/openclaw.plugin.json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert payload["name"] == "eimemory-bridge"
    assert payload["id"] == "eimemory-bridge"
    assert payload["activation"] == {"onStartup": True, "onCapabilities": ["hook"]}
    assert payload["hooks"] == ["message_received", "before_prompt_build", "agent_end"]
    assert payload["contracts"]["tools"] == ["eimemory_bridge_status"]
    assert payload["main"] == "./eimemory-bridge/index.js"
    assert payload["configSchema"]["type"] == "object"
    assert "Bridge" in payload["displayName"]


def test_nightly_jobs_emit_replay_and_promotion_summary(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.memory.ingest(
        text="Use concise answers for operator responses",
        memory_type="preference",
        title="Concise operator replies",
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
    )
    active_rule = runtime.evolution.store_rule(
        title="Operator recall rule",
        summary="Prefer operator reply preferences",
        task_type="brain.respond",
        retrieval_policy={"route_hint": "task_context_first"},
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
        status="active",
    )
    runtime.evolution.store_rule(
        title="Candidate response rule",
        summary="Promote after review",
        task_type="brain.respond",
        retrieval_policy={"route_hint": "semantic_first"},
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
        status="accepted",
    )

    report = run_nightly_jobs(
        runtime,
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
        replay_datasets={
            active_rule.record_id: [
                {
                    "query": "concise operator responses",
                    "scope": {"agent_id": "eibrain", "workspace_id": "robot"},
                    "task_context": {"task_type": "brain.respond"},
                    "expect_any_title": ["Concise operator replies"],
                }
            ]
        },
    )

    assert report["active_rule_count"] == 1
    assert report["promotion_candidate_count"] == 1
    assert report["replay"]["executed"] == 1
    assert report["replay"]["pass_count"] == 1


def test_new_conflicting_claim_marks_contradiction_and_triggers_refresh(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="papers")
    first = ClaimCard(
        claim_card_id="claim_positive",
        paper_source_id="paper_conflict_a",
        paper_extract_id="extract_a",
        claim_text="Method A improves accuracy.",
        confidence=0.8,
    ).to_record(scope=scope)
    second = ClaimCard(
        claim_card_id="claim_negative",
        paper_source_id="paper_conflict_b",
        paper_extract_id="extract_b",
        claim_text="Method A does not improve accuracy.",
        confidence=0.8,
    ).to_record(scope=scope)
    runtime.store.append(first)
    runtime.store.append(second)
    compiled = compile_paper_knowledge(
        paper_source_id="paper_conflict_a",
        paper_title="Method A",
        claims=[first.summary, second.summary],
        entities=["Method A"],
    )
    for record in compiled.to_records(scope=scope):
        runtime.store.append(record)

    report = runtime.evolution.reconcile_knowledge(scope={"agent_id": "main", "workspace_id": "papers"})
    refreshed_pages = runtime.store.list_records(
        kinds=["knowledge_page"],
        scope={"agent_id": "main", "workspace_id": "papers"},
        status="needs_refresh",
    )
    contradiction_edges = [
        record
        for record in runtime.store.list_records(
            kinds=["relation_record"],
            scope={"agent_id": "main", "workspace_id": "papers"},
            limit=20,
        )
        if record.content.get("relation_type") == "contradicts"
    ]
    updated_first = runtime.store.get_by_id(first.record_id)

    assert report["contradiction_count"] >= 1
    assert report["page_refresh_count"] >= 1
    assert refreshed_pages
    assert contradiction_edges
    assert updated_first is not None
    assert updated_first.meta["reliability"] < 0.8


def test_nightly_jobs_include_knowledge_evolution_summary(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="papers")
    for claim_id, text in [
        ("claim_positive", "Method A improves accuracy."),
        ("claim_negative", "Method A does not improve accuracy."),
    ]:
        runtime.store.append(
            ClaimCard(
                claim_card_id=claim_id,
                paper_source_id="paper_conflict",
                paper_extract_id="extract_conflict",
                claim_text=text,
                confidence=0.8,
            ).to_record(scope=scope)
        )

    report = run_nightly_jobs(runtime, scope={"agent_id": "main", "workspace_id": "papers"})

    assert report["knowledge"]["claim_card_count"] == 2
    assert report["knowledge"]["contradiction_count"] >= 1


def test_memory_quality_report_summarizes_distribution_salience_and_sources(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="quality")
    records = [
        RecordEnvelope.create(
            kind="memory",
            title="Candidate memory",
            summary="Candidate operator context",
            content={"text": "Candidate operator context", "memory_type": "conversation"},
            scope=scope,
            source="openclaw.message_received",
            meta={
                "memory_type": "conversation",
                "quality": {
                    "quality_tier": "candidate",
                    "capture_decision": "accept",
                    "salience_score": 0.4,
                },
            },
        ),
        RecordEnvelope.create(
            kind="memory",
            title="Confirmed memory",
            summary="Confirmed project decision",
            content={"text": "Confirmed project decision", "memory_type": "decision"},
            scope=scope,
            source="cli",
            meta={
                "memory_type": "decision",
                "quality": {
                    "quality_tier": "confirmed",
                    "capture_decision": "accept",
                    "salience_score": 0.7,
                },
            },
        ),
        RecordEnvelope.create(
            kind="memory",
            title="Core memory",
            summary="Core OpenClaw memory contract",
            content={"text": "Core OpenClaw memory contract", "memory_type": "fact"},
            scope=scope,
            source="cli",
            meta={
                "memory_type": "fact",
                "quality": {
                    "quality_tier": "core",
                    "capture_decision": "accept",
                    "salience_score": 0.9,
                },
            },
        ),
        RecordEnvelope.create(
            kind="memory",
            title="Rejected memory",
            summary="Rejected noisy input",
            content={"text": "ok", "memory_type": "conversation"},
            scope=scope,
            source="openclaw.message_received",
            status="rejected",
            meta={
                "memory_type": "conversation",
                "quality": {
                    "quality_tier": "candidate",
                    "capture_decision": "reject",
                    "salience_score": 0.1,
                },
            },
        ),
    ]
    for record in records:
        runtime.store.append(record)

    report = runtime.evolution.memory_quality_report(scope={"agent_id": "main", "workspace_id": "quality"})

    assert report["memory_count"] == 4
    assert report["accepted_count"] == 3
    assert report["quality_distribution"] == {
        "candidate": 1,
        "confirmed": 1,
        "core": 1,
        "rejected": 1,
    }
    assert report["average_salience"] == 0.525
    assert report["by_source"]["cli"] == 2
    assert report["by_source"]["openclaw.message_received"] == 2
    assert report["by_memory_type"]["conversation"] == 2
    assert report["by_memory_type"]["decision"] == 1


def test_memory_quality_repair_dry_run_reports_without_mutating(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repair")
    legacy = RecordEnvelope.create(
        kind="memory",
        title="Legacy memory",
        summary="Decision: keep eimemory repair reports deterministic.",
        content={"text": "Decision: keep eimemory repair reports deterministic.", "memory_type": "decision"},
        scope=scope,
        source="legacy",
        meta={"memory_type": "decision"},
    )
    legacy.meta.pop("quality", None)
    runtime.store.append(legacy)

    report = runtime.evolution.repair_memory_quality(
        scope={"agent_id": "main", "workspace_id": "repair"},
    )
    unchanged = runtime.store.get_by_id(legacy.record_id)

    assert report["applied"] is False
    assert report["scanned_count"] == 1
    assert report["backfilled_count"] == 1
    assert report["rejected_count"] == 0
    assert report["duplicate_count"] == 0
    assert report["actions"][0]["action"] == "backfill_quality"
    assert unchanged is not None
    assert "quality" not in unchanged.meta


def test_memory_quality_repair_apply_backfills_rejects_mojibake_and_duplicates(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repair")
    missing_quality = RecordEnvelope.create(
        kind="memory",
        title="Legacy decision",
        summary="Decision: eimemory repair should backfill old memories.",
        content={"text": "Decision: eimemory repair should backfill old memories.", "memory_type": "decision"},
        scope=scope,
        source="legacy",
        meta={"memory_type": "decision"},
    )
    missing_quality.meta.pop("quality", None)
    mojibake = RecordEnvelope.create(
        kind="memory",
        title="Broken encoding",
        summary="????? ????? ????? ????",
        content={"text": "????? ????? ????? ????", "memory_type": "conversation"},
        scope=scope,
        source="legacy",
        meta={"memory_type": "conversation"},
    )
    original = RecordEnvelope.create(
        kind="memory",
        title="Original duplicate",
        summary="Remember that OpenClaw uses eimemory for durable scoped recall.",
        content={"text": "Remember that OpenClaw uses eimemory for durable scoped recall.", "memory_type": "fact"},
        scope=scope,
        source="legacy",
        meta={
            "memory_type": "fact",
            "quality": {
                "quality_tier": "confirmed",
                "capture_decision": "accept",
                "salience_score": 0.8,
            },
        },
    )
    duplicate = RecordEnvelope.create(
        kind="memory",
        title="Newer duplicate",
        summary="Remember that OpenClaw uses eimemory for durable scoped recall.",
        content={"text": "Remember that OpenClaw uses eimemory for durable scoped recall.", "memory_type": "fact"},
        scope=scope,
        source="legacy",
        meta={
            "memory_type": "fact",
            "quality": {
                "quality_tier": "candidate",
                "capture_decision": "accept",
                "salience_score": 0.3,
            },
        },
    )
    for record in [missing_quality, mojibake, original, duplicate]:
        runtime.store.append(record)

    report = runtime.evolution.repair_memory_quality(
        scope={"agent_id": "main", "workspace_id": "repair"},
        apply=True,
    )

    repaired = runtime.store.get_by_id(missing_quality.record_id)
    rejected_mojibake = runtime.store.get_by_id(mojibake.record_id)
    kept_original = runtime.store.get_by_id(original.record_id)
    rejected_duplicate = runtime.store.get_by_id(duplicate.record_id)

    assert report["applied"] is True
    assert report["scanned_count"] == 4
    assert report["backfilled_count"] == 1
    assert report["rejected_count"] == 2
    assert report["duplicate_count"] == 1
    assert repaired is not None
    assert repaired.meta["quality"]["capture_decision"] == "accept"
    assert rejected_mojibake is not None
    assert rejected_mojibake.status == "rejected"
    assert rejected_mojibake.meta["rejection_reason"] == "mojibake_or_noise"
    assert kept_original is not None
    assert kept_original.status == "active"
    assert rejected_duplicate is not None
    assert rejected_duplicate.status == "rejected"
    assert rejected_duplicate.meta["duplicate_of"] == original.record_id
    assert rejected_duplicate.meta["rejection_reason"] == "duplicate"


def test_nightly_jobs_include_memory_quality_observability(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.memory.ingest(
        text="Decision: eimemory should keep high-value OpenClaw project memories.",
        memory_type="decision",
        title="Memory quality decision",
        scope={"agent_id": "main", "workspace_id": "quality"},
    )

    report = run_nightly_jobs(runtime, scope={"agent_id": "main", "workspace_id": "quality"})

    assert report["memory_quality"]["memory_count"] == 1
    high_quality_count = (
        report["memory_quality"]["quality_distribution"]["confirmed"]
        + report["memory_quality"]["quality_distribution"]["core"]
    )
    assert high_quality_count == 1
    assert report["memory_quality"]["average_salience"] > 0



def test_replay_rule_uses_runtime_recall_pipeline_for_graph_expansion(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="eibrain", workspace_id="robot")
    supporting = RecordEnvelope.create(
        kind="memory",
        title="Linked catalog entry",
        summary="Ceramic glaze catalog entry.",
        scope=scope,
    )
    primary = RecordEnvelope.create(
        kind="memory",
        title="Operator reply preference",
        summary="Respond briefly to the operator",
        scope=scope,
        links=[
            LinkRef(
                relation="supports",
                target_kind="memory",
                target_id=supporting.record_id,
            )
        ],
    )
    runtime.store.append(supporting)
    runtime.store.append(primary)
    rule = runtime.evolution.store_rule(
        title="Graph-aware replay",
        summary="Replay should use runtime recall",
        task_type="brain.respond",
        retrieval_policy={"route_hint": "task_context_first"},
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
        status="accepted",
    )

    replay = runtime.evolution.replay_rule(
        record_id=rule.record_id,
        dataset=[
            {
                "query": "brief operator reply",
                "scope": {"agent_id": "eibrain", "workspace_id": "robot"},
                "task_context": {"task_type": "brain.respond"},
                "expect_any_title": ["Linked catalog entry"],
            }
        ],
    )

    assert replay.meta["verdict"] == "pass"
    assert replay.meta["pass_rate"] == 1.0



def test_evaluate_recall_dataset_uses_runtime_recall_pipeline_for_graph_expansion(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="eibrain", workspace_id="robot")
    supporting = RecordEnvelope.create(
        kind="memory",
        title="Linked catalog entry",
        summary="Ceramic glaze catalog entry.",
        scope=scope,
    )
    primary = RecordEnvelope.create(
        kind="memory",
        title="Operator reply preference",
        summary="Respond briefly to the operator",
        scope=scope,
        links=[
            LinkRef(
                relation="supports",
                target_kind="memory",
                target_id=supporting.record_id,
            )
        ],
    )
    runtime.store.append(supporting)
    runtime.store.append(primary)

    report = runtime.evolution.evaluate_recall_dataset(
        dataset=[
            {
                "query": "brief operator reply",
                "scope": {"agent_id": "eibrain", "workspace_id": "robot"},
                "task_context": {"task_type": "brain.respond"},
                "expect_any_title": ["Linked catalog entry"],
            }
        ],
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
        task_type="brain.respond",
        profile="balanced",
    )

    assert report["hit_count"] == 1
    assert report["miss_count"] == 0
    assert report["samples"][0]["returned_titles"] == ["Operator reply preference", "Linked catalog entry"]
