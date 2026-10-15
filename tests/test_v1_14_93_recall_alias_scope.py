from eimemory.models.records import ScopeRef
from eimemory.scheduler import jobs


def test_legacy_agent_scope_counts_as_canonical(monkeypatch):
    import eimemory.identity as identity

    monkeypatch.setattr(identity, "HONGTU_AGENT_ID", "new-agent")
    monkeypatch.setattr(identity, "legacy_agent_ids", lambda: ("old-agent",))
    target = ScopeRef(tenant_id="t", agent_id="new-agent", workspace_id="w", user_id="u")
    legacy = ScopeRef(tenant_id="t", agent_id="old-agent", workspace_id="w", user_id="u")
    other = ScopeRef(tenant_id="t", agent_id="other", workspace_id="w", user_id="u")
    assert jobs._same_canonical_scope(legacy, target)
    assert jobs._same_canonical_scope(target, target)
    assert not jobs._same_canonical_scope(other, target)
    assert not jobs._same_canonical_scope(ScopeRef(tenant_id="t", agent_id="old-agent", workspace_id="x", user_id="u"), target)


def test_known_items_stay_exact_scope_under_rename(monkeypatch):
    import eimemory.identity as identity
    from types import SimpleNamespace

    monkeypatch.setattr(identity, "HONGTU_AGENT_ID", "new-agent")
    monkeypatch.setattr(identity, "legacy_agent_ids", lambda: ("old-agent",))
    calls = []

    def list_records(**kw):
        calls.append(kw["offset"])
        return []

    runtime = SimpleNamespace(store=SimpleNamespace(list_records=list_records))
    scope = {"tenant_id": "t", "agent_id": "new-agent", "workspace_id": "w", "user_id": "u"}
    out = jobs._production_recall_smoke_dataset(runtime, scope=scope, stable_before="2099-01-01T00:00:00Z")
    assert out["cases"] == []
    import inspect
    src = inspect.getsource(jobs._production_recall_smoke_dataset)
    assert "record.scope != target_scope" in src and "scan_limit = 40_000" in src
