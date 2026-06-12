from __future__ import annotations

from eimemory.adapters.openclaw.tools import OpenClawMemoryTools
from eimemory.api.runtime import Runtime
from eimemory.intake.registry import SourceRegistry
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_openclaw_memory_learn_status_reports_learning_state(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    tools = OpenClawMemoryTools(runtime)

    SourceRegistry(runtime.sources.path).add_source(
        {
            "source_kind": "rss",
            "title": "AI Daily",
            "uri": "https://example.test/rss.xml",
            "tags": ["news"],
            "enabled": True,
        }
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="capability_candidate",
            title="Test capability candidate",
            summary="Test capability candidate summary",
            scope=ScopeRef.from_dict({"agent_id": "main", "workspace_id": "repo-x"}),
            source="test.autonomy",
        )
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Watch me",
            summary="Promotion request watch",
            scope=ScopeRef.from_dict({"agent_id": "main", "workspace_id": "repo-x"}),
            content={"post_promotion_status": "observing"},
            source="test.autonomy",
        )
    )

    status = tools.memory_learn_status(scope={"agent_id": "main", "workspace_id": "repo-x"})

    assert status["ok"] is True
    assert status["knowledge_intake"]["source_count"] == 1
    assert status["source_registry"]["source_count"] == 1
    assert status["source_registry"]["by_kind"]["rss"] == 1
    assert status["external_collection"]["available"] is True
    assert status["capability_candidates"]["capability_candidate_count"] == 1
    assert status["capability_candidates"]["candidate_count"] == 1
    assert status["promotion_watch"]["request_count"] >= 1
    assert status["code_sandbox"]["available"] is True


def test_openclaw_memory_run_autonomy_dry_run_defaults_to_safe_mode(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    tools = OpenClawMemoryTools(runtime)
    captured: dict[str, object] = {}

    def fake_run_autonomy_cycle(*, scope, apply=False, dry_run=True, max_goals=3, policy=None, **_kwargs) -> dict:
        captured["scope"] = scope
        captured["apply"] = apply
        captured["dry_run"] = dry_run
        captured["max_goals"] = max_goals
        captured["policy"] = policy
        return {
            "ok": True,
            "loop_id": "openclaw-test",
            "goal_count": max_goals,
            "promotions": [],
        }

    monkeypatch.setattr(runtime, "run_autonomy_cycle", fake_run_autonomy_cycle)

    report = tools.memory_run_autonomy(scope={"agent_id": "main", "workspace_id": "repo-x"})

    assert report["ok"] is True
    assert captured["apply"] is False
    assert captured["dry_run"] is True
    assert captured["max_goals"] == 1
    assert isinstance(captured["policy"], dict)
    assert captured["policy"]["max_auto_promotions"] == 0
    assert report["requested"]["max_goals"] == 1
    assert report["requested"]["max_promotions"] == 0


def test_openclaw_memory_source_scan_defaults_no_persist_no_fetch(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    tools = OpenClawMemoryTools(runtime)
    captured: dict[str, object] = {}

    def fake_collect_external_sources(*, scope, source_kind=None, limit=None, persist=False, fetch=False, **_kwargs) -> dict:
        captured["persist"] = persist
        captured["fetch"] = fetch
        captured["source_kind"] = source_kind
        captured["limit"] = limit
        captured["scope"] = scope
        return {
            "ok": True,
            "source_count": 0,
            "item_count": 0,
        }

    monkeypatch.setattr(runtime, "collect_external_sources", fake_collect_external_sources)

    report = tools.memory_source_scan(scope={"agent_id": "main", "workspace_id": "repo-x"}, source_kind="rss", limit=3)

    assert report["ok"] is True
    assert captured["persist"] is False
    assert captured["fetch"] is False
    assert captured["source_kind"] == "rss"
    assert captured["limit"] == 3


def test_openclaw_memory_skill_status_is_compatible_without_skill_candidates(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    tools = OpenClawMemoryTools(runtime)

    runtime.store.append(
        RecordEnvelope.create(
            kind="capability_candidate",
            title="Capability from test",
            summary="Capability candidate in test scope",
            scope=ScopeRef.from_dict({"agent_id": "main", "workspace_id": "repo-x"}),
            source="test.autonomy",
        )
    )

    report = tools.memory_skill_status(scope={"agent_id": "main", "workspace_id": "repo-x"}, limit=10)

    assert report["ok"] is True
    assert report["capability_candidate_count"] == 1
    assert report["skill_candidate_count"] == 0
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["kind"] == "capability_candidate"
