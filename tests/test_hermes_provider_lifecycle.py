"""Provider ownership through real, versioned Hermes MemoryManager methods.

Only the host's environment, background executor and RPC transport are stubbed.
The fixture is historical upstream code, not a claim about the live host version.
"""
from __future__ import annotations

import ast
from contextvars import copy_context
import inspect
import json
import logging
from pathlib import Path
import sys
import threading
from types import ModuleType

import pytest

from eimemory.adapters.hermes.provider_registry import get_hermes_provider
from integrations.hermes.eimemory import EIMemoryProvider


HOST = Path(__file__).parent / "fixtures/hermes_hosts/memory_manager_main_e0550c97.py.txt"


class Client:
    def __init__(self):
        self.calls = []

    def call_or_bypass(self, method, params):
        self.calls.append((method, params))
        if method == "adapter.proactive_prefetch":
            return {"ok": True, "bypassed": True, "result": {"ok": True, "bypassed": True}}
        return {"ok": True, "result": {"ok": True, "context": "synthetic prior memory"}}


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    for key in ("EIMEMORY_AGENT_ID", "EIMEMORY_DEPLOY_SCOPE_AGENT", "EIMEMORY_WORKSPACE_ID",
                "EIMEMORY_DEPLOY_SCOPE_WORKSPACE", "EIMEMORY_USER_ID", "EIMEMORY_SOURCE_IDS",
                "EIMEMORY_HERMES_SOURCE_IDS", "EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE"):
        monkeypatch.delenv(key, raising=False)

    def module(name, **values):
        result = ModuleType(name)
        result.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, result)

    module("hermes_cli.config", load_config=lambda: {})
    module("toolsets", _HERMES_CORE_TOOLS=set())
    module("hermes_cli.observability.shared_metrics_loop", record_provider_memory_call=lambda *a, **k: None)
    tree = ast.parse(HOST.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MemoryManager")
    methods = {"__init__", "_each_provider", "add_provider", "initialize_all", "build_system_prompt",
               "prefetch_all", "_prefetch_provider", "sync_all", "_provider_sync_accepts", "handle_tool_call",
               "on_session_end", "on_session_switch", "commit_session_boundary_async"}
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    namespace = {
        "logger": logging.getLogger("hermes.lifecycle.fixture"), "logging": logging,
        "threading": threading, "_EXTERNAL_PREFETCH_TIMEOUT_S": 2,
        "get_spill_config": lambda: {"max_chars": 10000},
        "spill_if_oversized": lambda text, **kwargs: text,
        "normalize_tool_schema": lambda schema: schema,
        "_redact_for_provider": lambda *values: values[0] if len(values) == 1 else values,
        "_redact_messages_for_egress": lambda messages: list(messages),
        "_signature_params": lambda fn: inspect.signature(fn).parameters,
        "_has_var_kwargs": lambda params: any(p.kind == p.VAR_KEYWORD for p in params.values()),
        "spawn_context_thread": lambda fn, **kwargs: threading.Thread(target=copy_context().run, args=(fn,), **kwargs),
    }
    module_ast = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module_ast), str(HOST), "exec"), namespace)
    manager_type = namespace["MemoryManager"]
    manager_type._strip_skill_scaffolding = staticmethod(lambda text: text)
    # The real host has this inline fallback when its background executor is absent.
    manager_type._submit_background = lambda self, fn, **kwargs: fn()
    context = dict(agent_identity="synthetic-agent", agent_workspace="synthetic-workspace",
                   user_id="synthetic-user", hermes_home=str(tmp_path / "profile"))
    providers = []

    def make():
        client = Client()
        provider = EIMemoryProvider(client=client)
        providers.append(provider)
        manager = manager_type()
        manager.add_provider(provider)
        return manager, provider, client

    yield make, context
    for provider in providers:
        provider.shutdown()


def test_host_same_owner_initialize_is_idempotent(lifecycle):
    make, context = lifecycle
    manager, provider, client = make()
    manager.initialize_all("session-a", **context)
    provider.bind_verified_host_turn(session_id="session-a", turn_id="host-turn")
    manager.sync_all("synthetic question", "synthetic answer", session_id="session-a")
    summary = provider.on_pre_compress([])
    outbox = provider._outbox
    manager.initialize_all("  session-a  ", **context)
    assert list(provider._verified_host_turns) == [("session-a", "host-turn")]
    assert provider.on_pre_compress([]) == summary and summary
    assert provider._outbox is outbox
    assert get_hermes_provider("session-a", hermes_home=context["hermes_home"]) is provider
    assert manager.build_system_prompt()
    assert manager.prefetch_all("synthetic prior work", session_id="session-a")


