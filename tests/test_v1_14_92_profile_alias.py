from dataclasses import replace
from contextlib import closing

import pytest

from eimemory.capabilities import profiles as mod
from eimemory.models.records import ScopeRef


def test_profile_resolve_falls_back_to_legacy_partition(monkeypatch, tmp_path):
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
    # Registry selection is now verified against actual entities. A bare
    # object cannot represent the store-backed Profile resolver contract.
    from eimemory.capabilities.registry import CapabilityRegistry
    from eimemory.storage.runtime_store import RuntimeStore
    from test_capability_state_projector import _definition
    with closing(RuntimeStore(tmp_path)) as store:
        CapabilityRegistry(store).register_definition(_definition("synthetic.alias"),
            runtime_scope=replace(scope, agent_id="old-agent"))
        resolver = mod.CapabilityProfiles(store)
        assert resolver.resolve("p", runtime_scope=scope, capability_scope="global") == {"ok": True}
        assert seen == ["new-agent", "old-agent"]
        seen.clear()
        other = replace(scope, agent_id="someone-else")
        with pytest.raises(mod.CapabilityProfileError):
            resolver.resolve("p", runtime_scope=other, capability_scope="global")
        assert seen == ["someone-else"]
