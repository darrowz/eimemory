from __future__ import annotations

import pytest

from deploy.runtime_identity_policy import managed_dropin_name, verification_units


def test_policy_uses_final_authority_dropin_name() -> None:
    assert managed_dropin_name() == "zzzz-eimemory-python-runtime.conf"


def test_policy_verifies_every_discovered_python_runtime_unit() -> None:
    discovered = [
        "eimemory-console.service",
        "eimemory-learn-dashboard.service",
        "eimemory-learn-think.service",
        "eimemory-learn-watch.service",
        "eimemory-nightly.service",
    ]

    selected = verification_units(discovered, include_hermes=False)

    assert selected == (
        "eimemory-console.service",
        "eimemory-learn-dashboard.service",
        "eimemory-learn-think.service",
        "eimemory-learn-watch.service",
        "eimemory-nightly.service",
        "openclaw-loop-watch.service",
    )
    assert set(discovered).issubset(selected)


def test_policy_preserves_baseline_anchor_without_duplication() -> None:
    discovered = [
        "eimemory-rpc.service",
        "openclaw-loop-watch.service",
        "openclaw-loop-watch.service",
    ]

    assert verification_units(discovered, include_hermes=False) == (
        "eimemory-rpc.service",
        "openclaw-loop-watch.service",
    )


def test_policy_adds_hermes_when_requested() -> None:
    selected = verification_units(
        ["eimemory-rpc.service"], include_hermes=True
    )

    assert selected == (
        "eimemory-rpc.service",
        "openclaw-loop-watch.service",
        "hermes-gateway.service",
    )


def test_policy_rejects_invalid_discovered_unit_names() -> None:
    with pytest.raises(ValueError, match="invalid systemd service unit"):
        verification_units(["not-a-service"], include_hermes=False)


def test_policy_excludes_only_unselected_openclaw_units() -> None:
    units = ["eimemory-rpc.service", "openclaw-gateway.service", "openclaw-loop-watch.service",
             "openclaw-loop-compact.service", "eimemory-code-implementation-refresh.service"]
    assert verification_units(units, include_hermes=True, include_openclaw=False) == (
        "eimemory-rpc.service", "eimemory-code-implementation-refresh.service", "hermes-gateway.service",
    )


def test_policy_optional_adapter_does_not_hide_malformed_names() -> None:
    with pytest.raises(ValueError, match="invalid systemd service unit"):
        verification_units(["openclaw-gateway.service;bad"], include_hermes=False, include_openclaw=False)


def test_policy_cli_accepts_installer_optional_adapter_flags(monkeypatch, capsys) -> None:
    import io
    from deploy.runtime_identity_policy import main
    monkeypatch.setattr("sys.stdin", io.StringIO("eimemory-rpc.service\nopenclaw-gateway.service\n"))
    assert main(["verification-units", "--exclude-openclaw", "--include-hermes"]) == 0
    assert capsys.readouterr().out.splitlines() == ["eimemory-rpc.service", "hermes-gateway.service"]