def test_same_owner_reentry_can_recover_availability_without_clearing_turn(lifecycle, monkeypatch):
    make, context = lifecycle
    manager, provider, _client = make()
    provider._client_injected = False
    available = {"value": False}
    monkeypatch.setattr(provider, "is_available", lambda: available["value"])
    manager.initialize_all("session-a", **context)
    assert not provider._active
    provider.bind_verified_host_turn(session_id="session-a", turn_id="host-turn")
    available["value"] = True
    manager.initialize_all("session-a", **context)
    assert provider._active
    assert list(provider._verified_host_turns) == [("session-a", "host-turn")]


def test_failed_first_initialization_can_retry_same_owner(lifecycle, monkeypatch):
    make, context = lifecycle
    _manager, provider, _client = make()
    start = provider._start_write_worker

    def fail():
        raise OSError("synthetic startup failure")

    monkeypatch.setattr(provider, "_start_write_worker", fail)
    with pytest.raises(OSError, match="synthetic startup failure"):
        provider.initialize("session-a", **context)
    assert not provider._active
    assert get_hermes_provider("session-a", hermes_home=context["hermes_home"]) is None
    monkeypatch.setattr(provider, "_start_write_worker", start)
    provider.initialize("session-a", **context)
    assert provider._active
    assert get_hermes_provider("session-a", hermes_home=context["hermes_home"]) is provider


@pytest.mark.parametrize("change", [
    {"agent_identity": "different-agent"}, {"agent_workspace": "different-workspace"},
    {"user_id": "different-user"}, {"agent_context": "cron"},
    {"expected_source_ids": ["different-source"]}, {"expected_scope": {"tenant_id": "other"}},
    {"hermes_home": "different-profile"},
])
def test_initialized_instance_cannot_change_owner(lifecycle, change):
    make, context = lifecycle
    manager, provider, _client = make()
    manager.initialize_all("session-a", **context)
    original = (dict(provider._host_binding), dict(provider._scope), provider._outbox)
    if "hermes_home" in change:
        change = {**change, "hermes_home": str(Path(context["hermes_home"]).parent / "different-profile")}
    with pytest.raises(ValueError, match="hermes_(session_owner_change_requires_new_provider|expected_sources_mismatch|expected_scope_invalid)"):
        provider.initialize("session-a", **{**context, **change})
    assert (provider._host_binding, provider._scope, provider._outbox) == original
    assert get_hermes_provider("session-a", hermes_home=context["hermes_home"]) is provider


def test_initialized_instance_rejects_changed_source_configuration(lifecycle, monkeypatch):
    make, context = lifecycle
    manager, provider, _client = make()
    manager.initialize_all("session-a", **context)
    monkeypatch.setenv("EIMEMORY_SOURCE_IDS", "another-source")
    with pytest.raises(ValueError, match="owner_change_requires_new_provider"):
        provider.initialize("session-a", **context)
    assert provider._source_ids() == ["hermes"]


