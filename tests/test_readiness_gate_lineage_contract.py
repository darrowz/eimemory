"""The v2 readiness gate re-verifies lineage under the contract it was recorded with.

Release closure records the lineage with ``legacy_compatibility=True``.  The
v2 gate (which itself requires ``legacy_compatibility``) used to re-verify via
the runtime provider without that flag, i.e. under the dynamic catalog.  The
recomputation then rejected the compatible attestation (replay evidence
"incomplete" under different rules) and fell back to an older record with no
gate evidence, failing ``release_lineage_compatible`` on honrui (1.14.47).
"""
from __future__ import annotations

from types import SimpleNamespace

from eimemory.contracts.release_identity import ReleaseIdentity, release_identity_payload
from eimemory.governance.l5.l5_readiness import readiness_gate_status_diagnostics

RELEASE = ReleaseIdentity(commit="4" * 40, version="1.14.47", receipt_id="rec_r", session_id="rec_r")
SCOPE = {"tenant_id": "default", "agent_id": "hongtu", "workspace_id": "embodied", "user_id": "u"}


def _runtime(calls: list[dict]):
    def lineage(**kwargs):
        calls.append(kwargs)
        if kwargs.get("legacy_compatibility") is True:
            return {"ok": True, "validated": True, "compatible": True, "record_id": "rec_current"}
        return {"ok": True, "validated": True, "compatible": False, "record_id": "rec_older"}

    return SimpleNamespace(
        current_release_identity=lambda **_kw: RELEASE,
        current_release_lineage=lineage,
    )


def _readiness() -> dict:
    return {
        "schema_version": "l5_readiness.v2",
        "legacy_compatibility": True,
        "scope": SCOPE,
        "release_identity": release_identity_payload(RELEASE),
        "release_lineage": {"ok": True, "validated": True, "compatible": True, "record_id": "rec_current"},
    }


def test_v2_gate_reverifies_lineage_under_legacy_contract() -> None:
    calls: list[dict] = []
    result = readiness_gate_status_diagnostics(
        _readiness(), runtime=_runtime(calls), scope=SCOPE, repo_root="/nonexistent",
    )
    assert calls and all(call.get("legacy_compatibility") is True for call in calls)
    passed = dict(result["conditions"])
    assert passed["release_lineage_compatible"] is True
    assert passed["release_lineage_matches_reported"] is True


def test_v2_gate_still_fails_closed_on_incompatible_lineage() -> None:
    calls: list[dict] = []
    runtime = _runtime(calls)
    runtime.current_release_lineage = lambda **kw: (
        calls.append(kw) or {"ok": True, "validated": True, "compatible": False, "record_id": "rec_x"}
    )
    result = readiness_gate_status_diagnostics(
        _readiness(), runtime=runtime, scope=SCOPE, repo_root="/nonexistent",
    )
    assert result["status"] != "L5"
    assert result["first_failed_condition"] == "release_lineage_compatible"
