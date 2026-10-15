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
