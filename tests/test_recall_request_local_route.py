"""Saved invocation context must not lose or share a scoped caller model."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import runpy
import sys
from threading import Barrier
from types import ModuleType, SimpleNamespace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.contracts.recall_boundary import (
    normalize_runtime_channel,
    runtime_channel_from_workspace,
)
from eimemory.llm.command_client import (
    bind_verifier_route,
    current_verifier_route,
    reset_verifier_route,
    UnavailableVerifierRoute,
    VerifierRouteUnavailable,
)
from eimemory.retrieval.caller_assistance import route_for_channel, route_for_request


AGENTS = ("hongrui", "hongtai", "hongxin", "xiaomage")


def _scope(agent="hongrui", workspace="work::channel::hermes"):
    return {"tenant_id": "tenant", "agent_id": agent,
            "workspace_id": workspace, "user_id": "operator"}


def _route(agent):
    return {"provider": f"provider-{agent}", "model": f"model-{agent}", "caller_route_required": True}


@pytest.fixture
def hermes_profiles(tmp_path, monkeypatch):
    from eimemory import identity

    home = tmp_path / "hermes"
    for agent in AGENTS:
        path = home / "config.yaml" if agent == AGENTS[0] else home / "profiles" / agent / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"model:\n  provider: provider-{agent}\n  default: model-{agent}\n", encoding="utf-8")
    monkeypatch.setenv("EIMEMORY_HERMES_HOME", str(home))
    monkeypatch.setenv("EIMEMORY_LEGACY_AGENT_IDS", "hongtu")
    monkeypatch.setattr(identity, "HONGTU_AGENT_ID", AGENTS[0])
    return home


def _api(callback):
    api = MemoryAPI.__new__(MemoryAPI)
    api.recall_engine = SimpleNamespace(recall=callback)
    return api


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("channel", ["hermes", "hermes-agent", "hermes_agent", "proactive", "", None, "scheduled-task"])
def test_memory_api_uses_each_scoped_profile_without_rewriting_saved_context(hermes_profiles, agent, channel):
    context = {"runtime_channel": channel, "task_type": "research.task",
               "source_ids": ["hermes"], "target_source_id": "hermes",
               "exact_scope_only": True, "nested": {"original": ["原始值", 1]}}
    scope = _scope(agent)
    before = deepcopy(context)
    original_bytes = json.dumps(before, ensure_ascii=False, sort_keys=True).encode("utf-8")
    environment = dict(os.environ)
    observed = {}

    def recall(request):
        observed["request"] = request
        observed["route"] = current_verifier_route()
        return "bundle"

    assert _api(recall).recall(query="真实业务查询", scope=scope, task_context=context, limit=7) == "bundle"
    request = observed["request"]
    saved = request.task_context_dict()
    assert saved.pop("_recall_deadline_monotonic") > 0
    assert json.dumps(saved, ensure_ascii=False, sort_keys=True).encode("utf-8") == original_bytes
    assert context == before
    assert asdict(request.scope) == scope
    assert request.query == "真实业务查询" and request.limit == 7
    assert request.source_ids == ("hermes",) and request.target_source_id == "hermes"
    assert observed["route"] == _route(agent)
    assert current_verifier_route() is None
    assert dict(os.environ) == environment


@pytest.mark.parametrize("channel", ["openclaw", "open-claw", "open_claw", "codex", "codex-cli", "codex_cli"])
def test_explicit_other_transports_cannot_use_hermes_scope(hermes_profiles, channel):
    assert route_for_request(channel, scope=_scope()) is None


@pytest.mark.parametrize("workspace", ["work", "", "work::channel::codex", "work::channel::openclaw",
                                       "work::channel::unknown", "work::channel::hermes-agent"])
@pytest.mark.parametrize("channel", ["hermes", "proactive"])
def test_source_id_or_context_cannot_turn_other_scope_into_hermes(hermes_profiles, workspace, channel):
    seen = []
    _api(lambda request: seen.append((request, current_verifier_route()))).recall(
        query="business query", scope=_scope(workspace=workspace),
        task_context={"runtime_channel": channel, "source_ids": ["hermes"]})
    assert len(seen) == 1 and seen[0][1] is None
    assert seen[0][0].source_ids == ("hermes",)
    assert seen[0][0].scope.workspace_id == workspace


@pytest.mark.parametrize("agent", ["missing-colleague", "../hongtai", "..\\hongtai", "/hongtai", "hongtai/config.yaml", "", "HONGTAI"])
def test_unknown_or_unsafe_profile_never_borrows_primary_model(hermes_profiles, agent):
    assert route_for_request("proactive", scope=_scope(agent)) == UnavailableVerifierRoute(channel="hermes")


@pytest.mark.parametrize("agent", ["hongrui", "hongtu", "default", "hermes"])
def test_configured_primary_and_its_rename_use_primary_config(hermes_profiles, agent):
    # A stale alias profile is not a different caller model for the renamed agent.
    stale = hermes_profiles / "profiles" / agent / "config.yaml"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("model:\n  provider: stale\n  default: stale\n", encoding="utf-8")
    assert route_for_request("proactive", scope=_scope(agent)) == _route("hongrui")


def test_missing_colleague_config_does_not_fall_back_to_primary(hermes_profiles):
    (hermes_profiles / "profiles" / "hongtai" / "config.yaml").unlink()
    assert route_for_channel("hermes", agent_id="hongtai") is None
    assert route_for_request("proactive", scope=_scope("hongtai")) == UnavailableVerifierRoute(channel="hermes")


def test_route_accepts_scope_ref_and_immutable_exact_scope(hermes_profiles):
    from eimemory.models.records import ScopeRef
    from eimemory.retrieval.contracts import ExactScope

    ref = ScopeRef.from_dict(_scope("hongxin"))
    exact = ExactScope.from_scope(ref)
    assert route_for_request("proactive", scope=ref) == _route("hongxin")
    assert route_for_request("proactive", scope=exact) == _route("hongxin")


@pytest.mark.parametrize("agent", AGENTS)
def test_scope_route_reaches_command_request_without_default_provider_env(hermes_profiles, monkeypatch, agent):
    from eimemory.llm import command_client

    for key in ("EIMEMORY_LUNA_PROVIDER", "EIMEMORY_LUNA_MODEL", "EIMEMORY_RECALL_PROVIDER", "EIMEMORY_RECALL_EXPECTED_MODEL"):
        monkeypatch.delenv(key, raising=False)
    requests = []

    def run(argv, raw_request, **kwargs):
        payload = json.loads(raw_request)
        requests.append(payload)
        return 0, json.dumps({"text": "verified", "provider_id": payload["provider"],
                              "model_id": payload["model"]}).encode(), b""

    # Exercise the real command payload builder; no subprocess or model runs.
    monkeypatch.setattr(command_client, "run_bounded_command", run)
    client = command_client.CommandLLMClient(["unused-fixture-command"], timeout_seconds=1)
    result = _api(lambda request: client.complete(system_prompt="policy", user_prompt=request.query)).recall(
        query="business query", scope=_scope(agent), task_context={"runtime_channel": "proactive"})
    assert len(requests) == 1
    assert requests[0]["provider"] == result.provider_id == _route(agent)["provider"]
    assert requests[0]["model"] == result.model_id == _route(agent)["model"]
    assert current_verifier_route() is None


@pytest.fixture
def native_luna_transport(monkeypatch):
    """Real bridge and command serialization; only the external provider is fake."""
    from eimemory.llm import command_client

    state = {"setups": [], "provider_calls": [], "transport_calls": [], "fail_providers": set()}

    def resolve(provider, *, model):
        state["setups"].append((provider, model))

        def create(**kwargs):
            state["provider_calls"].append((provider, kwargs["model"]))
            if provider in state["fail_providers"]:
                error = RuntimeError("usage_limit_reached")
                error.status_code = 429
                raise error
            message = SimpleNamespace(content=json.dumps({"selected": [{"id": "0", "quote": "Read the complete document"}]}), tool_calls=None)
            return SimpleNamespace(model=model, choices=[SimpleNamespace(message=message)])

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), model

    auxiliary = ModuleType("agent.auxiliary_client")
    auxiliary.resolve_provider_client = resolve
    monkeypatch.setitem(sys.modules, "agent", ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)
    monkeypatch.setitem(sys.modules, "hermes_bootstrap", ModuleType("hermes_bootstrap"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    bridge = runpy.run_path(str(Path(__file__).resolve().parents[1] / "deploy/luna_bridge/luna_review_command.py"), run_name="eimemory_luna_test")
    state["complete"] = bridge["complete"]

    def run(argv, raw_request, **kwargs):
        state["transport_calls"].append(json.loads(raw_request))
        result = bridge["complete"](json.loads(raw_request))
        return 0, json.dumps(result).encode(), b""

    monkeypatch.setattr(command_client, "run_bounded_command", run)
    monkeypatch.setenv("EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED", "1")
    monkeypatch.setenv("EIMEMORY_RECALL_LLM_COMMAND", json.dumps(["fixture-command"]))
    monkeypatch.setenv("EIMEMORY_RECALL_GATEWAY_POOL", "0")
    monkeypatch.setenv("EIMEMORY_RECALL_BRIDGE_POOL", "0")
    monkeypatch.setenv("EIMEMORY_LUNA_PROVIDER", "global-primary-provider")
    monkeypatch.setenv("EIMEMORY_LUNA_MODEL", "global-primary-model")
    monkeypatch.setenv("EIMEMORY_LUNA_FALLBACK_PROVIDER", "global-primary-fallback-provider")
    monkeypatch.setenv("EIMEMORY_LUNA_FALLBACK_MODEL", "global-primary-fallback-model")
    return state


@pytest.mark.parametrize("agent", AGENTS)
def test_native_verifier_uses_own_model_even_with_global_primary(hermes_profiles, native_luna_transport, agent):
    from eimemory.retrieval import caller_assistance

    record = SimpleNamespace(record_id="business-memory", aliases=())
    selected, report = _api(lambda request: caller_assistance.verify_candidates(
        query=request.query, candidates=[(record, "Read the complete document, not merely its title.")], limit=1)).recall(
        query="How should the document be read?", scope=_scope(agent), task_context={"runtime_channel": "proactive"})
    assert selected == [record] and report["status"] == "evidence_found"
    assert native_luna_transport["provider_calls"] == [(_route(agent)["provider"], _route(agent)["model"])]


@pytest.mark.parametrize("agent", ["hongtai", "missing-colleague", "../hongtai", "..\\hongtai", ""])
@pytest.mark.parametrize("mode", ["one-shot", "verifier", "prepared-verifier"])
def test_unavailable_scoped_model_never_calls_global_primary(hermes_profiles, native_luna_transport, monkeypatch, agent, mode):
    from eimemory.llm.command_client import CommandLLMClient
    from eimemory.retrieval import caller_assistance

    (hermes_profiles / "profiles" / "hongtai" / "config.yaml").unlink()
    client = CommandLLMClient(["fixture-command"], timeout_seconds=1)
    record = SimpleNamespace(record_id="business-memory", aliases=())
    seen = []

    def recall(request):
        seen.append(request)
        if mode == "one-shot":
            return client.complete(system_prompt="policy", user_prompt=request.query)
        if mode == "prepared-verifier":
            monkeypatch.setattr(caller_assistance, "_PREPARED", SimpleNamespace(get=lambda: client))
        return caller_assistance.verify_candidates(query=request.query,
            candidates=[(record, "Read the complete document, not merely its title.")], limit=1)

    if mode == "one-shot":
        with pytest.raises(VerifierRouteUnavailable, match="caller_model_route_unavailable"):
            _api(recall).recall(query="How should the document be read?", scope=_scope(agent), task_context={"runtime_channel": "proactive"})
    else:
        selected, report = _api(recall).recall(query="How should the document be read?", scope=_scope(agent), task_context={"runtime_channel": "proactive"})
        assert selected == [] and report["status"] == "unavailable" and report["calls"] == 0
        assert report["error_type"] == "VerifierRouteUnavailable"
    assert len(seen) == 1  # collection/independent evidence is not rejected at the API boundary
    assert all(native_luna_transport[key] == [] for key in ("setups", "provider_calls", "transport_calls"))
    assert current_verifier_route() is None


def test_unavailable_scoped_model_blocks_bridge_pool_before_acquiring_worker(hermes_profiles, native_luna_transport, monkeypatch):
    from eimemory.llm.bridge_pool import BridgePoolClient

    pool_calls = []
    client = BridgePoolClient(["fixture-command"], identity_key="fixture", timeout_seconds=1)
    monkeypatch.setattr(client, "_pool", lambda: pool_calls.append("acquired"))
    with pytest.raises(VerifierRouteUnavailable, match="caller_model_route_unavailable"):
        _api(lambda request: client.complete(system_prompt="policy", user_prompt=request.query)).recall(
            query="business query", scope=_scope("missing-colleague"), task_context={"runtime_channel": "proactive"})
    assert pool_calls == [] and native_luna_transport["provider_calls"] == []


def test_unavailable_scoped_model_still_allows_recall_without_model(hermes_profiles, native_luna_transport):
    seen = []
    result = _api(lambda request: seen.append(request) or "independently-authorized-evidence").recall(
        query="business query", scope=_scope("missing-colleague"), task_context={"runtime_channel": "proactive"})
    assert result == "independently-authorized-evidence" and len(seen) == 1
    assert native_luna_transport["provider_calls"] == []
    assert current_verifier_route() is None


def test_non_hermes_transport_preserves_legacy_global_model_defaults(hermes_profiles, native_luna_transport):
    from eimemory.llm.command_client import CommandLLMClient

    client = CommandLLMClient(["fixture-command"], timeout_seconds=1)
    result = _api(lambda request: client.complete(system_prompt="policy", user_prompt=request.query)).recall(
        query="business query", scope=_scope(workspace="work::channel::codex"), task_context={"runtime_channel": "codex"})
    assert (result.provider_id, result.model_id) == ("global-primary-provider", "global-primary-model")
    assert native_luna_transport["provider_calls"] == [("global-primary-provider", "global-primary-model")]


def _native_transport_client(mode, state, monkeypatch):
    from eimemory.llm.command_client import CommandLLMClient, LLMResult
    from eimemory.llm.bridge_pool import BridgePoolClient

    if mode == "one-shot":
        return CommandLLMClient(["fixture-command"], timeout_seconds=1)
    client = BridgePoolClient(["fixture-command"], identity_key="fixture", timeout_seconds=1)

    def call(payload, timeout):
        state["transport_calls"].append(payload)
        return LLMResult(**state["complete"](payload))

    worker = SimpleNamespace(call=call)
    pool = SimpleNamespace(acquire=lambda: worker, release=lambda value: None)
    monkeypatch.setattr(client, "_pool", lambda: pool)
    return client


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("mode", ["one-shot", "pool"])
def test_scoped_model_failure_cannot_borrow_global_fallback(hermes_profiles, native_luna_transport, monkeypatch, agent, mode):
    state = native_luna_transport
    state["fail_providers"].add(_route(agent)["provider"])
    client = _native_transport_client(mode, state, monkeypatch)
    with pytest.raises(RuntimeError, match="usage_limit_reached"):
        _api(lambda request: client.complete(system_prompt="policy", user_prompt=request.query)).recall(
            query="business query", scope=_scope(agent), task_context={"runtime_channel": "proactive"})
    assert state["setups"] == state["provider_calls"] == [(_route(agent)["provider"], _route(agent)["model"])]
    assert len(state["transport_calls"]) == 1 and state["transport_calls"][0]["caller_route_required"] is True
    assert current_verifier_route() is None


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("mode", ["one-shot", "pool"])
def test_scoped_model_can_use_only_its_own_explicit_fallback(hermes_profiles, native_luna_transport, monkeypatch, agent, mode):
    path = hermes_profiles / "config.yaml" if agent == AGENTS[0] else hermes_profiles / "profiles" / agent / "config.yaml"
    path.write_text(path.read_text(encoding="utf-8") + f"fallback_providers:\n  - provider: fallback-{agent}\n    model: fallback-model-{agent}\n", encoding="utf-8")
    state = native_luna_transport
    state["fail_providers"].add(_route(agent)["provider"])
    client = _native_transport_client(mode, state, monkeypatch)
    result = _api(lambda request: client.complete(system_prompt="policy", user_prompt=request.query)).recall(
        query="business query", scope=_scope(agent), task_context={"runtime_channel": "proactive"})
    expected = [(_route(agent)["provider"], _route(agent)["model"]), (f"fallback-{agent}", f"fallback-model-{agent}")]
    assert state["setups"] == state["provider_calls"] == expected
    assert (result.provider_id, result.model_id) == expected[-1]


@pytest.mark.parametrize("fields", [{}, {"provider": "own"}, {"model": "own"}, {"provider": " ", "model": "own"},
                                    {"provider": "own", "model": "own", "fallback_provider": "own-fallback"},
                                    {"provider": "own", "model": "own", "fallback_model": "own-fallback"}])
def test_native_bridge_required_route_rejects_missing_or_partial_route_without_provider_calls(native_luna_transport, fields):
    with pytest.raises((ValueError, RuntimeError)):
        native_luna_transport["complete"]({"system_prompt": "policy", "user_prompt": "business query",
                                            "caller_route_required": True, **fields})
    assert native_luna_transport["setups"] == native_luna_transport["provider_calls"] == []


def test_native_bridge_rejects_nonboolean_route_requirement(native_luna_transport):
    with pytest.raises(ValueError, match="caller_route_required_invalid"):
        native_luna_transport["complete"]({"system_prompt": "policy", "user_prompt": "business query",
                                            "caller_route_required": "true"})
    assert native_luna_transport["setups"] == native_luna_transport["provider_calls"] == []


def test_non_scoped_legacy_model_can_still_use_global_fallback(hermes_profiles, native_luna_transport):
    from eimemory.llm.command_client import CommandLLMClient

    state = native_luna_transport
    state["fail_providers"].add("global-primary-provider")
    client = CommandLLMClient(["fixture-command"], timeout_seconds=1)
    result = _api(lambda request: client.complete(system_prompt="policy", user_prompt=request.query)).recall(
        query="business query", scope=_scope(workspace="work::channel::codex"), task_context={"runtime_channel": "codex"})
    assert state["provider_calls"] == [("global-primary-provider", "global-primary-model"),
                                        ("global-primary-fallback-provider", "global-primary-fallback-model")]
    assert (result.provider_id, result.model_id) == state["provider_calls"][-1]


def test_nested_recalls_restore_parent_route_and_restore_outer_context_on_exception(hermes_profiles):
    outer_route = {"provider": "outer", "model": "outer"}
    outer = bind_verifier_route(outer_route)
    events = []

    def inner(request):
        events.append((request.scope.agent_id, current_verifier_route()))
        raise RuntimeError("engine failed")

    def parent(request):
        events.append((request.scope.agent_id, current_verifier_route()))
        with pytest.raises(RuntimeError, match="engine failed"):
            _api(inner).recall(query="inner", scope=_scope("hongxin"), task_context={"runtime_channel": "proactive"})
        assert current_verifier_route() == _route("hongtai")
        raise ValueError("parent failed")

    try:
        with pytest.raises(ValueError, match="parent failed"):
            _api(parent).recall(query="parent", scope=_scope("hongtai"), task_context={"runtime_channel": "proactive"})
        assert current_verifier_route() == outer_route
        assert events == [("hongtai", _route("hongtai")), ("hongxin", _route("hongxin"))]
    finally:
        reset_verifier_route(outer)
    assert current_verifier_route() is None


def test_concurrent_colleague_recalls_keep_independent_routes(hermes_profiles):
    barrier = Barrier(len(AGENTS))

    def run(agent):
        outer = {"provider": f"outer-{agent}", "model": "outer"}
        token = bind_verifier_route(outer)

        def recall(request):
            barrier.wait(timeout=5)
            assert current_verifier_route() == _route(agent)
            barrier.wait(timeout=5)
            return request.scope.agent_id

        try:
            result = _api(recall).recall(query="business query", scope=_scope(agent), task_context={"runtime_channel": "proactive"})
            assert current_verifier_route() == outer
            return result
        finally:
            reset_verifier_route(token)
            assert current_verifier_route() is None

    with ThreadPoolExecutor(max_workers=len(AGENTS)) as executor:
        assert list(executor.map(run, AGENTS)) == list(AGENTS)
    assert current_verifier_route() is None


@pytest.mark.parametrize("channel,canonical", [("hermes-agent", "hermes"), ("hermes_agent", "hermes"),
                                               ("codex-cli", "codex"), ("open_claw", "openclaw")])
def test_runtime_adapter_delegates_pure_explicit_channel_contract(channel, canonical):
    from eimemory.adapters.runtime.channel import normalize_runtime_channel as adapter_normalize

    assert adapter_normalize(channel) == normalize_runtime_channel(channel) == canonical


@pytest.mark.parametrize("suffix,expected", [("hermes", "hermes"), ("codex", "codex"),
                                            ("openclaw", "openclaw"), (" HERMES ", "hermes"),
                                            ("hermes-agent", ""), ("unknown", "")])
def test_runtime_adapter_preserves_stored_scope_suffix_contract(suffix, expected):
    from eimemory.adapters.runtime.channel import runtime_channel_from_scope

    scope = _scope(workspace="work::channel::" + suffix)
    assert runtime_channel_from_scope(scope) == runtime_channel_from_workspace(scope["workspace_id"]) == expected


def test_unknown_explicit_channel_normalization_still_fails_closed():
    from eimemory.adapters.runtime.channel import normalize_runtime_channel as adapter_normalize

    for normalize in (normalize_runtime_channel, adapter_normalize):
        with pytest.raises(ValueError, match="unsupported runtime channel: proactive"):
            normalize("proactive")
