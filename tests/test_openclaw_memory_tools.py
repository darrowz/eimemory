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
    assert status["code_sandbox"]["code_patch_proposal"] is True


def test_openclaw_tools_latest_audit_uses_indexed_session_lookup(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    tools = OpenClawMemoryTools(runtime)
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    runtime.store.append(
        RecordEnvelope.create(
            kind="recall_view",
            title="Session audit",
            source="openclaw.before_prompt_build",
            scope=scope,
            content={"session_id": "sess-indexed-tool"},
            meta={"session_id": "sess-indexed-tool"},
        )
    )
    def reject_audit_scan(*_args, **_kwargs):
        raise AssertionError("OpenClaw tool scanned record pages instead of using the indexed audit lookup")

    monkeypatch.setattr(runtime.store, "list_records", reject_audit_scan)
    try:
        audit = tools._latest_recall_audit(
            session_id="sess-indexed-tool",
            scope={
                "tenant_id": scope.tenant_id,
                "agent_id": scope.agent_id,
                "workspace_id": scope.workspace_id,
                "user_id": scope.user_id,
            },
        )
    finally:
        runtime.close()

    assert audit is not None
    assert audit.content["session_id"] == "sess-indexed-tool"


def test_openclaw_tools_latest_audit_rejects_alias_scope_policy_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    tools = OpenClawMemoryTools(runtime)
    runtime.store.append(
        RecordEnvelope.create(
            kind="recall_view",
            title="Legacy alias audit",
            source="openclaw.before_prompt_build",
            scope=ScopeRef(agent_id="main", workspace_id="repo-x", user_id=""),
            content={"session_id": "sess-alias-tool"},
            meta={"session_id": "sess-alias-tool"},
        )
    )
    try:
        audit = tools._latest_recall_audit(
            session_id="sess-alias-tool",
            scope={"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        )
    finally:
        runtime.close()

    assert audit is None


def test_promotion_watch_summary_excludes_capability_score_history(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    tools = OpenClawMemoryTools(runtime)
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    try:
        runtime.store.append(
            RecordEnvelope.create(
                kind="capability_score",
                title="Not a promotion watch",
                scope=scope,
                content={"evidence_items": [{"blob": "x" * 500_000}]},
            )
        )
        original = runtime.store.list_records

        def reject_capability_score_load(*args, **kwargs):
            kinds = kwargs.get("kinds")
            if kinds is None and args:
                kinds = args[0]
            if "capability_score" in list(kinds or []):
                raise AssertionError("promotion watch loaded capability_score payloads")
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime.store, "list_records", reject_capability_score_load)
        summary = tools._summarize_promotion_watch(scope)
    finally:
        runtime.close()

    assert summary["request_count"] == 0


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


def test_openclaw_memory_code_patch_propose_defaults_to_safe_proposal(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    tools = OpenClawMemoryTools(runtime)
    captured: dict[str, object] = {}

    def fake_propose_code_patch(*, incident, scope, create_worktree=False, persist_report=False, **_kwargs) -> dict:
        captured["incident"] = incident
        captured["scope"] = scope
        captured["create_worktree"] = create_worktree
        captured["persist_report"] = persist_report
        return {
            "ok": True,
            "report_type": "code_patch_proposal",
            "proposal_status": "sandbox_ready",
            "sandbox_plan": {"worktree_created": bool(create_worktree)},
        }

    monkeypatch.setattr(runtime, "propose_code_patch", fake_propose_code_patch)

    report = tools.memory_code_patch_propose(
        incident={"incident_type": "TypeError", "summary": "Runtime crash"},
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )

    assert report["ok"] is True
    assert report["report_type"] == "code_patch_proposal"
    assert report["proposal_status"] == "sandbox_ready"
    assert captured["create_worktree"] is False
    assert captured["persist_report"] is False
    assert captured["incident"] == {"incident_type": "TypeError", "summary": "Runtime crash"}
