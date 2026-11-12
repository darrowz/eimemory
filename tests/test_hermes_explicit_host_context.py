"""Only the real Hermes middleware invocation identifies explicit captures."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.adapters.hermes.provider_registry import bind_hermes_provider, unbind_hermes_provider


class Client:
    def __init__(self):
        self.calls = []

    def call_or_bypass(self, method, params):
        self.calls.append((method, params))
        return {"ok": True, "result": {"ok": True, "context": "remembered context"}}


@pytest.fixture
def middleware(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "integrations/hermes/eimemory_hook/__init__.py"
    spec = importlib.util.spec_from_file_location("explicit_host_context_hook", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "hermes_attestation_client_from_env", lambda **_kwargs: None)

    class Context:
        def __init__(self):
            self._manager = SimpleNamespace(_middleware={})

        def register_middleware(self, name, callback):
            self._manager._middleware[name] = [callback]

        def register_hook(self, name, callback):
            pass

    context = Context()
    module.register(context)
    return context._manager._middleware["tool_execution"][0]


@pytest.fixture
def make_provider(tmp_path, monkeypatch):
    providers = []
    for key in ("EIMEMORY_AGENT_ID", "EIMEMORY_DEPLOY_SCOPE_AGENT", "EIMEMORY_WORKSPACE_ID",
                "EIMEMORY_DEPLOY_SCOPE_WORKSPACE", "EIMEMORY_SOURCE_IDS", "EIMEMORY_HERMES_SOURCE_IDS"):
        monkeypatch.delenv(key, raising=False)

    def make(profile="hongtai", session="host-session", *, provider_type=HermesMemoryProviderCore):
        client = Client()
        provider = provider_type(client=client)
        provider.initialize(session, agent_identity=profile, agent_workspace="embodied",
                            user_id="channel-user", hermes_home=str(tmp_path / profile))
        bind_hermes_provider(session_id=session, provider=provider)
        providers.append(provider)
        return provider, client

    yield make
    for provider in providers:
        unbind_hermes_provider(provider)
        provider.shutdown()


def invoke(middleware, provider, args, *, request="call-1", next_call=None, **kwargs):
    return middleware(
        "eimemory_recall", args,
        next_call or (lambda actual: provider.handle_tool_call("eimemory_recall", actual)),
        session_id=provider._session_id, turn_id="host-turn", tool_call_id=request, **kwargs,
    )


def prefetches(client):
    return [params for method, params in client.calls if method == "adapter.prefetch"]


@pytest.mark.parametrize("profile", ["hongrui", "hongtai", "hongxin", "xiaomage"])
def test_shared_middleware_binds_real_identity_for_every_profile(middleware, make_provider, profile):
    provider, client = make_provider(profile)
    args = {"query": "Which deadline applies?", "limit": 3,
            "session_id": "model-forged-session", "tool_call_id": "model-forged-call",
            "acceptance_generated": True, "capture_mode": "model-forged-mode"}
    result = json.loads(invoke(middleware, provider, args, request="host-call-42"))
    params = prefetches(client)[0]
    assert result["ok"] is True and result["result"]["context"]
    assert params["scope"]["agent_id"] == profile
    assert params["task_context"] == {"source_ids": ["hermes"]}
    assert params["explicit_request"] == {
        "session_id": "host-session", "request_id": "host-call-42",
        "acceptance_generated": False, "capture_mode": "private",
    }


@pytest.mark.parametrize("tool_name,args", [
    ("eimemory_recall", {"query": "Which deadline applies?"}),
    ("eimemory_status", {}),
])
def test_host_retained_duplicate_uses_only_verified_initialized_owner(
    middleware, make_provider, tool_name, args,
):
    from integrations.hermes.eimemory import EIMemoryProvider

    owner, client = make_provider(provider_type=EIMemoryProvider)
    candidate = EIMemoryProvider()
    # Hermes v0.23 MemoryManager.add_provider registers tools before
    # initialize_all; _each_provider swallows initialize failures and keeps
    # this candidate in _tool_to_provider.
    routes = {schema["name"]: candidate for schema in candidate.get_tool_schemas()}
    with pytest.raises(ValueError, match="hermes_provider_session_already_bound"):
        candidate.initialize(owner._session_id, agent_identity="hongtai",
                             agent_workspace="embodied", user_id="channel-user",
                             hermes_home=owner._hermes_home)
    result = json.loads(middleware(
        tool_name, args,
        lambda actual: routes[tool_name].handle_tool_call(tool_name, actual),
        session_id=owner._session_id, turn_id="host-turn", tool_call_id="host-call",
    ))
    assert result["ok"] is True
    if tool_name == "eimemory_recall":
        assert "explicit_capture" not in result
        assert prefetches(client)[-1]["explicit_request"]["request_id"] == "host-call"
    else:
        assert result["adapter_local"]["identity"] == owner.identity_diagnostics()
        assert result["adapter_local"]["handoff_outbox"]["durable"] is True
    assert client.calls[-1][1]["scope"] == owner._scope
    # A failed candidate never becomes a generally callable registry proxy.
    before = len(client.calls)
    direct = json.loads(candidate.handle_tool_call(tool_name, args))
    assert direct["ok"] is False
    assert len(client.calls) == before


def retained_candidate(make_provider):
    from integrations.hermes.eimemory import EIMemoryProvider

    owner, client = make_provider(provider_type=EIMemoryProvider)
    candidate = EIMemoryProvider()
    context = dict(agent_identity="hongtai", agent_workspace="embodied",
                   user_id="channel-user", hermes_home=owner._hermes_home)
    with pytest.raises(ValueError, match="hermes_provider_session_already_bound"):
        candidate.initialize(owner._session_id, **context)
    return owner, client, candidate, context


@pytest.mark.parametrize("change", [
    {"agent_identity": "another-agent"}, {"agent_workspace": "another-workspace"},
    {"user_id": "another-user"}, {"agent_context": "cron"},
    {"expected_source_ids": ["another-source"]},
    {"expected_scope": {"tenant_id": "other"}},
])
def test_incompatible_duplicate_initialization_does_not_gain_owner(
    middleware, make_provider, change,
):
    owner, client, candidate, context = retained_candidate(make_provider)
    with pytest.raises(ValueError, match="hermes_provider_session_already_bound"):
        candidate.initialize(owner._session_id, **{**context, **change})
    before = len(client.calls)
    result = json.loads(invoke(
        middleware, owner, {"query": "original"},
        next_call=lambda actual: candidate.handle_tool_call("eimemory_recall", actual),
    ))
    assert result["ok"] is False
    assert len(client.calls) == before


@pytest.mark.parametrize("fault", ["session", "arguments", "tool", "missing_call"])
def test_retained_candidate_requires_exact_middleware_invocation(
    middleware, make_provider, fault,
):
    owner, client, candidate, _context = retained_candidate(make_provider)
    before = len(client.calls)
    result = json.loads(middleware(
        "eimemory_recall", {"query": "original"},
        lambda actual: candidate.handle_tool_call(
            "eimemory_status" if fault == "tool" else "eimemory_recall",
            {"query": "changed"} if fault == "arguments" else actual,
        ),
        session_id="another-session" if fault == "session" else owner._session_id,
        tool_call_id="" if fault == "missing_call" else "host-call",
    ))
    assert result["ok"] is False
    assert len(client.calls) == before


@pytest.mark.parametrize("lifecycle", ["cleanup", "replacement", "rotation", "shutdown"])
def test_retained_candidate_cannot_follow_changed_registry_owner(
    middleware, make_provider, lifecycle,
):
    owner, client, candidate, _context = retained_candidate(make_provider)
    session = owner._session_id
    if lifecycle in {"cleanup", "replacement"}:
        unbind_hermes_provider(owner)
    if lifecycle == "replacement":
        replacement, replacement_client = make_provider()
    elif lifecycle == "rotation":
        owner.on_session_switch("rotated-session")
    elif lifecycle == "shutdown":
        candidate.shutdown()
    before = len(client.calls)
    result = json.loads(middleware(
        "eimemory_recall", {"query": "original"},
        lambda actual: candidate.handle_tool_call("eimemory_recall", actual),
        session_id=session, tool_call_id="host-call",
    ))
    assert result["ok"] is False
    assert len(client.calls) == before
    if lifecycle == "replacement":
        assert prefetches(replacement_client) == []


def test_successful_reinitialization_clears_retained_candidate_owner(
    middleware, make_provider,
):
    owner, client, candidate, context = retained_candidate(make_provider)
    unbind_hermes_provider(owner)
    candidate_client = Client()
    candidate._client = candidate._explicit_recall_client = candidate._proactive_client = candidate_client
    candidate._client_injected = True
    try:
        candidate.initialize("new-session", **context)
        assert candidate._retained_tool_owner is None
        result = json.loads(invoke(middleware, candidate, {"query": "new-session query"}))
        assert result["ok"] is True
        assert prefetches(candidate_client)[-1]["explicit_request"]["session_id"] == "new-session"
        assert prefetches(client) == []
    finally:
        candidate.shutdown()


def test_retained_candidate_keeps_nested_and_concurrent_invocations_isolated(
    middleware, make_provider,
):
    owner, client, candidate, _context = retained_candidate(make_provider)

    def call(index):
        def execute(actual):
            if index == 0:
                invalid = json.loads(middleware(
                    "eimemory_recall", actual,
                    lambda inner: candidate.handle_tool_call("eimemory_recall", inner),
                    session_id=owner._session_id, tool_call_id="",
                ))
                assert invalid["ok"] is False
            return candidate.handle_tool_call("eimemory_recall", actual)
        return invoke(middleware, owner, {"query": f"query {index}"},
                      request=f"call-{index}", next_call=execute)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(call, range(8)))
    assert all(json.loads(result)["ok"] for result in results)
    assert {params["query"]: params["explicit_request"]["request_id"] for params in prefetches(client)} == {
        f"query {index}": f"call-{index}" for index in range(8)
    }


def test_direct_calls_cannot_forge_host_identity_and_still_recall(make_provider):
    provider, client = make_provider()
    result = json.loads(provider.handle_tool_call(
        "eimemory_recall", {"query": "Useful prior work", "explicit_request": {"request_id": "forged"}},
        session_id="host-session", tool_call_id="forged", acceptance_generated=False,
    ))
    assert result["ok"] is True and result["result"]["context"]
    assert result["explicit_capture"] == {
        "status": "not_captured", "reason": "hermes_explicit_host_context_missing",
    }
    assert "explicit_request" not in prefetches(client)[0]
    assert prefetches(client)[0]["task_context"] == {"source_ids": ["hermes"]}


def test_changed_executed_arguments_are_not_bound_to_outer_request(middleware, make_provider):
    provider, client = make_provider()
    result = json.loads(invoke(
        middleware, provider, {"query": "original"},
        next_call=lambda _: provider.handle_tool_call("eimemory_recall", {"query": "rewritten"}),
    ))
    assert result["ok"] is True
    assert result["explicit_capture"]["reason"] == "hermes_explicit_host_invocation_mismatch"
    assert prefetches(client)[0]["query"] == "rewritten"
    assert "explicit_request" not in prefetches(client)[0]


def test_known_host_redaction_matches_the_actual_provider_egress(
    middleware, make_provider, monkeypatch,
):
    provider, client = make_provider()
    manager = SimpleNamespace(_redact_for_provider=lambda args: {
        **args, "query": args["query"].replace("fixture-secret", "[redacted]"),
    })
    monkeypatch.setitem(sys.modules, "agent.memory_manager", manager)
    args = {"query": "Find the prior fixture-secret configuration"}
    result = json.loads(invoke(
        middleware, provider, args,
        next_call=lambda actual: provider.handle_tool_call(
            "eimemory_recall", manager._redact_for_provider(actual)),
    ))
    params = prefetches(client)[0]
    assert result["ok"] is True
    assert params["query"] == "Find the prior [redacted] configuration"
    assert params["explicit_request"]["request_id"] == "call-1"


def test_unavailable_host_redaction_does_not_fabricate_a_capture(
    middleware, make_provider, monkeypatch,
):
    provider, client = make_provider()

    def broken(_args):
        raise ValueError("private host failure")

    monkeypatch.setitem(sys.modules, "agent.memory_manager", SimpleNamespace(_redact_for_provider=broken))
    result = json.loads(invoke(middleware, provider, {"query": "original"}))
    assert result["ok"] is True
    assert result["explicit_capture"]["reason"] == "hermes_explicit_host_context_missing"
    assert "explicit_request" not in prefetches(client)[0]


def test_same_session_different_provider_cannot_borrow_binding(middleware, make_provider):
    provider, _ = make_provider()
    other, client = make_provider("hongxin", "other-session")
    other._session_id = provider._session_id
    result = json.loads(invoke(
        middleware, provider, {"query": "original"},
        next_call=lambda actual: other.handle_tool_call("eimemory_recall", actual),
    ))
    assert result["explicit_capture"]["reason"] == "hermes_explicit_host_session_mismatch"
    assert "explicit_request" not in prefetches(client)[0]


@pytest.mark.parametrize("identity", [None, "", " ", "x" * 257, 42])
def test_task_id_is_not_a_substitute_for_real_tool_call_identity(
    middleware, make_provider, identity,
):
    provider, client = make_provider()
    result = json.loads(invoke(middleware, provider, {"query": "original"},
                               request=identity, task_id="host-task-shared-by-many-tools"))
    assert result["explicit_capture"]["reason"] == "hermes_explicit_host_context_missing"
    assert "explicit_request" not in prefetches(client)[0]


def test_nested_calls_restore_outer_binding_and_clear_after_return(middleware, make_provider):
    provider, client = make_provider()
    args = {"query": "outer query"}

    def nested(actual):
        invoke(middleware, provider, {"query": "inner query"}, request="inner-call")
        return provider.handle_tool_call("eimemory_recall", actual)

    invoke(middleware, provider, args, request="outer-call", next_call=nested)
    assert [p["explicit_request"]["request_id"] for p in prefetches(client)] == ["inner-call", "outer-call"]
    provider.handle_tool_call("eimemory_recall", args)
    assert "explicit_request" not in prefetches(client)[-1]


def test_invalid_nested_invocation_shadows_valid_outer_binding(middleware, make_provider):
    provider, client = make_provider()
    args = {"query": "same query"}

    def nested(actual):
        inner = json.loads(invoke(middleware, provider, actual, request=""))
        assert inner["explicit_capture"]["reason"] == "hermes_explicit_host_context_missing"
        return provider.handle_tool_call("eimemory_recall", actual)

    invoke(middleware, provider, args, request="outer-call", next_call=nested)
    assert "explicit_request" not in prefetches(client)[0]
    assert prefetches(client)[1]["explicit_request"]["request_id"] == "outer-call"


def test_exception_resets_binding_before_next_direct_invocation(middleware, make_provider):
    provider, client = make_provider()

    def fail(_args):
        raise RuntimeError("host failure")

    with pytest.raises(RuntimeError, match="host failure"):
        invoke(middleware, provider, {"query": "original"}, next_call=fail)
    result = json.loads(provider.handle_tool_call("eimemory_recall", {"query": "original"}))
    assert result["explicit_capture"]["reason"] == "hermes_explicit_host_context_missing"
    assert "explicit_request" not in prefetches(client)[0]


def test_concurrent_invocations_keep_their_own_host_call_identity(middleware, make_provider):
    provider, client = make_provider()
    barrier = threading.Barrier(4)

    def run(index):
        def next_call(actual):
            barrier.wait(timeout=5)
            return provider.handle_tool_call("eimemory_recall", actual)
        return invoke(middleware, provider, {"query": f"query {index}"},
                      request=f"call-{index}", next_call=next_call)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))
    assert all(json.loads(result)["ok"] for result in results)
    assert {(p["query"], p["explicit_request"]["request_id"]) for p in prefetches(client)} == {
        (f"query {index}", f"call-{index}") for index in range(4)
    }
