from __future__ import annotations

from dataclasses import replace
import importlib
import re
from pathlib import Path

import pytest

import eimemory.capabilities.code_implementation_bootstrap as bootstrap_module
import eimemory.ops.code_implementation_owner as owner_module
from eimemory.adapters.hermes.code_implementation import (
    BINDING_ID,
    IMPLEMENTATION_DIGEST,
    PROVIDER_INSTANCE_ID,
    REVISION_ID,
)
from eimemory.api.runtime import Runtime
from eimemory.governance.l5_assessment_v3 import _adapter_readiness
from eimemory.models.records import ScopeRef
from eimemory.ops.code_implementation_owner import (
    CODE_IMPLEMENTATION_REFRESH_SERVICE,
    CODE_IMPLEMENTATION_REFRESH_TIMER,
    PRODUCTION_RUNTIME_SCOPE,
    inspect_code_implementation_owner,
    refresh_code_implementation_owner,
)


STAMP = "2026-08-23T00:00:00Z"


class _HealthyClient:
    def health(self, *, nonce: str) -> dict[str, object]:
        return {
            "ok": True,
            "operation": "health",
            "nonce": nonce,
            "provider_instance_id": PROVIDER_INSTANCE_ID,
            "implementation_digest": IMPLEMENTATION_DIGEST,
        }


def _seed(root: Path) -> None:
    runtime = Runtime.create(root=root)
    try:
        runtime.apply_capability_seed_manifest(scope=PRODUCTION_RUNTIME_SCOPE)
    finally:
        runtime.close()


def test_refresh_uses_only_eimemory_root_and_leaves_legacy_store_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = tmp_path / "production-authority"
    legacy = tmp_path / "legacy-openclaw-store"
    _seed(authority)
    _seed(legacy)
    monkeypatch.setenv("EIMEMORY_ROOT", str(authority))
    monkeypatch.setattr(bootstrap_module, "CodeImplementationSocketClient", _HealthyClient)

    result = refresh_code_implementation_owner(now=STAMP)

    assert result["ok"] is True
    assert result["authority_root"] == str(authority.resolve())
    assert result["registration"]["revision_id"] == REVISION_ID
    assert result["advertisement"]["binding_id"] == BINDING_ID
    assert result["advertisement"]["manual_bootstrap"] is True
    assert result["advertisement"]["qualifying"] is False

    production_runtime = Runtime.create(root=authority)
    legacy_runtime = Runtime.create(root=legacy)
    try:
        production_context = production_runtime.capabilities.incubation_context(
            "code.implementation",
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
        )
        legacy_context = legacy_runtime.capabilities.incubation_context(
            "code.implementation",
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
        )
        production_v1_events = production_runtime.capabilities.list_lifecycle_events(
            entity_type="revision",
            entity_id="code.implementation:v1",
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
            limit=8,
        )
    finally:
        production_runtime.close()
        legacy_runtime.close()

    assert REVISION_ID in {
        row["entity_id"] for row in production_context["revisions"]
    }
    assert {
        row["entity_id"] for row in production_context["revisions"]
    } == {REVISION_ID}
    assert BINDING_ID in {
        row["entity_id"] for row in production_context["bindings"]
    }
    assert REVISION_ID not in {row["entity_id"] for row in legacy_context["revisions"]}
    assert legacy_context["bindings"] == []
    assert production_v1_events[-1]["status"] == "deprecated"
    assert production_v1_events[-1]["provenance"]["manual_bootstrap"] is True
    assert production_v1_events[-1]["provenance"]["qualifying"] is False


