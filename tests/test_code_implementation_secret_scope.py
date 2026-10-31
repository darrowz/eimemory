"""Multiplexed Hermes: code-impl aux completion must bind a secret scope."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from eimemory.adapters.hermes.code_implementation import (
    CodeImplementationError,
    CodeImplementationSocketServer,
)


class _FakeToken:
    pass


def test_complete_installs_secret_scope_when_multiplex_unscoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_calls: list[dict[str, Any]] = []
    reset_calls: list[Any] = []

    fake_scope = {"HERMES_CODEX_BASE_URL": "https://example.invalid"}

    def build_profile_secret_scope(home):  # noqa: ANN001
        assert str(home).endswith(".hermes") or "hermes" in str(home).lower()
        return fake_scope

    def set_secret_scope(secrets, *, profile_home=None):  # noqa: ANN001
        set_calls.append({"secrets": secrets, "profile_home": profile_home})
        return _FakeToken()

    def reset_secret_scope(token):  # noqa: ANN001
        reset_calls.append(token)

    secret_mod = SimpleNamespace(
        build_profile_secret_scope=build_profile_secret_scope,
        current_secret_scope=lambda: None,
        is_multiplex_active=lambda: True,
        reset_secret_scope=reset_secret_scope,
        set_secret_scope=set_secret_scope,
    )
    monkeypatch.setitem(__import__("sys").modules, "agent.secret_scope", secret_mod)
    monkeypatch.setitem(__import__("sys").modules, "agent", SimpleNamespace(secret_scope=secret_mod))

    profile_mod = MagicMock()
    profile_mod.native_context.return_value = {
        "native": True,
        "profile_name": "default",
        "hermes_home": "/home/darrow/.hermes",
    }
    monkeypatch.setitem(
        __import__("sys").modules,
        "eimemory.adapters.hermes.profile_context",
        profile_mod,
    )

    parsed = {
        "schema": "code_implementation_response.v2",
        "file_updates": [],
        "summary": "noop",
        "rationale": "test",
    }
    # Minimal valid response is hard; stub validate_request/response/build_attestation
    import eimemory.adapters.hermes.code_implementation as mod

    monkeypatch.setattr(
        mod,
        "validate_request",
        lambda request: {
            "schema": "code_implementation_request.v2",
            "nonce": "a" * 32,
            "incident_id": "inc_test",
            "acceptance": ["noop"],
            "files": [{"path": "x.py", "content": "print(1)\n"}],
        },
    )
    monkeypatch.setattr(
        mod,
        "validate_response",
        lambda candidate, request=None: {
            "schema": "code_implementation_response.v2",
            "file_updates": [{"path": "x.py", "content": "print(2)\n"}],
            "summary": "noop",
            "rationale": "test",
        },
    )
    monkeypatch.setattr(
        mod,
        "build_attestation",
        lambda *a, **k: {"schema": "code_implementation_attestation.v2", "ok": True},
    )
    monkeypatch.setattr(mod, "_response_json_schema", lambda request: {"type": "object"})
    monkeypatch.setattr(mod, "canonical_json", lambda obj: "{}")

    llm = SimpleNamespace(
        complete_structured=MagicMock(
            return_value=SimpleNamespace(
                parsed=parsed,
                audit={"task": "eimemory_code_implementation"},
                provider="openai-codex",
                model="gpt-test",
                agent_id="default",
            )
        )
    )
    server = CodeImplementationSocketServer(SimpleNamespace(llm=llm))
    result = server._complete({"nonce": "a" * 32})
    assert result["ok"] is True
    assert len(set_calls) == 1
    assert set_calls[0]["secrets"] == fake_scope
    assert set_calls[0]["profile_home"] == "/home/darrow/.hermes"
    assert len(reset_calls) == 1
    llm.complete_structured.assert_called_once()


def test_complete_skips_scope_when_already_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    set_calls: list[Any] = []

    secret_mod = SimpleNamespace(
        build_profile_secret_scope=lambda home: {},
        current_secret_scope=lambda: {"HERMES_CODEX_BASE_URL": "https://example.invalid"},
        is_multiplex_active=lambda: True,
        reset_secret_scope=lambda token: None,
        set_secret_scope=lambda secrets, *, profile_home=None: set_calls.append(1) or _FakeToken(),
    )
    monkeypatch.setitem(__import__("sys").modules, "agent.secret_scope", secret_mod)
    monkeypatch.setitem(__import__("sys").modules, "agent", SimpleNamespace(secret_scope=secret_mod))

    import eimemory.adapters.hermes.code_implementation as mod

    monkeypatch.setattr(
        mod,
        "validate_request",
        lambda request: {
            "schema": "code_implementation_request.v2",
            "nonce": "b" * 32,
            "incident_id": "inc_test",
            "acceptance": ["noop"],
            "files": [{"path": "x.py", "content": "print(1)\n"}],
        },
    )
    monkeypatch.setattr(
        mod,
        "validate_response",
        lambda candidate, request=None: {
            "schema": "code_implementation_response.v2",
            "file_updates": [{"path": "x.py", "content": "print(2)\n"}],
            "summary": "noop",
            "rationale": "test",
        },
    )
    monkeypatch.setattr(
        mod,
        "build_attestation",
        lambda *a, **k: {"schema": "code_implementation_attestation.v2", "ok": True},
    )
    monkeypatch.setattr(mod, "_response_json_schema", lambda request: {"type": "object"})
    monkeypatch.setattr(mod, "canonical_json", lambda obj: "{}")

    llm = SimpleNamespace(
        complete_structured=MagicMock(
            return_value=SimpleNamespace(
                parsed={"schema": "code_implementation_response.v2"},
                audit={},
                provider="x",
                model="y",
                agent_id="z",
            )
        )
    )
    server = CodeImplementationSocketServer(SimpleNamespace(llm=llm))
    assert server._complete({})["ok"] is True
    assert set_calls == []
