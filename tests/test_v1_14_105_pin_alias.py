from eimemory.governance.release import evidence_contract as ec
from eimemory.models.records import ScopeRef
import eimemory.identity as ident

def _patch(monkeypatch):
    monkeypatch.setattr(ident, "canonical_agent_id", lambda a: "newa" if a in ("olda", "newa") else a)

def test_legacy_pin_applies_to_canonical_agent(monkeypatch):
    _patch(monkeypatch)
    pin = {"tenant_id": "t", "agent_id": "olda", "workspace_id": "w::c", "user_id": "u"}
    assert ec._pin_scope_matches(pin, ScopeRef(tenant_id="t", agent_id="newa", workspace_id="w::c", user_id="u"))

def test_other_dimensions_and_unrelated_agents_rejected(monkeypatch):
    _patch(monkeypatch)
    pin = {"tenant_id": "t", "agent_id": "olda", "workspace_id": "w::c", "user_id": "u"}
    assert not ec._pin_scope_matches(pin, ScopeRef(tenant_id="t", agent_id="newa", workspace_id="w", user_id="u"))
    assert not ec._pin_scope_matches(pin, ScopeRef(tenant_id="t", agent_id="newa", workspace_id="w::c", user_id="x"))
    assert not ec._pin_scope_matches(pin, ScopeRef(tenant_id="t", agent_id="other", workspace_id="w::c", user_id="u"))
    canon = {"tenant_id": "t", "agent_id": "newa", "workspace_id": "w::c", "user_id": "u"}
    # a canonical pin never extends to the legacy id
    assert not ec._pin_scope_matches(canon, ScopeRef(tenant_id="t", agent_id="olda", workspace_id="w::c", user_id="u"))