def test_refresh_creates_durable_adapter_readiness_and_expiry_remains_truthful(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = tmp_path / "production-authority"
    _seed(authority)
    monkeypatch.setenv("EIMEMORY_ROOT", str(authority))
    monkeypatch.setattr(bootstrap_module, "CodeImplementationSocketClient", _HealthyClient)
    assert refresh_code_implementation_owner(now=STAMP)["ok"] is True

    runtime = Runtime.create(root=authority)
    try:
        ready = _adapter_readiness(
            runtime.store,
            ScopeRef.from_dict(PRODUCTION_RUNTIME_SCOPE),
            "global",
            at_time="2026-08-23T00:30:00Z",
        )
        expired = _adapter_readiness(
            runtime.store,
            ScopeRef.from_dict(PRODUCTION_RUNTIME_SCOPE),
            "global",
            at_time="2026-08-23T01:00:00Z",
        )
    finally:
        runtime.close()

    assert ready == {"hermes.code-implementation": "ready"}
    assert expired == {"adapter_registry": "unknown"}


def test_refresh_retries_a_transient_provider_socket_startup_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = tmp_path / "production-authority"
    _seed(authority)
    calls = 0

    class _StartingClient(_HealthyClient):
        def health(self, *, nonce: str) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise bootstrap_module.CodeImplementationError(
                    "provider_transport_unavailable"
                )
            return super().health(nonce=nonce)

    monkeypatch.setenv("EIMEMORY_ROOT", str(authority))
    monkeypatch.setattr(bootstrap_module, "CodeImplementationSocketClient", _StartingClient)
    monkeypatch.setattr(bootstrap_module.time, "sleep", lambda _seconds: None)

    result = refresh_code_implementation_owner(now=STAMP)

    assert result["ok"] is True
    assert calls == 2


def test_refresh_retry_is_idempotent_for_the_same_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = tmp_path / "production-authority"
    _seed(authority)
    monkeypatch.setenv("EIMEMORY_ROOT", str(authority))
    monkeypatch.setattr(bootstrap_module, "CodeImplementationSocketClient", _HealthyClient)

    first = refresh_code_implementation_owner(now=STAMP)
    second = refresh_code_implementation_owner(now=STAMP)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["registration"]["revision_receipt"]["idempotent"] is True
    assert second["registration"]["binding_receipt"]["idempotent"] is True
    assert second["registration"]["legacy_revision_transition"] is None
    assert second["advertisement"]["mutation"]["idempotent"] is True

    runtime = Runtime.create(root=authority)
    try:
        advertisements = runtime.capabilities.list_adapter_advertisements(
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
            binding_id=BINDING_ID,
            fresh_at="2026-08-23T00:30:00Z",
            limit=10,
        )
    finally:
        runtime.close()
    assert len(advertisements) == 1


def test_refresh_versions_revision_and_binding_when_prior_v8_digest_is_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = tmp_path / "production-authority"
    _seed(authority)
    prior_digest = "a" * 64
    current_revision = bootstrap_module.code_implementation_revision()
    current_binding = bootstrap_module.code_implementation_binding()
    prior_revision = replace(
        current_revision,
        revision_id="code.implementation:v8",
        contract={
            **current_revision.contract,
            "evidence_requirements": {
                **current_revision.contract["evidence_requirements"],
                "implementation_digest": prior_digest,
            },
        },
    )
    prior_binding = replace(
        current_binding,
        binding_id="binding.hermes.code-implementation:v8",
        capability_revision_id=prior_revision.revision_id,
        implementation_digest=prior_digest,
        environment_fingerprint={
            **current_binding.environment_fingerprint,
            "implementation_digest": prior_digest,
        },
        applicability={
            **current_binding.applicability,
            "revision_id": prior_revision.revision_id,
        },
    )
    runtime = Runtime.create(root=authority)
    try:
        runtime.capabilities.register_revision(
            prior_revision,
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            request_key="prior-v8-revision",
        )
        runtime.capabilities.bind(
            prior_binding,
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            request_key="prior-v8-binding",
        )
    finally:
        runtime.close()

    monkeypatch.setenv("EIMEMORY_ROOT", str(authority))
    monkeypatch.setattr(bootstrap_module, "CodeImplementationSocketClient", _HealthyClient)

    result = refresh_code_implementation_owner(now=STAMP)

    assert result["ok"] is True, result
    assert result["registration"]["revision_id"] == REVISION_ID
    assert result["registration"]["binding_id"] == BINDING_ID
    assert REVISION_ID != prior_revision.revision_id
    assert BINDING_ID != prior_binding.binding_id
    runtime = Runtime.create(root=authority)
    try:
        context = runtime.capabilities.incubation_context(
            "code.implementation",
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
        )
        prior_revision_events = runtime.capabilities.list_lifecycle_events(
            entity_type="revision",
            entity_id=prior_revision.revision_id,
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
            limit=8,
        )
    finally:
        runtime.close()
    bindings = {
        row["entity_id"]: row["descriptor"]
        for row in context["bindings"]
    }
    assert {row["entity_id"] for row in context["revisions"]} == {REVISION_ID}
    assert prior_revision_events[-1]["status"] == "deprecated"
    assert bindings[prior_binding.binding_id]["implementation_digest"] == prior_digest
    assert bindings[BINDING_ID]["implementation_digest"] == IMPLEMENTATION_DIGEST


def test_owner_status_reports_timer_catalog_and_fail_closed_effect_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = tmp_path / "production-authority"
    kill_switch = tmp_path / "code-evolution.disabled"
    policy = tmp_path / "code-automation-policy.v2.json"
    kill_switch.touch()
    _seed(authority)
    monkeypatch.setenv("EIMEMORY_ROOT", str(authority))
    monkeypatch.setattr(bootstrap_module, "CodeImplementationSocketClient", _HealthyClient)
    assert refresh_code_implementation_owner(now=STAMP)["ok"] is True
    assert kill_switch.exists()
    assert not policy.exists()

    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        unit = args[args.index("show") + 1]
        if unit == CODE_IMPLEMENTATION_REFRESH_TIMER:
            return "\n".join(
                (
                    "LoadState=loaded",
                    "ActiveState=active",
                    "SubState=waiting",
                    "UnitFileState=enabled",
                    "LastTriggerUSec=Sun 2026-08-23 00:00:00 UTC",
                    "NextElapseUSecRealtime=Sun 2026-08-23 00:20:00 UTC",
                    "Result=success",
                )
            )
        assert unit == CODE_IMPLEMENTATION_REFRESH_SERVICE
        return "\n".join(
            (
                "LoadState=loaded",
                "ActiveState=inactive",
                "SubState=dead",
                "UnitFileState=static",
                "Result=success",
            )
        )

    status = inspect_code_implementation_owner(
        checked_at="2026-08-23T00:30:00Z",
        runner=runner,
        kill_switch_path=kill_switch,
        automation_policy_path=policy,
    )

    assert status["authority"]["root"] == str(authority.resolve())
    assert status["authority"]["matches_runtime"] is True
    assert status["provider_health"]["ok"] is True
    assert status["advertisement"]["fresh"] is True
    assert status["catalog"]["required_passes"] == 2
    assert status["catalog"]["status"] == "waiting"
    assert status["catalog"]["sealed"] is True
    assert status["catalog"]["case_present"] is True
    assert status["catalog"]["executor_present"] is True
    assert status["catalog"]["structural_ready"] is True
    assert status["timer_owner"]["timer"]["active_state"] == "active"
    assert status["timer_owner"]["timer"]["unit_file_state"] == "enabled"
    assert status["safety"] == {
        "kill_switch_path": str(kill_switch),
        "kill_switch_present": True,
        "automation_policy_path": str(policy),
        "automation_policy_present": False,
        "effects_fail_closed": True,
    }
    assert {args[args.index("show") + 1] for args in calls} == {
        CODE_IMPLEMENTATION_REFRESH_TIMER,
        CODE_IMPLEMENTATION_REFRESH_SERVICE,
    }

    unprobed = inspect_code_implementation_owner(
        checked_at="2026-08-23T00:30:00Z",
        runner=runner,
        kill_switch_path=kill_switch,
        automation_policy_path=policy,
        probe_provider=False,
    )
    assert unprobed["provider_health"]["status"] == "not_probed"
    assert unprobed["refresh_ready"] is False


def test_release_owner_and_units_have_no_task_path_or_hardcoded_release_commit() -> None:
    paths = (
        Path("eimemory/ops/code_implementation_owner.py"),
        Path("deploy/systemd/eimemory-code-implementation-refresh.service"),
        Path("deploy/systemd/eimemory-code-implementation-refresh.timer"),
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert "/.hermes/tasks/" not in text
    assert "/home/darrow/.openclaw/memory/eimemory" not in text
    assert re.search(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])", text) is None
    assert "EIMEMORY_ROOT=/var/lib/eimemory" in text
    assert "/opt/eimemory/current/.venv/bin/eimemory ops code-implementation-refresh" in text


def test_refresh_cli_bypasses_legacy_settings_and_returns_nonzero_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_module = importlib.import_module("eimemory.cli.main")
    settings_called = False

    def forbidden_settings():
        nonlocal settings_called
        settings_called = True
        raise AssertionError("legacy settings must not select the provider authority")

    monkeypatch.setattr(cli_module, "load_settings", forbidden_settings)
    monkeypatch.setattr(
        owner_module,
        "refresh_code_implementation_owner",
        lambda: {
            "schema": "code.implementation.owner.v1",
            "ok": False,
            "status": "blocked",
            "reason": "provider_health_unavailable",
        },
    )

    exit_code = cli_module.main(["ops", "code-implementation-refresh", "--json"])

    assert exit_code == 1
    assert settings_called is False
    assert "provider_health_unavailable" in capsys.readouterr().out
