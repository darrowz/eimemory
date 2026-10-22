"""Deployment replay uses native routing while retaining its expected scope."""
from copy import deepcopy
import os

import pytest

from deploy.check_hermes_recall_identity import probe_hermes_recall_identity
from deploy.verify_hermes_integration import _initialize_deployment_replay
from eimemory.adapters.hermes import profile_context
from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.contracts import hermes_topology
from test_hermes_adapter import FakeClient


SCOPE = dict(tenant_id="audit", agent_id="owner-agent", workspace_id="embodied", user_id="local-owner")


@pytest.fixture
def registered_replay(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    home_value = str(home)
    if os.name == "nt":
        # The topology contract uses POSIX absolute paths. On Windows a rooted
        # path resolves on the current drive; keep that drive inside tmp_path.
        monkeypatch.chdir(tmp_path)
        home_value = "/" + home.as_posix().split(":", 1)[1].lstrip("/")
    canonical = dict(SCOPE, workspace_id="embodied::channel::hermes")
    body = dict(schema="eimemory.hermes_topology.v1", mode="single_native_gateway",
        runtime_uid=1001, runtime_gid=1001, owner_unit="hermes-gateway.service",
        runtime_units=["eimemory-rpc.service", "hermes-gateway.service"], retired_gateways=[],
        profiles=[dict(profile_name="default", hermes_home=home_value, agent_id=SCOPE["agent_id"],
            identity_status="bound", capture_scopes=[dict(scope=dict(canonical, user_id="business-user"),
                channel="hermes", source_ids=["hermes"])], operator_scope=canonical)])
    snapshot = dict(body, topology_digest=hermes_topology.topology_digest(body))
    for loader in ("load_topology", "load_verified_topology"):
        monkeypatch.setattr(hermes_topology, loader, lambda: deepcopy(snapshot))
    monkeypatch.setattr(profile_context, "native_context", lambda **kwargs: {
        "native": True, "profile_name": "default", "hermes_home": home_value})
    monkeypatch.setattr(profile_context, "profile_value", lambda key, default=None: os.environ.get(key, default))
    for key, value in SCOPE.items():
        monkeypatch.setenv("EIMEMORY_" + key.upper(), value)
    monkeypatch.setenv("HERMES_HOME", home_value)
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    try:
        yield provider, client, canonical
    finally:
        provider.shutdown()


@pytest.mark.parametrize("workspace", ["embodied", "embodied::channel::hermes"])
def test_deployment_replay_accepts_base_and_channel_scope_without_business_credit(registered_replay, monkeypatch, workspace):
    provider, client, canonical = registered_replay
    monkeypatch.setenv("EIMEMORY_WORKSPACE_ID", workspace)
    _initialize_deployment_replay(provider, "deployment-replay")
    assert provider._scope == canonical
    assert provider._expected_scope == dict(SCOPE, workspace_id=workspace)
    assert provider._identity_context["agent_workspace"] == "hermes"
    assert provider._host_binding["binding_kind"] == "operator"
    assert provider._host_binding["natural_sample_eligible"] is False
    assert client.calls == []


@pytest.mark.parametrize(("key", "value"), [
    ("workspace_id", "another-workspace"),
    ("workspace_id", "embodied::channel::codex"),
    ("user_id", "foreign-user"),
    ("agent_id", "foreign-agent"),
    ("tenant_id", "foreign-tenant"),
])
def test_deployment_replay_rejects_other_scope_before_rpc(registered_replay, monkeypatch, key, value):
    provider, client, _ = registered_replay
    monkeypatch.setenv("EIMEMORY_" + key.upper(), value)
    with pytest.raises(ValueError, match="hermes_(expected_scope_mismatch|topology_request_scope_unbound)"):
        _initialize_deployment_replay(provider, "deployment-replay")
    assert client.calls == []


def test_deployment_replay_keeps_explicit_environment_scope_without_registration(monkeypatch, tmp_path):
    monkeypatch.setattr(hermes_topology, "load_topology", lambda: None)
    monkeypatch.setattr(profile_context, "native_context", lambda **kwargs: {
        "native": False, "profile_name": "", "hermes_home": str(tmp_path)})
    monkeypatch.setattr(profile_context, "profile_value", lambda key, default=None: os.environ.get(key, default))
    for key, value in SCOPE.items():
        monkeypatch.setenv("EIMEMORY_" + key.upper(), value)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    client = FakeClient()
    provider = HermesMemoryProviderCore(client=client)
    try:
        _initialize_deployment_replay(provider, "deployment-replay")
        assert provider._scope == SCOPE
        assert provider._expected_scope == SCOPE
        assert client.calls == []
    finally:
        provider.shutdown()


@pytest.mark.parametrize("workspace", ["embodied", "embodied::channel::hermes"])
def test_independent_registered_probe_accepts_equivalent_scope(registered_replay, workspace):
    provider, _, canonical = registered_replay
    seen = []
    def initialize(*args, **kwargs):
        # Exercise the real initialization and stop before any recall, delivery,
        # or production evidence. This fixture never accesses a runtime store.
        HermesMemoryProviderCore.initialize(provider, *args, **kwargs)
        seen.append(kwargs)
        raise StopIteration("initialized")
    provider.initialize = initialize
    with pytest.raises(StopIteration, match="initialized"):
        probe_hermes_recall_identity(provider, scope=dict(SCOPE, workspace_id=workspace),
            source_ids=["hermes"], queries=["synthetic query"])
    assert provider._scope == canonical
    assert seen[0]["agent_workspace"] == "hermes"
    assert seen[0]["expected_scope"] == dict(SCOPE, workspace_id=workspace)


@pytest.mark.parametrize(("key", "value"), [
    ("workspace_id", "another-workspace"), ("user_id", "foreign-user"),
    ("agent_id", "foreign-agent"), ("tenant_id", "foreign-tenant"),
])
def test_independent_registered_probe_rejects_other_scope_before_rpc(registered_replay, key, value):
    provider, client, _ = registered_replay
    with pytest.raises(ValueError, match="hermes_(expected_scope_mismatch|topology_request_scope_unbound)"):
        probe_hermes_recall_identity(provider, scope={**SCOPE, key: value}, source_ids=["hermes"], queries=["synthetic query"])
    assert client.calls == []


def test_independent_registered_probe_retains_exact_source_check(registered_replay):
    provider, client, _ = registered_replay
    with pytest.raises(ValueError, match="hermes_expected_sources_mismatch"):
        probe_hermes_recall_identity(provider, scope=SCOPE, source_ids=["foreign-source"], queries=["synthetic query"])
    assert client.calls == []
