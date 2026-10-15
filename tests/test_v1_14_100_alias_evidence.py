from eimemory.evaluation import production_query_dataset as pq
from eimemory.models.records import ScopeRef


def _s(agent, user="u"):
    return ScopeRef(tenant_id="t", agent_id=agent, workspace_id="w::channel::hermes", user_id=user)


def test_alias_equivalence_uses_configured_legacy_ids(monkeypatch):
    import eimemory.identity as identity
    monkeypatch.setattr(identity, "HONGTU_AGENT_ID", "new-agent")
    monkeypatch.setattr(identity, "legacy_agent_ids", lambda: ("old-agent",))
    assert pq._alias_equivalent_scope(_s("old-agent"), _s("new-agent"))
    assert pq._alias_equivalent_scope(_s("new-agent"), _s("new-agent"))
    assert not pq._alias_equivalent_scope(_s("other"), _s("new-agent"))
    assert not pq._alias_equivalent_scope(_s("old-agent", "x"), _s("new-agent"))
    assert pq._decision_agent_ids("new-agent") == ["new-agent", "old-agent"]
    assert pq._decision_agent_ids("other") == ["other"]


def test_no_alias_without_configuration(monkeypatch):
    import eimemory.identity as identity
    monkeypatch.setattr(identity, "legacy_agent_ids", lambda: ())
    assert not pq._alias_equivalent_scope(_s("old-agent"), _s("new-agent"))


def test_accepted_validator_binds_pending_payload_through_alias():
    import inspect
    src = inspect.getsource(pq.accepted_production_query_validation_error)
    assert '_alias_equivalent_scope(pending_payload["scope"], exact_scope)' in src
