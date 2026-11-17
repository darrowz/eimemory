"""The reuse opt-in is a read-only identity check, never owner acquisition."""
from types import ModuleType
import sys

import pytest

from eimemory.adapters.hermes.provider_registry import get_hermes_provider
from eimemory.contracts import hermes_topology
from integrations.hermes.eimemory import EIMemoryProvider


@pytest.fixture
def owner(tmp_path, monkeypatch):
    class Client:
        def call_or_bypass(self, method, params):
            return {"ok": True, "result": {"ok": True, "bypassed": True}}

    context = dict(hermes_home=str(tmp_path / "profile"), user_id="synthetic-user",
                   agent_identity="default", agent_workspace="hermes")
    secrets = {}
    for name, values in {
        "hermes_constants": {"get_hermes_home": lambda: context["hermes_home"]},
        "hermes_cli.profiles": {"get_active_profile_name": lambda: "default"},
        "agent.secret_scope": {"get_secret": lambda key, default=None: secrets.get(key, default),
                               "current_secret_scope_home": lambda: context["hermes_home"]},
        "gateway.session_context": {"get_session_env": lambda *a: context["user_id"]},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(hermes_topology, "load_topology", lambda: None)
    provider = EIMemoryProvider(client=Client())
    provider.initialize("session", **context)
    yield provider, context, secrets
    provider.shutdown()


def test_exact_reuse_does_not_mutate_owner(owner):
    provider, context, _ = owner
    provider.bind_verified_host_turn(session_id="session", turn_id="verified-turn")
    snapshot = provider._outbox, dict(provider._host_binding), list(provider._verified_host_turns)
    assert provider.can_reuse_for_session(" session ", **context) is True
    assert (provider._outbox, provider._host_binding, list(provider._verified_host_turns)) == snapshot
    assert get_hermes_provider("session", hermes_home=context["hermes_home"]) is provider


@pytest.mark.parametrize("state", ["_initialized", "_active", "_closing", "_closed"])
def test_only_fully_live_initialized_provider_can_opt_in(owner, monkeypatch, state):
    provider, context, _ = owner
    monkeypatch.setattr(provider, state, state in {"_closing", "_closed"}, raising=False)
    assert provider.can_reuse_for_session("session", **context) is False
    # Restore before fixture shutdown so the test still drains its own worker.
    monkeypatch.setattr(provider, state, state in {"_initialized", "_active"})


@pytest.mark.parametrize("change", [
    {"user_id": "another-user"}, {"agent_identity": "another-agent"},
    {"agent_workspace": "another-workspace"}, {"agent_context": "cron"},
    {"expected_source_ids": ["another-source"]}, {"expected_scope": {"tenant_id": "another"}},
])
def test_changed_identity_never_opts_in(owner, change):
    provider, context, _ = owner
    assert provider.can_reuse_for_session("session", **{**context, **change}) is False


def test_other_session_cannot_reuse_owner(owner):
    provider, context, _ = owner
    assert provider.can_reuse_for_session("other-session", **context) is False


def test_source_change_cannot_reuse_owner(owner):
    provider, context, secrets = owner
    secrets["EIMEMORY_SOURCE_IDS"] = "another-source"
    assert provider.can_reuse_for_session("session", **context) is False


@pytest.mark.parametrize("result", [1, "true", "raises"])
def test_reuse_check_fails_closed_for_non_boolean_or_exception(owner, monkeypatch, result):
    provider, context, _ = owner
    def check(*a, **kw):
        if result == "raises":
            raise LookupError("synthetic check failure")
        return result
    monkeypatch.setattr(provider, "_same_initialization", check)
    assert provider.can_reuse_for_session("session", **context) is False


def test_uninitialized_candidate_cannot_acquire_live_owner(owner):
    provider, context, _ = owner
    other = EIMemoryProvider(client=provider._client)
    assert other.can_reuse_for_session("session", **context) is False
    assert get_hermes_provider("session", hermes_home=context["hermes_home"]) is provider
