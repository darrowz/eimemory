from __future__ import annotations

import getpass
import json
from pathlib import Path

import pytest

from eimemory.scheduler.memory_benchmark_context import resolve_memory_benchmark_context

SCOPE = {"tenant_id": "tenant-one", "agent_id": "agent-one", "workspace_id": "workspace-one",
         "user_id": "logical-user"}


def _bindings(tmp_path, monkeypatch, scopes, *, mode=0o600):
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps([{"scope": scope, "receipt_id": "rec-test", "receipt_sha256": "a" * 64}
                                for scope in scopes]))
    path.chmod(mode)
    monkeypatch.setenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", str(path))
    return path


def _channel(scope, channel):
    return {**scope, "workspace_id": scope["workspace_id"] + "::channel::" + channel}


@pytest.fixture(autouse=True)
def _clean_context(monkeypatch):
    monkeypatch.delenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", raising=False)
    monkeypatch.delenv("EIMEMORY_DEPLOY_SCOPE_USER", raising=False)
    monkeypatch.delenv("EIMEMORY_SOURCE_IDS", raising=False)
    monkeypatch.setattr(getpass, "getuser", lambda: "host-user")


def test_unqualified_scope_uses_unique_private_binding_and_adapter_sources(tmp_path, monkeypatch):
    _bindings(tmp_path, monkeypatch, [_channel(SCOPE, "hermes")])
    monkeypatch.setenv("EIMEMORY_SOURCE_IDS", "team-memory")
    context = resolve_memory_benchmark_context(scope=SCOPE)
    assert context["ok"] is True
    assert context["scope"] == _channel(SCOPE, "hermes")
    assert context["source_ids"] == ["team-memory", "hermes"]
    assert context["channel"] == "hermes"
    assert context["resolution"]["channel_source"] == "operator_binding"


def test_host_account_uses_same_channel_bound_logical_user(tmp_path, monkeypatch):
    _bindings(tmp_path, monkeypatch, [_channel(SCOPE, "hermes")])
    context = resolve_memory_benchmark_context(scope={**SCOPE, "user_id": "host-user"})
    assert context["ok"] is True
    assert context["scope"] == _channel(SCOPE, "hermes")
    assert context["resolution"]["logical_user_rewritten"] is True
    assert "host-user" not in json.dumps(context["resolution"])


def test_explicit_channel_request_cannot_be_redirected_by_different_binding(tmp_path, monkeypatch):
    _bindings(tmp_path, monkeypatch, [_channel(SCOPE, "hermes")])
    requested = _channel(SCOPE, "codex")
    context = resolve_memory_benchmark_context(scope=requested)
    assert context["ok"] is True
    assert context["scope"] == requested
    assert context["channel"] == "codex"
    assert context["source_ids"] == ["default"]


def test_explicit_channel_cannot_reuse_host_user_translation_from_other_channel(tmp_path, monkeypatch):
    _bindings(tmp_path, monkeypatch, [_channel(SCOPE, "hermes")])
    context = resolve_memory_benchmark_context(scope=_channel({**SCOPE, "user_id": "host-user"}, "codex"))
    assert context["ok"] is False
    assert context["reason"] == "memory_benchmark_logical_user_channel_binding_missing"


def test_explicit_channel_scope_is_sufficient_without_database_or_install_discovery():
    requested = _channel(SCOPE, "hermes")
    context = resolve_memory_benchmark_context(scope=requested)
    assert context["ok"] is True
    assert context["scope"] == requested
    assert context["source_ids"] == ["hermes"]


@pytest.mark.parametrize("field", ["tenant_id", "agent_id", "workspace_id", "user_id"])
def test_unqualified_scope_does_not_borrow_other_identity_binding(tmp_path, monkeypatch, field):
    other = {**SCOPE, field: "other-identity"}
    _bindings(tmp_path, monkeypatch, [_channel(other, "hermes")])
    context = resolve_memory_benchmark_context(scope=SCOPE)
    assert context["ok"] is False
    assert context["reason"] == "memory_benchmark_channel_binding_missing"


def test_unqualified_multi_channel_binding_is_blocked_instead_of_guessing(tmp_path, monkeypatch):
    _bindings(tmp_path, monkeypatch, [_channel(SCOPE, "hermes"), _channel(SCOPE, "codex")])
    context = resolve_memory_benchmark_context(scope=SCOPE)
    assert context["ok"] is False
    assert context["reason"] == "memory_benchmark_channel_binding_ambiguous"
    assert context["resolution"]["matching_channel_count"] == 2


def test_duplicate_same_channel_pins_are_one_authority(tmp_path, monkeypatch):
    _bindings(tmp_path, monkeypatch, [_channel(SCOPE, "hermes"), _channel(SCOPE, "hermes")])
    assert resolve_memory_benchmark_context(scope=SCOPE)["ok"] is True


def test_unqualified_openclaw_binding_retains_exact_unsuffixed_scope(tmp_path, monkeypatch):
    _bindings(tmp_path, monkeypatch, [SCOPE])
    context = resolve_memory_benchmark_context(scope=SCOPE)
    assert context["ok"] is True
    assert context["scope"] == SCOPE
    assert context["channel"] == "openclaw"


@pytest.mark.parametrize("failure", ["missing", "public", "symlink", "oversized", "malformed"])
def test_unqualified_scope_requires_private_bounded_binding_file(tmp_path, monkeypatch, failure):
    if failure != "missing":
        path = _bindings(tmp_path, monkeypatch, [_channel(SCOPE, "hermes")])
        if failure == "public":
            path.chmod(0o644)
        elif failure == "symlink":
            link = tmp_path / "link.json"
            link.symlink_to(path)
            monkeypatch.setenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", str(link))
        elif failure == "oversized":
            path.write_bytes(b" " * (64 * 1024 + 1))
        elif failure == "malformed":
            path.write_text("private-invalid-content")
    context = resolve_memory_benchmark_context(scope=SCOPE)
    assert context["ok"] is False
    assert context["reason"] == ("memory_benchmark_bindings_missing" if failure == "missing"
                                 else "memory_benchmark_bindings_unreadable")
    assert str(tmp_path) not in json.dumps(context)
    assert "private-invalid-content" not in json.dumps(context)


@pytest.mark.parametrize("workspace", ["workspace-one::channel::unsupported",
                                      "workspace-one::channel::hermes::channel::codex"])
def test_malformed_explicit_channel_scope_is_rejected(workspace):
    context = resolve_memory_benchmark_context(scope={**SCOPE, "workspace_id": workspace})
    assert context["ok"] is False
    assert context["reason"] == "memory_benchmark_channel_scope_invalid"


def test_invalid_configured_source_ids_are_not_expanded(monkeypatch):
    monkeypatch.setenv("EIMEMORY_SOURCE_IDS", "*")
    context = resolve_memory_benchmark_context(scope=_channel(SCOPE, "hermes"))
    assert context["ok"] is False
    assert context["reason"] == "memory_benchmark_source_ids_invalid"