def test_idempotent_reentry_still_verifies_native_owner(lifecycle, monkeypatch, tmp_path):
    from eimemory.contracts import hermes_topology

    make, context = lifecycle
    current = {"home": context["hermes_home"], "user": context["user_id"]}
    for name, values in {
        "hermes_constants": {"get_hermes_home": lambda: current["home"]},
        "hermes_cli.profiles": {"get_active_profile_name": lambda: "default"},
        "gateway.session_context": {"get_session_env": lambda *_args: current["user"]},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(hermes_topology, "load_topology", lambda: None)
    manager, provider, _client = make()
    manager.initialize_all("session-a", **context)
    assert provider._host_binding["native"] is True
    provider.initialize("session-a", **context)
    current["home"] = str(tmp_path / "other-native-profile")
    with pytest.raises(ValueError, match="hermes_native_profile_home_mismatch"):
        provider.initialize("session-a", **context)
    current["home"] = context["hermes_home"]
    current["user"] = "different-native-user"
    with pytest.raises(ValueError, match="hermes_native_request_user_mismatch"):
        provider.initialize("session-a", **context)
    current["user"] = context["user_id"]
    assert get_hermes_provider("session-a", hermes_home=context["hermes_home"]) is provider


def test_same_owner_reinitialize_to_new_session_moves_registry(lifecycle):
    make, context = lifecycle
    manager, provider, _client = make()
    manager.initialize_all("session-a", **context)
    provider.bind_verified_host_turn(session_id="session-a", turn_id="old-turn")
    manager.initialize_all("session-b", **context)
    assert provider._session_id == "session-b"
    assert get_hermes_provider("session-a", hermes_home=context["hermes_home"]) is None
    assert get_hermes_provider("session-b", hermes_home=context["hermes_home"]) is provider
    assert not provider._verified_host_turns


def test_real_host_end_then_switch_preserves_canonical_provider(lifecycle):
    make, context = lifecycle
    manager, provider, client = make()
    manager.initialize_all("session-a", **context)
    manager.commit_session_boundary_async([], new_session_id="session-b", parent_session_id="session-a")
    terminal = [p for method, p in client.calls if method == "adapter.record_terminal"]
    assert [p["session_id"] for p in terminal] == ["session-a"]
    assert get_hermes_provider("session-a", hermes_home=context["hermes_home"]) is None
    assert get_hermes_provider("session-b", hermes_home=context["hermes_home"]) is provider
    assert provider._active and manager.build_system_prompt()
    assert manager.prefetch_all("synthetic query", session_id="session-b")


def test_session_end_keeps_owner_until_actual_shutdown(lifecycle):
    make, context = lifecycle
    manager, provider, _client = make()
    manager.initialize_all("session-a", **context)
    manager.on_session_end([])
    assert provider._active
    assert get_hermes_provider("session-a", hermes_home=context["hermes_home"]) is provider
    _duplicate_manager, duplicate, _duplicate_client = make()
    with pytest.raises(ValueError, match="hermes_provider_session_already_bound"):
        duplicate.initialize("session-a", **context)


def test_active_duplicate_remains_rejected_and_host_failure_is_visible(lifecycle, caplog):
    make, context = lifecycle
    original_manager, owner, _owner_client = make()
    original_manager.initialize_all("session-a", **context)
    duplicate_manager, candidate, client = make()
    duplicate_manager.initialize_all("session-a", **context)
    assert "hermes_provider_session_already_bound" in caplog.text
    assert get_hermes_provider("session-a", hermes_home=context["hermes_home"]) is owner
    assert duplicate_manager._tool_to_provider["eimemory_recall"] is candidate
    assert not candidate._active
    assert duplicate_manager.build_system_prompt() == ""
    assert duplicate_manager.prefetch_all("synthetic query", session_id="session-a") == ""
    duplicate_manager.sync_all("synthetic question", "synthetic answer", session_id="session-a")
    assert candidate._outbox is None and not client.calls


def test_shutdown_releases_binding_and_cannot_be_resurrected(lifecycle):
    make, context = lifecycle
    manager, provider, client = make()
    manager.initialize_all("session-a", **context)
    provider.shutdown()
    assert not provider._active
    assert get_hermes_provider("session-a", hermes_home=context["hermes_home"]) is None
    before = len(client.calls)
    assert manager.build_system_prompt() == ""
    assert manager.prefetch_all("synthetic query", session_id="session-a") == ""
    manager.sync_all("synthetic question", "synthetic answer", session_id="session-a")
    provider.on_post_llm_call(user_message="late synthetic question", assistant_message="late synthetic answer",
                              session_id="session-a", turn_id="late-turn")
    result = json.loads(provider.handle_tool_call("eimemory_remember", {"text": "synthetic", "event_id": "late"}))
    assert not result["ok"] and result["error"] == "hermes_provider_closed"
    with pytest.raises(ValueError, match="hermes_provider_closed"):
        provider.initialize("session-a", **context)
    with pytest.raises(ValueError, match="hermes_provider_closed"):
        provider.on_session_switch("session-b")
    assert len(client.calls) == before
    replacement_manager, replacement, _replacement_client = make()
    replacement_manager.initialize_all("session-a", **context)
    assert get_hermes_provider("session-a", hermes_home=context["hermes_home"]) is replacement
    assert replacement._active and replacement_manager.build_system_prompt()


def test_shutdown_drains_existing_capture_before_releasing_owner(lifecycle):
    make, context = lifecycle
    manager, provider, client = make()
    manager.initialize_all("session-a", **context)
    manager.sync_all("synthetic question", "synthetic answer", session_id="session-a")
    provider.shutdown()
    captures = [params for method, params in client.calls if method == "adapter.sync_turn"]
    assert len(captures) == 1 and captures[0]["session_id"] == "session-a"
    assert not provider._outbox.entries("write")
    assert not provider._active
