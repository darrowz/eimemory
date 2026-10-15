from dataclasses import replace

import pytest

from eimemory.capabilities import profiles as mod
from eimemory.models.records import ScopeRef


def test_profile_resolve_falls_back_to_legacy_partition(monkeypatch):
    import eimemory.identity as identity

    monkeypatch.setattr(identity, "HONGTU_AGENT_ID", "new-agent")
    monkeypatch.setattr(identity, "legacy_agent_ids", lambda: ("old-agent",))
    seen = []

    def fake_resolve(self, profile_key, *, runtime_scope, capability_scope, **kw):
        seen.append(runtime_scope.agent_id)
        if runtime_scope.agent_id != "old-agent":
            raise mod.CapabilityProfileError("active profile 'p' is not available in this exact scope")
        return {"ok": True}

    original = mod.CapabilityProfiles.resolve
    monkeypatch.setattr(mod.CapabilityProfiles, "resolve", fake_resolve)
    mod._wrap_profile_scope("resolve")
    scope = ScopeRef(tenant_id="t", agent_id="new-agent", workspace_id="w", user_id="u")
    assert mod.CapabilityProfiles.resolve(object(), "p", runtime_scope=scope, capability_scope="global") == {"ok": True}
    assert seen == ["new-agent", "old-agent"]
    seen.clear()
    other = replace(scope, agent_id="someone-else")
    with pytest.raises(mod.CapabilityProfileError):
        mod.CapabilityProfiles.resolve(object(), "p", runtime_scope=other, capability_scope="global")
    assert seen == ["someone-else"]
