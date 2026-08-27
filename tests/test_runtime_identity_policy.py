from __future__ import annotations

from deploy.runtime_identity_policy import managed_dropin_name, verification_units


def test_extracted_policy_preserves_legacy_dropin_name_until_incident_fix() -> None:
    assert managed_dropin_name() == "90-eimemory-python-runtime.conf"


def test_extracted_policy_preserves_legacy_verification_subset() -> None:
    discovered = [
        "eimemory-rpc.service",
        "eimemory-nightly.service",
        "eimemory-learn-watch.service",
    ]

    assert verification_units(discovered, include_hermes=False) == (
        "eimemory-rpc.service",
        "eimemory-code-implementation-refresh.service",
        "openclaw-gateway.service",
        "openclaw-loop-watch.service",
    )
    assert verification_units(discovered, include_hermes=True)[-1] == "hermes-gateway.service"
