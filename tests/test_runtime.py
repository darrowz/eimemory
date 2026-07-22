import json
import re

import pytest

from eimemory.api.runtime import Runtime
from eimemory.api import memory as memory_module
from eimemory.cli.main import main as cli_main
from eimemory.knowledge.source_trust import resolve_source_trust
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef


def test_runtime_ingest_and_recall(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    record = runtime.memory.ingest(
        text="OpenClaw should inject memory before prompt build",
        memory_type="fact",
        title="Prompt-build recall",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        tags=["openclaw"],
    )

    bundle = runtime.memory.recall(
        query="inject memory before prompt build",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply", "goal": "answer user"},
        limit=5,
    )

    assert record.kind == "memory"
    assert bundle.items
    assert bundle.items[0].record_id == record.record_id
    assert bundle.explanation["query"] == "inject memory before prompt build"
    assert record.meta["quality"]["capture_decision"] == "accept"
    assert record.meta["quality"]["quality_tier"] in {"confirmed", "core"}


def test_runtime_ingest_rejects_thin_chatter_without_persisting(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    record = runtime.memory.ingest(
        text="ok",
        memory_type="conversation",
        title="Thin chatter",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )
    persisted = runtime.store.list_records(
        kinds=["memory"],
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        limit=10,
    )

    assert record.status == "rejected"
    assert record.meta["quality"]["capture_decision"] == "reject"
    assert record.meta["capture_warnings"][0]["code"] == "thin_or_noisy_risk"
    assert persisted == []


def test_runtime_create_uses_eimemory_root_env(tmp_path, monkeypatch) -> None:
    root = tmp_path / "env-runtime"
    monkeypatch.setenv("EIMEMORY_ROOT", str(root))

    runtime = Runtime.create()

    assert runtime.store.root == root
    runtime.close()


def test_runtime_preference_query_uses_compiled_default_markers(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    assert isinstance(memory_module._DEFAULT_PREFERENCE_QUERY_MARKER_RE, re.Pattern)
    assert runtime.memory._is_preference_query("鸿哥 沟通风格", {}, recall_intent=None)
    assert runtime.memory._is_preference_query(
        "how should this route",
        {"goal": "honor persona route", "preference_query_markers": ["persona route"]},
        recall_intent=None,
    )


def test_runtime_ingest_can_force_capture_low_salience_memory(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    record = runtime.memory.ingest(
        text="ok",
        memory_type="conversation",
        title="Forced short signal",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        force_capture=True,
    )
    persisted = runtime.store.list_records(
        kinds=["memory"],
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        limit=10,
    )

    assert record.status == "active"
    assert record.meta["quality"]["capture_decision"] == "accept"
    assert persisted[0].record_id == record.record_id


def test_runtime_ingest_caller_record_id_conflict_is_rejected_without_side_effects(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"tenant_id": "tenant", "agent_id": "main", "workspace_id": "repo-x", "user_id": "user"}
    first = runtime.memory.ingest(
        text="original protected memory",
        memory_type="durable_fact",
        title="Original",
        scope=scope,
        source="trusted.runtime",
        source_id="alpha",
        force_capture=True,
        record_id="caller-fixed-record",
    )
    before_jsonl = (tmp_path / "records.jsonl").read_bytes()
    before_rows = [tuple(row) for row in runtime.store.sqlite.conn.execute(
        "SELECT storage_key, normalized_alias, alias_ordinal FROM recall_alias_index ORDER BY storage_key, alias_ordinal"
    )]
    try:
        with pytest.raises(ValueError, match="record_id conflict"):
            runtime.memory.ingest(
                text="attacker replacement",
                memory_type="durable_fact",
                title="Replacement",
                scope=scope,
                source="trusted.runtime",
                source_id="alpha",
                force_capture=True,
                record_id="caller-fixed-record",
            )
        persisted = runtime.store.get_by_id(first.record_id, scope=scope)
        assert persisted is not None
        assert persisted.title == "Original"
        assert persisted.content["text"] == "original protected memory"
        assert (tmp_path / "records.jsonl").read_bytes() == before_jsonl
        assert [tuple(row) for row in runtime.store.sqlite.conn.execute(
            "SELECT storage_key, normalized_alias, alias_ordinal FROM recall_alias_index ORDER BY storage_key, alias_ordinal"
        )] == before_rows
    finally:
        runtime.close()


def test_runtime_ingest_identical_caller_record_id_request_is_idempotent(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    request = {
        "text": "stable deterministic memory",
        "memory_type": "durable_fact",
        "title": "Stable",
        "scope": {"agent_id": "main", "workspace_id": "repo-x"},
        "source": "trusted.runtime",
        "source_id": "alpha",
        "force_capture": True,
        "record_id": "caller-stable-record",
    }
    first = runtime.memory.ingest(**request)
    before_jsonl = (tmp_path / "records.jsonl").read_bytes()
    before_changes = runtime.store.sqlite.conn.total_changes
    try:
        second = runtime.memory.ingest(**request)
        assert second.to_dict() == first.to_dict()
        assert (tmp_path / "records.jsonl").read_bytes() == before_jsonl
        assert runtime.store.sqlite.conn.total_changes == before_changes
    finally:
        runtime.close()


def _remove_ingest_request_digest(runtime: Runtime, record: RecordEnvelope) -> RecordEnvelope:
    legacy = runtime.store.get_by_id(record.record_id, scope=record.scope)
    assert legacy is not None
    legacy.meta.pop("ingest_request_digest", None)
    business = legacy.meta.get("business_meta")
    if isinstance(business, dict):
        business.pop("ingest_request_digest", None)
    runtime.store.sqlite.upsert(legacy)
    reloaded = runtime.store.get_by_id(record.record_id, scope=record.scope)
    assert reloaded is not None
    return reloaded


def test_runtime_ingest_identical_legacy_record_without_request_digest_is_idempotent(
    tmp_path,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    request = {
        "text": "legacy stable deterministic memory",
        "memory_type": "durable_fact",
        "title": "Legacy stable",
        "scope": {"agent_id": "main", "workspace_id": "repo-x"},
        "tags": ["legacy", "stable"],
        "source": "trusted.runtime",
        "source_id": "alpha",
        "force_capture": True,
        "meta": {"capture_origin": "legacy-import", "host": "node-a"},
        "content": {"format": "plain"},
        "record_id": "caller-legacy-stable-record",
    }
    first = runtime.memory.ingest(**request)
    legacy = _remove_ingest_request_digest(runtime, first)
    before_changes = runtime.store.sqlite.conn.total_changes
    before_jsonl = (tmp_path / "records.jsonl").read_bytes()
    try:
        retried = runtime.memory.ingest(**request)
        assert retried.to_dict() == legacy.to_dict()
        assert runtime.store.sqlite.conn.total_changes == before_changes
        assert (tmp_path / "records.jsonl").read_bytes() == before_jsonl
    finally:
        runtime.close()


def test_runtime_ingest_changed_legacy_record_without_request_digest_still_conflicts(
    tmp_path,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    request = {
        "text": "legacy protected memory",
        "memory_type": "durable_fact",
        "title": "Legacy protected",
        "scope": {"agent_id": "main", "workspace_id": "repo-x"},
        "source": "trusted.runtime",
        "source_id": "alpha",
        "force_capture": True,
        "record_id": "caller-legacy-protected-record",
    }
    first = runtime.memory.ingest(**request)
    legacy = _remove_ingest_request_digest(runtime, first)
    before_changes = runtime.store.sqlite.conn.total_changes
    try:
        with pytest.raises(ValueError, match="record_id conflict"):
            runtime.memory.ingest(**{**request, "text": "changed attacker replacement"})
        persisted = runtime.store.get_by_id(first.record_id, scope=first.scope)
        assert persisted is not None
        assert persisted.to_dict() == legacy.to_dict()
        assert runtime.store.sqlite.conn.total_changes == before_changes
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "retry_meta",
    [
        {},
        {"custom_flag": "alpha"},
        {"custom_flag": "alpha", "host": "node-a", "extra_flag": "new"},
        {"custom_flag": "beta", "host": "node-a"},
        {"custom_flag": "alpha", "host": "node-b"},
    ],
    ids=(
        "all-meta-omitted",
        "runtime-meta-omitted",
        "business-meta-added",
        "business-meta-changed",
        "runtime-meta-changed",
    ),
)
def test_runtime_ingest_legacy_record_requires_exact_caller_metadata(
    tmp_path,
    retry_meta: dict,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    request = {
        "text": "legacy metadata protected memory",
        "memory_type": "durable_fact",
        "title": "Legacy metadata protected",
        "scope": {"agent_id": "main", "workspace_id": "repo-x"},
        "source": "trusted.runtime",
        "source_id": "alpha",
        "force_capture": True,
        "meta": {"custom_flag": "alpha", "host": "node-a"},
        "record_id": "caller-legacy-metadata-record",
    }
    first = runtime.memory.ingest(**request)
    legacy = _remove_ingest_request_digest(runtime, first)
    before_changes = runtime.store.sqlite.conn.total_changes
    try:
        with pytest.raises(ValueError, match="record_id conflict"):
            runtime.memory.ingest(**{**request, "meta": retry_meta})
        persisted = runtime.store.get_by_id(first.record_id, scope=first.scope)
        assert persisted is not None
        assert persisted.to_dict() == legacy.to_dict()
        assert runtime.store.sqlite.conn.total_changes == before_changes
    finally:
        runtime.close()


def test_runtime_recall_excludes_internal_audit_memories_by_default(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    audit = runtime.memory.ingest(
        text="ei bridge audit says vision saw a desk",
        memory_type="audit",
        title="ei-bridge OpenClaw command audit",
        scope={"agent_id": "hongtu", "workspace_id": "embodied"},
        source="ei_bridge.openclaw_feishu",
        force_capture=True,
    )
    useful = runtime.memory.ingest(
        text="Darrow asked eibrain to describe real objects on the desk",
        memory_type="fact",
        title="Vision object preference",
        scope={"agent_id": "hongtu", "workspace_id": "embodied"},
        source="openclaw.agent_end",
    )

    bundle = runtime.memory.recall(
        query="vision desk objects",
        scope={"agent_id": "hongtu", "workspace_id": "embodied"},
        limit=8,
    )

    record_ids = {item.record_id for item in bundle.items}
    assert useful.record_id in record_ids
    assert audit.record_id not in record_ids


def test_runtime_recall_pollution_guard_blocks_operational_lanes_by_default(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied")
    quality = {
        "importance": 0.95,
        "confidence": 0.95,
        "freshness": 1.0,
        "reuse_potential": 0.95,
        "salience_score": 0.95,
        "quality_tier": "core",
        "capture_decision": "accept",
    }
    runtime.sources.add_source(
        {
            "source_id": "openclaw-official-docs",
            "source_kind": "url",
            "title": "OpenClaw Docs",
            "uri": "https://example.test/openclaw/docs",
            "metadata": {
                "connector_id": "test.fixture",
                "knowledge_source_kind": "official_docs",
                "trust": 1.0,
            },
        }
    )

    def add_memory(memory_type: str, title: str) -> RecordEnvelope:
        meta = {"memory_type": memory_type, "quality": quality}
        if memory_type == "external_knowledge":
            decision = resolve_source_trust(
                {
                    "source_id": "openclaw-official-docs",
                    "source_kind": "official_docs",
                    "source_uri": "https://example.test/openclaw/docs",
                },
                registry=runtime.sources,
                connector_id="test.fixture",
            )
            meta.update(
                {
                    "source_id": "openclaw-official-docs",
                    "source_kind": "official_docs",
                    "source_uri": "https://example.test/openclaw/docs",
                    "source_trust": 1.0,
                    "trust_tier": "high",
                    "source_trust_decision": decision.to_dict(),
                }
            )
        return runtime.store.append(
            RecordEnvelope.create(
                kind="memory",
                title=title,
                summary=f"OpenClaw recall pollution guard shared marker for {memory_type}.",
                scope=scope,
                source="openclaw.agent_end",
                content={
                    "text": f"OpenClaw recall pollution guard shared marker for {memory_type}.",
                    "memory_type": memory_type,
                },
                meta=meta,
            )
        )

    try:
        polluted = {
            add_memory("run_log", "OpenClaw run log").record_id,
            add_memory("audit_record", "OpenClaw audit record").record_id,
            add_memory("incident_report", "OpenClaw incident report").record_id,
            add_memory("evolution_artifact", "OpenClaw evolution artifact").record_id,
        }
        preserved = {
            add_memory("user_preference", "OpenClaw user preference").record_id,
            add_memory("system_rule", "OpenClaw system rule").record_id,
            add_memory("durable_fact", "OpenClaw durable fact").record_id,
            add_memory("external_knowledge", "OpenClaw external knowledge").record_id,
        }

        bundle = runtime.memory.recall(
            query="OpenClaw recall pollution guard shared marker",
            scope={"agent_id": "hongtu", "workspace_id": "embodied"},
            task_context={"task_type": "chat.reply"},
            limit=20,
        )

        ids = {item.record_id for item in bundle.items}
        assert preserved <= ids
        assert ids.isdisjoint(polluted)
        assert set(bundle.explanation["recall_filters"]["blocked_recall_lanes"]) >= {
            "run_log",
            "audit_record",
            "incident_report",
            "evolution_artifact",
        }
    finally:
        runtime.close()


def test_runtime_recall_blocks_low_trust_external_knowledge_by_default(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied"}
    marker = "OpenClaw low trust external knowledge marker"
    try:
        runtime.sources.add_source(
            {
                "source_id": "trusted-docs",
                "source_kind": "url",
                "title": "Trusted Docs",
                "uri": "https://example.test/docs",
                "metadata": {
                    "connector_id": "test.fixture",
                    "knowledge_source_kind": "official_docs",
                    "trust": 1.0,
                },
            }
        )
        trusted_decision = resolve_source_trust(
            {
                "source_id": "trusted-docs",
                "source_kind": "official_docs",
                "source_uri": "https://example.test/docs",
            },
            registry=runtime.sources,
            connector_id="test.fixture",
        )
        low_trust = runtime.memory.ingest(
            text=f"{marker} from an unreviewed blog should not be default recalled.",
            memory_type="external_knowledge",
            title="Low trust external knowledge",
            scope=scope,
            source="eimemory.knowledge_ingest",
            force_capture=True,
            meta={
                "source_kind": "blog",
                "source_uri": "https://example.test/blog",
                "source_trust": 0.5,
                "trust_tier": "low",
            },
        )
        trusted = runtime.memory.ingest(
            text=f"{marker} from official docs can be default recalled.",
            memory_type="external_knowledge",
            title="Trusted external knowledge",
            scope=scope,
            source="eimemory.knowledge_ingest",
            force_capture=True,
            meta={
                "source_id": "trusted-docs",
                "source_kind": "official_docs",
                "source_uri": "https://example.test/docs",
                "source_trust": 1.0,
                "trust_tier": "high",
                "source_trust_decision": trusted_decision.to_dict(),
            },
        )

        bundle = runtime.memory.recall(
            query=marker,
            scope=scope,
            task_context={"task_type": "chat.reply"},
            limit=10,
        )

        ids = {item.record_id for item in bundle.items}
        assert trusted.record_id in ids
        assert low_trust.record_id not in ids
        assert bundle.explanation["online_recall_gate"]["blocked_counts"]["external_knowledge_untrusted"] >= 1
    finally:
        runtime.close()


def test_runtime_recall_online_gate_blocks_rolled_back_rules_after_promotion_insert(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied"}
    scope_ref = ScopeRef.from_dict(scope)
    try:
        useful = runtime.memory.ingest(
            text="鸿哥沟通风格：极简、直接、讨厌废话，先给结论。",
            memory_type="preference",
            title="鸿哥沟通风格",
            scope=scope,
            force_capture=True,
        )
        stale_rule = runtime.store.append(
            RecordEnvelope.create(
                kind="rule",
                title="鸿哥沟通风格旧规则",
                summary="鸿哥沟通风格旧规则：这条规则已经被回滚，不应进入普通召回。",
                detail="鸿哥沟通风格旧规则：这条规则已经被回滚，不应进入普通召回。",
                content={"text": "鸿哥沟通风格旧规则：这条规则已经被回滚，不应进入普通召回。"},
                scope=scope_ref,
                status="active",
                meta={
                    "task_type": "operator_preference",
                    "post_promotion_watch": {"status": "rolled_back"},
                },
            )
        )

        bundle = runtime.memory.recall(
            query="鸿哥 沟通风格",
            scope=scope,
            task_context={"task_type": "operator_preference"},
            limit=5,
        )

        ids = {item.record_id for item in bundle.items}
        assert useful.record_id in ids
        assert stale_rule.record_id not in ids
        assert bundle.explanation["online_recall_gate"]["ok"] is True
        assert bundle.explanation["online_recall_gate"]["blocked_counts"]["stale_rule"] >= 1
        assert bundle.explanation["recall_filters"]["blocked_counts"]["stale_rule"] >= 1
    finally:
        runtime.close()


def test_runtime_recall_diagnostic_mode_can_include_operational_lanes(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied")
    quality = {
        "importance": 0.95,
        "confidence": 0.95,
        "freshness": 1.0,
        "reuse_potential": 0.95,
        "salience_score": 0.95,
        "quality_tier": "core",
        "capture_decision": "accept",
    }

    try:
        polluted = []
        for memory_type in ("run_log", "audit_record", "incident_report", "evolution_artifact"):
            polluted.append(
                runtime.store.append(
                    RecordEnvelope.create(
                        kind="memory",
                        title=f"OpenClaw {memory_type}",
                        summary=f"OpenClaw recall pollution guard diagnostic marker for {memory_type}.",
                        scope=scope,
                        source="openclaw.agent_end",
                        content={
                            "text": f"OpenClaw recall pollution guard diagnostic marker for {memory_type}.",
                            "memory_type": memory_type,
                        },
                        meta={"memory_type": memory_type, "quality": quality},
                    )
                )
            )
        polluted.append(
            runtime.store.append(
                RecordEnvelope.create(
                    kind="memory",
                    title="OpenClaw legacy audit",
                    summary="OpenClaw recall pollution guard diagnostic marker for legacy audit.",
                    scope=scope,
                    source="ei_bridge.openclaw_feishu",
                    content={
                        "text": "OpenClaw recall pollution guard diagnostic marker for legacy audit.",
                        "memory_type": "audit",
                    },
                    meta={"memory_type": "audit", "quality": quality},
                )
            )
        )

        bundle = runtime.memory.recall(
            query="debug OpenClaw recall pollution guard diagnostic marker",
            scope={"agent_id": "hongtu", "workspace_id": "embodied"},
            task_context={"task_type": "ops.diagnostic", "intent": "diagnostic"},
            limit=20,
        )

        assert {item.record_id for item in polluted} <= {item.record_id for item in bundle.items}
        assert "incident_report" not in bundle.explanation["recall_filters"].get("blocked_recall_lanes", [])
    finally:
        runtime.close()


def test_runtime_recall_diagnostic_mode_searches_evolution_artifact_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied")
    try:
        replay_result = runtime.store.append(
            RecordEnvelope.create(
                kind="replay_result",
                title="OpenClaw diagnostic replay artifact",
                summary="OpenClaw autonomy artifact marker for replay pass-rate analysis.",
                scope=scope,
                source="eimemory.skill_validation",
                content={"report": {"pass_rate": 0.8}},
                meta={"report_type": "skill_candidate_validation"},
            )
        )
        learning_eval = runtime.store.append(
            RecordEnvelope.create(
                kind="learning_eval",
                title="OpenClaw diagnostic learning eval",
                summary="OpenClaw autonomy artifact marker for learning eval analysis.",
                scope=scope,
                source="eimemory.learning_eval",
                content={"pass": True},
                meta={"ok": True},
            )
        )

        default_bundle = runtime.memory.recall(
            query="OpenClaw autonomy artifact marker",
            scope={"agent_id": "hongtu", "workspace_id": "embodied"},
            task_context={"task_type": "chat.reply"},
            limit=20,
        )
        diagnostic_bundle = runtime.memory.recall(
            query="debug OpenClaw autonomy artifact marker",
            scope={"agent_id": "hongtu", "workspace_id": "embodied"},
            task_context={"task_type": "ops.diagnostic", "intent": "diagnostic"},
            limit=20,
        )

        default_ids = {item.record_id for item in default_bundle.items}
        diagnostic_ids = {item.record_id for item in diagnostic_bundle.items}
        assert replay_result.record_id not in default_ids
        assert learning_eval.record_id not in default_ids
        assert replay_result.record_id in diagnostic_ids
        assert learning_eval.record_id in diagnostic_ids
    finally:
        runtime.close()


def test_runtime_recall_plain_report_request_does_not_enable_operational_lanes(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied")
    quality = {
        "importance": 0.95,
        "confidence": 0.95,
        "freshness": 1.0,
        "reuse_potential": 0.95,
        "salience_score": 0.95,
        "quality_tier": "core",
        "capture_decision": "accept",
    }
    try:
        incident = runtime.store.append(
            RecordEnvelope.create(
                kind="memory",
                title="Project report old incident",
                summary="Project status report marker from an old incident should stay hidden.",
                scope=scope,
                source="openclaw.agent_end",
                content={"text": "Project status report marker from an old incident should stay hidden.", "memory_type": "incident_report"},
                meta={"memory_type": "incident_report", "quality": quality},
            )
        )
        fact = runtime.store.append(
            RecordEnvelope.create(
                kind="memory",
                title="Project report fact",
                summary="Project status report marker from a durable fact should be visible.",
                scope=scope,
                source="openclaw.agent_end",
                content={"text": "Project status report marker from a durable fact should be visible.", "memory_type": "durable_fact"},
                meta={"memory_type": "durable_fact", "quality": quality},
            )
        )

        bundle = runtime.memory.recall(
            query="write a project status report marker",
            scope={"agent_id": "hongtu", "workspace_id": "embodied"},
            task_context={"task_type": "chat.reply"},
            limit=20,
        )

        ids = {item.record_id for item in bundle.items}
        assert fact.record_id in ids
        assert incident.record_id not in ids
        assert "incident_report" in bundle.explanation["recall_filters"]["blocked_recall_lanes"]
    finally:
        runtime.close()


def test_runtime_close_releases_sqlite_file_handle(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.memory.ingest(
        text="Release the sqlite handle",
        memory_type="fact",
        title="Close runtime",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )
    db_path = tmp_path / "state" / "eimemory.sqlite"

    runtime.close()
    db_path.unlink()

    assert not db_path.exists()


def test_runtime_collect_external_sources_can_use_fetcher(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.sources.add_source(
        {
            "source_kind": "url",
            "title": "ChatPaper",
            "uri": "https://www.chatpaper.ai/api/papers/arxiv?category=cs.AI&page=1&language=zh",
            "enabled": True,
        }
    )
    seen: list[str] = []

    def fake_fetch(url: str) -> str:
        seen.append(url)
        return '{"papers":[{"id":"2604.19740v1","title":"Paper","abstract":"Abstract","arxivUrl":"https://arxiv.org/abs/2604.19740v1"}]}'

    dry_run = runtime.collect_external_sources(fetch_text=fake_fetch)
    fetched = runtime.collect_external_sources(fetch=True, fetch_text=fake_fetch)

    assert dry_run["item_count"] == 1
    assert fetched["item_count"] == 1
    assert seen == [
        "https://www.chatpaper.ai/api/papers/arxiv?category=cs.AI&page=1&language=zh",
        "https://www.chatpaper.ai/api/papers/arxiv?category=cs.AI&page=1&language=zh",
    ]
    assert fetched["results"][0]["items"][0]["source_kind"] == "chatpaper_arxiv"


def test_runtime_collect_external_sources_can_persist_fetched_candidates_and_dedupe(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main", "workspace_id": "papers"}
    runtime.sources.add_source(
        {
            "source_kind": "url",
            "title": "ChatPaper",
            "uri": "https://www.chatpaper.ai/dashboard/arxiv/cs/AI?page=1&language=zh",
            "enabled": True,
        }
    )

    def fake_fetch(_url: str) -> str:
        return json.dumps(
            {
                "papers": [
                    {
                        "id": "2604.19740v1",
                        "title": "Externally Collected Paper",
                        "abstract": "A durable summary suitable for knowledge intake review.",
                        "arxivUrl": "https://arxiv.org/abs/2604.19740v1",
                        "publishedDate": "2026-04-20",
                    }
                ]
            }
        )

    first = runtime.collect_external_sources(fetch=True, persist=True, scope=scope, fetch_text=fake_fetch)
    second = runtime.collect_external_sources(fetch=True, persist=True, scope=scope, fetch_text=fake_fetch)
    records = runtime.store.list_records(kinds=["knowledge_candidate"], scope=scope, limit=10)

    assert first["persist"] is True
    assert first["written_count"] == 1
    assert first["skipped_existing_count"] == 0
    assert second["written_count"] == 0
    assert second["skipped_existing_count"] == 1
    assert len(records) == 1
    assert records[0].kind == "knowledge_candidate"
    assert records[0].status == "candidate"
    assert records[0].source == "eimemory.intake.collect"
    assert records[0].content["title"] == "Externally Collected Paper"
    assert records[0].content["item_url"] == "https://arxiv.org/abs/2604.19740v1"
    assert records[0].content["metadata"]["arxiv_id"] == "2604.19740v1"
    assert records[0].provenance["source_kind"] == "url"
    assert records[0].provenance["fetch_source"] == "chatpaper_arxiv"
    assert records[0].provenance["fingerprint"]


def test_runtime_collect_external_sources_strips_recursive_candidate_title_prefixes(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main", "workspace_id": "rss"}
    runtime.sources.add_source(
        {
            "source_kind": "rss",
            "title": "Recursive RSS",
            "uri": "https://example.test/rss.xml",
            "enabled": True,
        }
    )

    def fake_fetch(_url: str) -> str:
        return """<?xml version="1.0"?>
        <rss version="2.0"><channel><item>
          <title>News RSS candidate: Knowledge candidate: Graph memory update</title>
          <link>https://example.test/item</link>
          <description>Graph memory update summary.</description>
        </item></channel></rss>
        """

    report = runtime.collect_external_sources(fetch=True, persist=True, scope=scope, fetch_text=fake_fetch)
    records = runtime.store.list_records(kinds=["news"], scope=scope, limit=10)

    assert report["written_count"] == 1
    assert len(records) == 1
    assert records[0].title == "News item: Graph memory update"
    assert records[0].content["title"] == "Graph memory update"
    assert "Knowledge candidate:" not in records[0].title
    assert "News RSS candidate:" not in records[0].title


def test_runtime_context_manager_closes_store(tmp_path) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"

    with Runtime.create(root=tmp_path) as runtime:
        runtime.memory.ingest(
            text="Context manager closes runtime",
            memory_type="fact",
            title="Context runtime",
            scope={"agent_id": "main", "workspace_id": "repo-x"},
        )

    db_path.unlink()
    assert not db_path.exists()


def test_runtime_evolution_observe_feedback_and_policy(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    incident = runtime.evolution.observe(
        signal_type="incident",
        payload={
            "incident_type": "asr_low_value_input",
            "severity": "medium",
            "title": "Ignore punctuation-only ASR",
            "summary": "Punctuation-only ASR should not trigger reply",
        },
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
    )

    rule = runtime.evolution.store_rule(
        title="Ignore low-value ASR",
        summary="Avoid replies on punctuation-only ASR",
        task_type="brain.respond",
        retrieval_policy={
            "route_hint": "task_context_first",
            "open_unknown_on_low_confidence": True,
        },
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
        status="active",
    )

    feedback = runtime.evolution.feedback(
        target_ref={"kind": "incident", "record_id": incident.record_id},
        decision="accept",
        reason="Correctly captured the failure mode",
        reviewed_by="operator",
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
    )

    policy = runtime.evolution.get_active_policy(
        task_type="brain.respond",
        scope={"agent_id": "eibrain", "workspace_id": "robot"},
    )

    assert incident.kind == "incident"
    assert rule.kind == "rule"
    assert feedback.kind == "feedback"
    assert policy["retrieval_policy"]["route_hint"] == "task_context_first"


def test_runtime_recall_surfaces_active_rules_and_captures_unknowns(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.evolution.store_rule(
        title="Open unknowns on weak recall",
        summary="Track weak recall for later evolution",
        task_type="chat.reply",
        retrieval_policy={
            "route_hint": "task_context_first",
            "open_unknown_on_low_confidence": True,
        },
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        status="active",
    )

    bundle = runtime.memory.recall(
        query="nonexistent preference",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply", "goal": "answer user"},
        limit=5,
    )
    unknowns = runtime.store.list_records(
        kinds=["unknown"],
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        limit=10,
    )
    reflections = runtime.store.list_records(
        kinds=["reflection"],
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        limit=10,
    )

    assert bundle.rules
    assert bundle.rules[0].title == "Open unknowns on weak recall"
    assert bundle.confidence == 0.0
    assert bundle.explanation["active_policy"]["route_hint"] == "task_context_first"
    assert bundle.explanation["unknown_record_id"] == unknowns[0].record_id
    assert unknowns[0].kind == "unknown"
    assert reflections[0].kind == "reflection"


def test_runtime_recall_uses_response_policy_for_next_action_hint(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.memory.ingest(
        text="Respond briefly when operator context is active",
        memory_type="preference",
        title="Brief operator replies",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )
    runtime.evolution.store_rule(
        title="Guide reply style",
        summary="Prefer concise confirmation",
        task_type="chat.reply",
        retrieval_policy={"route_hint": "task_context_first"},
        response_policy={"next_action_hint": "reply with one concise sentence"},
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        status="active",
    )

    bundle = runtime.memory.recall(
        query="brief operator context",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    assert bundle.items
    assert bundle.next_action_hint == "reply with one concise sentence"


def test_runtime_recall_suppresses_digest_pages_for_default_preference_queries(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    digest = RecordEnvelope.create(
        kind="knowledge_page",
        title="Research digest 2026-05-14",
        summary="News digest: communication trends, product launches, and market updates.",
        detail="News digest with communication style coverage but no operator preference.",
        scope=scope,
        source="eimemory.knowledge.synthesis",
        content={"page_type": "digest"},
        meta={"page_type": "digest"},
    )
    preference = RecordEnvelope.create(
        kind="memory",
        title="Hongtu operator communication style",
        summary="鸿哥 沟通风格：直接、简洁，先给结论再给证据。",
        scope=scope,
        meta={
            "memory_type": "preference",
            "quality": {
                "importance": 0.9,
                "confidence": 0.9,
                "freshness": 1.0,
                "reuse_potential": 0.9,
                "salience_score": 0.9,
                "quality_tier": "core",
                "capture_decision": "accept",
            },
        },
    )
    runtime.store.append(digest)
    runtime.store.append(preference)

    bundle = runtime.memory.recall(
        query="鸿哥 沟通风格",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        limit=5,
    )

    titles = [item.title for item in bundle.items]
    assert titles[0] == "Hongtu operator communication style"
    assert "Research digest 2026-05-14" not in titles


def test_runtime_recall_does_not_answer_preference_query_with_digest_only(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    runtime.store.append(
        RecordEnvelope.create(
            kind="knowledge_page",
            title="Research digest 2026-05-14",
            summary="新闻简报：鸿哥 沟通风格 was mentioned in a broad market update.",
            detail="Digest and news material should not satisfy direct operator preference recall.",
            scope=scope,
            source="eimemory.knowledge.synthesis",
            content={"page_type": "digest"},
            meta={"page_type": "digest"},
        )
    )

    bundle = runtime.memory.recall(
        query="鸿哥 沟通风格",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        limit=5,
    )

    assert bundle.items == []


def test_runtime_recall_surfaces_matching_active_rule_for_operator_preference(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    rule = RecordEnvelope.create(
        kind="rule",
        title="Hongtu communication style rule",
        summary="鸿哥 沟通风格：极简、直接，讨厌废话；先给结论，少解释。",
        scope=scope,
        source="eimemory.rule_evolution_loop",
        status="active",
        meta={"task_type": "chat.reply"},
    )
    runtime.store.append(rule)

    bundle = runtime.memory.recall(
        query="鸿哥 沟通风格",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "cli.recall"},
        limit=5,
    )

    assert bundle.items
    assert bundle.items[0].record_id == rule.record_id
    assert bundle.items[0].kind == "rule"
    assert bundle.confidence > 0
    assert bundle.explanation["rule_recall_promoted_count"] == 1
    assert bundle.explanation["recall_view"]["items"][0]["record_id"] == rule.record_id


def test_runtime_recall_does_not_answer_preference_query_with_diagnostics(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    runtime.store.append(
        RecordEnvelope.create(
            kind="memory",
            title="OpenClaw agent outcome",
            summary=(
                "问题：eimemory recall '鸿哥 沟通风格' 返回了新闻简报，相关性明显不对。"
                "结论：recall ranking / filter 还不稳，需要继续诊断。"
            ),
            scope=scope,
            source="openclaw.agent_end",
            content={
                "text": (
                    "问题：eimemory recall '鸿哥 沟通风格' 返回了新闻简报，相关性明显不对。"
                    "结论：recall ranking / filter 还不稳，需要继续诊断。"
                ),
                "memory_type": "conversation",
            },
            meta={
                "memory_type": "conversation",
                "quality": {
                    "importance": 0.9,
                    "confidence": 0.9,
                    "freshness": 1.0,
                    "reuse_potential": 0.9,
                    "salience_score": 0.9,
                    "quality_tier": "core",
                    "capture_decision": "accept",
                },
            },
        )
    )

    bundle = runtime.memory.recall(
        query="鸿哥 沟通风格",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        limit=5,
    )

    assert bundle.items == []


def test_runtime_recall_prefers_explicit_communication_style_over_diagnostics(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    diagnostic = RecordEnvelope.create(
        kind="memory",
        title="OpenClaw agent outcome",
        summary="eimemory recall '鸿哥 沟通风格' 返回了新闻简报，相关性明显不对。",
        scope=scope,
        source="openclaw.agent_end",
        content={
            "text": "eimemory recall '鸿哥 沟通风格' 返回了新闻简报，相关性明显不对。",
            "memory_type": "conversation",
        },
        meta={
            "memory_type": "conversation",
            "quality": {
                "importance": 0.9,
                "confidence": 0.9,
                "freshness": 1.0,
                "reuse_potential": 0.9,
                "salience_score": 0.9,
                "quality_tier": "core",
                "capture_decision": "accept",
            },
        },
    )
    unrelated_conversation = RecordEnvelope.create(
        kind="memory",
        title="OpenClaw agent outcome",
        summary=(
            "鸿哥，看完文档，核心信息：MiniMax 有图片理解讨论。"
            "如果模型支持 vision，就不要生成额外图片摘要，直接传原图。"
        ),
        scope=scope,
        source="openclaw.agent_end",
        content={
            "text": (
                "鸿哥，看完文档，核心信息：MiniMax 有图片理解讨论。"
                "如果模型支持 vision，就不要生成额外图片摘要，直接传原图。"
            ),
            "memory_type": "conversation",
        },
        meta={
            "memory_type": "conversation",
            "quality": {
                "importance": 0.9,
                "confidence": 0.9,
                "freshness": 1.0,
                "reuse_potential": 0.9,
                "salience_score": 0.9,
                "quality_tier": "core",
                "capture_decision": "accept",
            },
        },
    )
    preference = runtime.memory.ingest(
        text="鸿哥 沟通风格：极简、直接，讨厌废话；先给结论，少解释。",
        memory_type="conversation",
        title="Hongtu operator communication style",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        source="openclaw.message_received",
        content={"memory_type": "conversation"},
        meta={"memory_type": "conversation"},
    )
    runtime.store.append(diagnostic)
    runtime.store.append(unrelated_conversation)

    bundle = runtime.memory.recall(
        query="鸿哥 沟通风格",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        limit=5,
    )

    assert preference.meta["memory_type"] == "preference"
    assert bundle.items[0].record_id == preference.record_id
    returned_ids = [item.record_id for item in bundle.items]
    assert diagnostic.record_id not in returned_ids
    assert unrelated_conversation.record_id not in returned_ids


def test_runtime_recall_returns_rule_evolution_report_for_report_query(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    report = runtime.run_rule_evolution(scope=scope, apply=True, persist_report=True)
    runtime.memory.ingest(
        text="Paper about generic autonomous memory evaluation and long-term recall.",
        memory_type="fact",
        title="Autonomous memory benchmark paper",
        scope=scope,
    )

    bundle = runtime.memory.recall(
        query=f"rule evolution report {report['persisted_record_id']}",
        scope=scope,
        limit=3,
    )

    assert bundle.items
    assert bundle.items[0].record_id == report["persisted_record_id"]
    assert bundle.items[0].kind == "reflection"
    assert bundle.items[0].meta["report_type"] == "rule_evolution"


def test_runtime_recall_prefers_rule_evolution_report_over_matching_paper(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    report = runtime.run_rule_evolution(scope=scope, apply=True, persist_report=True)
    runtime.store.append(
        RecordEnvelope.create(
            kind="knowledge_page",
            title="Paper: rule evolution report benchmark",
            summary="A generic paper page about rule evolution reports and memory evaluation.",
            scope=ScopeRef.from_dict(scope),
            content={"page_type": "paper"},
            meta={"page_type": "paper"},
        )
    )

    bundle = runtime.memory.recall(
        query="rule evolution report",
        scope=scope,
        limit=3,
    )

    assert bundle.items
    assert bundle.items[0].record_id == report["persisted_record_id"]
    assert bundle.items[0].kind == "reflection"


def test_runtime_recall_expands_graph_linked_memories(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
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

    bundle = runtime.memory.recall(
        query="brief operator reply",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    titles = [item.title for item in bundle.items]
    assert "Operator reply preference" in titles
    assert "Linked catalog entry" in titles
    assert bundle.explanation["recall_profile"] == "balanced"
    assert bundle.explanation["recall_profile_source"] == "default"
    assert bundle.explanation["graph_expanded"] >= 1


def test_runtime_recall_expands_graph_linked_memories_from_alias_scope(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    alias_scope = ScopeRef(agent_id="eibrain", workspace_id="robot", user_id="darrow")
    supporting = RecordEnvelope.create(
        kind="memory",
        title="Alias linked catalog entry",
        summary="Ceramic glaze catalog entry.",
        scope=alias_scope,
    )
    primary = RecordEnvelope.create(
        kind="memory",
        title="Alias operator reply preference",
        summary="Respond briefly to the operator from the shared robot scope",
        scope=alias_scope,
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

    bundle = runtime.memory.recall(
        query="brief operator shared robot reply",
        scope={"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    titles = [item.title for item in bundle.items]
    assert "Alias operator reply preference" in titles
    assert "Alias linked catalog entry" in titles
    assert bundle.explanation["graph_expanded"] >= 1


def test_runtime_recall_dedupes_recall_gaps_for_same_query(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.evolution.store_rule(
        title="Open unknowns on weak recall",
        summary="Track weak recall for later evolution",
        task_type="chat.reply",
        retrieval_policy={
            "route_hint": "task_context_first",
            "open_unknown_on_low_confidence": True,
        },
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        status="active",
    )

    for _ in range(2):
        runtime.memory.recall(
            query="missing operator preference",
            scope={"agent_id": "main", "workspace_id": "repo-x"},
            task_context={"task_type": "chat.reply", "goal": "answer user"},
            limit=5,
        )

    unknowns = runtime.store.list_records(
        kinds=["unknown"],
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        limit=10,
    )
    reflections = runtime.store.list_records(
        kinds=["reflection"],
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        limit=10,
    )

    assert len(unknowns) == 1
    assert len(reflections) == 1


def test_runtime_recall_reports_only_returned_graph_expansion(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    linked = RecordEnvelope.create(
        kind="memory",
        title="Linked detail",
        summary="This record is only reachable through a graph edge",
        scope=scope,
    )
    primary = RecordEnvelope.create(
        kind="memory",
        title="Primary detail",
        summary="This record matches directly",
        scope=scope,
        links=[LinkRef(relation="supports", target_kind="memory", target_id=linked.record_id)],
    )
    runtime.store.append(linked)
    runtime.store.append(primary)

    bundle = runtime.memory.recall(
        query="primary detail",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=1,
    )

    assert len(bundle.items) == 1
    assert bundle.items[0].title == "Primary detail"
    assert bundle.explanation["graph_expanded"] == 0


def test_runtime_recall_graph_expansion_respects_scope_isolation(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    tenant_a_scope = ScopeRef(tenant_id="tenant-a", agent_id="main", workspace_id="repo-x", user_id="alice")
    tenant_b_scope = ScopeRef(tenant_id="tenant-b", agent_id="main", workspace_id="repo-x", user_id="bob")
    linked_other_tenant = RecordEnvelope.create(
        kind="memory",
        title="Tenant B private linked memory",
        summary="Tenant B private preference should never leak",
        scope=tenant_b_scope,
    )
    primary = RecordEnvelope.create(
        kind="memory",
        title="Tenant A primary graph memory",
        summary="Tenant A primary graph recall target",
        scope=tenant_a_scope,
        links=[LinkRef(relation="supports", target_kind="memory", target_id=linked_other_tenant.record_id)],
    )
    runtime.store.append(linked_other_tenant)
    runtime.store.append(primary)

    bundle = runtime.memory.recall(
        query="tenant primary graph recall",
        scope={"tenant_id": "tenant-a", "agent_id": "main", "workspace_id": "repo-x", "user_id": "alice"},
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    assert [item.title for item in bundle.items] == ["Tenant A primary graph memory"]
    assert bundle.explanation["graph_expanded"] == 0


def test_runtime_recall_graph_expansion_allows_global_user_scope_links(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    user_scope = ScopeRef(tenant_id="tenant-a", agent_id="main", workspace_id="repo-x", user_id="alice")
    global_scope = ScopeRef(tenant_id="tenant-a", agent_id="main", workspace_id="repo-x", user_id="")
    linked_global = RecordEnvelope.create(
        kind="memory",
        title="Linked global entry",
        summary="Ceramic glaze catalog entry.",
        scope=global_scope,
    )
    primary = RecordEnvelope.create(
        kind="memory",
        title="Alice primary graph memory",
        summary="Alice primary graph recall target",
        scope=user_scope,
        links=[LinkRef(relation="supports", target_kind="memory", target_id=linked_global.record_id)],
    )
    runtime.store.append(linked_global)
    runtime.store.append(primary)

    bundle = runtime.memory.recall(
        query="alice primary graph recall",
        scope={"tenant_id": "tenant-a", "agent_id": "main", "workspace_id": "repo-x", "user_id": "alice"},
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    titles = [item.title for item in bundle.items]
    assert "Alice primary graph memory" in titles
    assert "Linked global entry" in titles
    assert bundle.explanation["graph_expanded"] == 1


def test_runtime_recall_precision_skips_graph_expansion(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    supporting = RecordEnvelope.create(
        kind="memory",
        title="Linked catalog entry",
        summary="Ceramic glaze catalog entry.",
        scope=scope,
    )
    primary = RecordEnvelope.create(
        kind="memory",
        title="Precision primary memory",
        summary="Precision recall should keep this focused.",
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

    bundle = runtime.memory.recall(
        query="precision focused recall",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply", "recall_profile": "precision"},
        limit=5,
    )

    assert [item.title for item in bundle.items] == ["Precision primary memory"]
    assert bundle.explanation["recall_profile"] == "precision"
    assert bundle.explanation["recall_profile_params"]["graph_policy"] == "disabled"
    assert bundle.explanation["graph_expanded"] == 0


def test_runtime_recall_exploratory_uses_task_context_retrieval_policy_profile(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    hop_two = RecordEnvelope.create(
        kind="memory",
        title="Linked hop two",
        summary="Ceramic glaze catalog entry.",
        scope=scope,
    )
    hop_one = RecordEnvelope.create(
        kind="memory",
        title="Linked hop one",
        summary="Ceramic glaze catalog entry.",
        scope=scope,
        links=[LinkRef(relation="supports", target_kind="memory", target_id=hop_two.record_id)],
    )
    primary = RecordEnvelope.create(
        kind="memory",
        title="Exploratory primary",
        summary="Primary project recall note.",
        scope=scope,
        links=[LinkRef(relation="supports", target_kind="memory", target_id=hop_one.record_id)],
    )
    runtime.store.append(hop_two)
    runtime.store.append(hop_one)
    runtime.store.append(primary)

    balanced_bundle = runtime.memory.recall(
        query="exploratory primary widen",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=3,
    )
    exploratory_bundle = runtime.memory.recall(
        query="exploratory primary widen",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={
            "task_type": "chat.reply",
            "retrieval_policy": {"recall_profile": "exploratory"},
        },
        limit=3,
    )

    assert [item.title for item in balanced_bundle.items] == ["Exploratory primary", "Linked hop one"]
    assert [item.title for item in exploratory_bundle.items] == [
        "Exploratory primary",
        "Linked hop one",
        "Linked hop two",
    ]
    assert exploratory_bundle.explanation["recall_profile"] == "exploratory"
    assert exploratory_bundle.explanation["recall_profile_source"] == "task_context.retrieval_policy"
    assert exploratory_bundle.explanation["recall_profile_params"]["graph_policy"] == "two_hop"


def test_runtime_recall_graph_expansion_survives_direct_hit_truncation(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    linked = RecordEnvelope.create(
        kind="memory",
        title="Linked catalog entry",
        summary="Ceramic glaze catalog entry.",
        scope=scope,
    )
    primary = RecordEnvelope.create(
        kind="memory",
        title="Primary graph detail",
        summary="Primary note for graph expansion testing.",
        scope=scope,
        links=[LinkRef(relation="supports", target_kind="memory", target_id=linked.record_id)],
    )
    runtime.store.append(linked)
    runtime.store.append(primary)

    bundle = runtime.memory.recall(
        query="primary graph detail",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=2,
    )

    assert [item.title for item in bundle.items] == ["Primary graph detail", "Linked catalog entry"]
    assert bundle.explanation["graph_expanded"] == 1


def test_runtime_recall_view_only_reports_selected_items(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    runtime.store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Selected alpha memory",
            summary="Alpha recall should be selected first.",
            scope=scope,
        )
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Overflow alpha memory",
            summary="Alpha recall should not appear when limit is one.",
            scope=scope,
        )
    )

    bundle = runtime.memory.recall(
        query="alpha recall",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=1,
    )

    returned_ids = {item.record_id for item in bundle.items}
    view_ids = {item["record_id"] for item in bundle.explanation["recall_view"]["items"]}
    assert len(bundle.items) == 1
    assert view_ids == returned_ids


def test_runtime_recall_does_not_graph_expand_rejected_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    rejected = RecordEnvelope.create(
        kind="memory",
        title="Rejected linked detail",
        summary="This rejected detail should never be returned.",
        scope=scope,
        status="rejected",
        meta={"quality": {"capture_decision": "reject", "salience_score": 0.0}},
    )
    primary = RecordEnvelope.create(
        kind="memory",
        title="Primary graph memory",
        summary="Primary record matches graph recall.",
        scope=scope,
        links=[LinkRef(relation="supports", target_kind="memory", target_id=rejected.record_id)],
    )
    runtime.store.append(rejected)
    runtime.store.append(primary)

    bundle = runtime.memory.recall(
        query="primary graph recall",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    assert [item.title for item in bundle.items] == ["Primary graph memory"]
    assert bundle.explanation["quality_summary"]["rejected_returned"] == 0


def test_runtime_recall_uses_vector_assist_for_hybrid_semantic_hits(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.memory.ingest(
        text="Keep responses succinct and compact for handheld voice interactions.",
        memory_type="preference",
        title="Compact mobile replies",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )

    bundle = runtime.memory.recall(
        query="short mobile voice replies",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    assert bundle.items
    assert bundle.items[0].title == "Compact mobile replies"
    assert bundle.explanation["retrieval_mode"] == "recall_index_hybrid"
    assert bundle.explanation["vector_hits"] >= 1


def test_cli_quality_stats_prints_default_scope_report(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))

    assert cli_main(
        [
            "ingest",
            "Decision: eimemory should keep OpenClaw memories scoped by tenant and user.",
            "--title",
            "OpenClaw scope decision",
            "--memory-type",
            "decision",
        ]
    ) == 0
    capsys.readouterr()

    assert cli_main(["quality", "stats"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["memory_count"] == 1
    high_quality_count = report["quality_distribution"]["confirmed"] + report["quality_distribution"]["core"]
    assert high_quality_count == 1
    assert report["by_memory_type"]["decision"] == 1


def test_cli_quality_repair_prints_dry_run_and_apply_reports(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    runtime = Runtime.create(root=tmp_path / "runtime")
    legacy = RecordEnvelope.create(
        kind="memory",
        title="Legacy CLI memory",
        summary="Decision: CLI repair should report old memory quality backfills.",
        content={"text": "Decision: CLI repair should report old memory quality backfills.", "memory_type": "decision"},
        scope=ScopeRef(agent_id="main", workspace_id=""),
        source="legacy",
        meta={"memory_type": "decision"},
    )
    legacy.meta.pop("quality", None)
    runtime.store.append(legacy)
    runtime.close()

    assert cli_main(["quality", "repair"]) == 0
    dry_run = json.loads(capsys.readouterr().out)

    assert dry_run["applied"] is False
    assert dry_run["backfilled_count"] == 1

    assert cli_main(["quality", "repair", "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)

    assert applied["applied"] is True
    assert applied["backfilled_count"] == 1
    assert applied["actions"][0]["action"] == "backfill_quality"


def test_runtime_recall_explains_quality_aware_scoring(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    high_quality = RecordEnvelope.create(
        kind="memory",
        title="Core OpenClaw memory rule",
        summary="OpenClaw memory recall must prioritize verified durable project preferences.",
        scope=scope,
        meta={
            "quality": {
                "importance": 0.96,
                "confidence": 0.94,
                "freshness": 1.0,
                "reuse_potential": 0.96,
                "salience_score": 0.95,
                "quality_tier": "core",
                "capture_decision": "accept",
            }
        },
    )
    high_quality.record_id = "core"
    low_quality = RecordEnvelope.create(
        kind="memory",
        title="Weak OpenClaw note",
        summary="OpenClaw memory recall project note.",
        scope=scope,
        meta={
            "quality": {
                "importance": 0.1,
                "confidence": 0.2,
                "freshness": 1.0,
                "reuse_potential": 0.1,
                "salience_score": 0.16,
                "quality_tier": "candidate",
                "capture_decision": "accept",
            }
        },
    )
    low_quality.record_id = "weak"
    runtime.store.append(high_quality)
    runtime.store.append(low_quality)

    bundle = runtime.memory.recall(
        query="openclaw memory recall project",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=2,
    )

    assert [item.title for item in bundle.items] == [
        "Core OpenClaw memory rule",
        "Weak OpenClaw note",
    ]
    scoring = bundle.explanation["scoring"]
    assert scoring[0]["record_id"] == high_quality.record_id
    assert scoring[0]["quality_tier"] == "core"
    assert scoring[0]["quality_score"] > scoring[1]["quality_score"]
    assert scoring[0]["scoring_version"] == "memory_score.v1"
    assert scoring[0]["memory_score"]["schema_version"] == "memory_score.v1"
    assert "relevance" in scoring[0]["components"]
    assert scoring[0]["provenance"]["activity"] == "memory.recall_score"
    assert "lexical_score" in scoring[0]
    assert "vector_score" in scoring[0]


def test_runtime_recall_explains_source_composition_and_selected_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.memory.ingest(
        text="OpenClaw runtime memory should cite selected knowledge records during recall.",
        memory_type="fact",
        title="Recall telemetry memory",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )

    bundle = runtime.memory.recall(
        query="selected knowledge records during recall",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    assert bundle.explanation["source_composition"]["by_kind"]["memory"] == 1
    assert bundle.explanation["source_composition"]["memory_count"] == 1
    assert bundle.explanation["selected_records"][0]["record_id"] == bundle.items[0].record_id
    assert bundle.explanation["selected_records"][0]["kind"] == "memory"



def test_runtime_recall_returns_empty_bundle_for_blank_query(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.evolution.store_rule(
        title="Open unknowns on weak recall",
        summary="Track weak recall for later evolution",
        task_type="chat.reply",
        retrieval_policy={
            "route_hint": "task_context_first",
            "open_unknown_on_low_confidence": True,
        },
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        status="active",
    )

    bundle = runtime.memory.recall(
        query="   ",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply", "goal": "answer user"},
        limit=0,
    )

    assert bundle.items == []
    assert bundle.reflections == []
    assert bundle.confidence == 0.0
    assert bundle.explanation["invalid_request"] == "empty_query"
    unknowns = runtime.store.list_records(
        kinds=["unknown"],
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        limit=10,
    )
    assert unknowns == []


def test_configured_production_recall_gate_prewarms_before_measured_run(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path)
    dataset = {
        "name": "generated-smoke",
        "scope": {"agent_id": "main", "workspace_id": "repo-x"},
        "cases": [{"case_id": "one", "query": "alpha"}],
    }
    monkeypatch.setattr(
        "eimemory.scheduler.jobs._production_recall_dataset",
        lambda _runtime, *, scope: (dataset, True, "generated_records", ""),
    )
    calls = []

    def fake_run(_dataset, *, seed, scope, persist_report):
        calls.append({"seed": seed, "scope": scope, "persist_report": persist_report})
        if not persist_report:
            return {
                "ok": True,
                "gate_ok": False,
                "sample_count": 1,
                "latency_ms_p95": 2400.0,
            }
        return {
            "ok": True,
            "gate_ok": True,
            "accepted": False,
            "gate_status": "diagnostic",
            "sample_count": 1,
            "latency_ms_p95": 1200.0,
        }

    runtime.run_production_recall_eval = fake_run
    try:
        report = runtime.run_configured_production_recall_gate(
            scope={"agent_id": "main", "workspace_id": "repo-x"}
        )
    finally:
        runtime.close()

    assert [call["persist_report"] for call in calls] == [False, True]
    assert report["gate_ok"] is True
    assert report["latency_ms_p95"] == 1200.0
    assert report["preload"] == {
        "ok": True,
        "gate_ok": False,
        "sample_count": 1,
        "latency_ms_p95": 2400.0,
    }
