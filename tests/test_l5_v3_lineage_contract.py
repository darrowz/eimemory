"""v3 reader lineage + version-truth source id (honrui 1.14.50 closure-65krmbgd).

The closure validated lineage rec_906420bdd686 (compatible) under the legacy
contract, but the v3 reader re-verified it only under the dynamic catalog and
reported ``current_lineage_incompatible`` from an older evidence-free record.
Outside the closure the version-truth probe also re-executed with a different
import path, so stored evidence mismatched.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from eimemory.governance.capability.capability_probe_executor import _release_source_id
from eimemory.governance.release.evidence_contract import ReleaseIdentity
from eimemory.models.records import ScopeRef

COMMIT = "5af82d4ef84a110bd4cb58f6b913e7fd23e8e3d6"


def test_version_truth_source_id_is_import_path_independent() -> None:
    closure = f"/opt/eimemory/releases/{COMMIT}/eimemory"
    service = f"/opt/eimemory/releases/{COMMIT}/.venv/lib/python3.14/site-packages/eimemory"
    assert _release_source_id(closure, COMMIT) == _release_source_id(service, COMMIT) == (
        f"/opt/eimemory/releases/{COMMIT}"
    )
    # Development checkouts without a commit-named directory keep the import root.
    assert _release_source_id("/src/eimemory", COMMIT) == "/src/eimemory"
    assert _release_source_id(closure, "") == closure


def _run(monkeypatch, lineage_by_contract):
    from eimemory.governance.l5_reader import build_l5_effective_report

    scope = ScopeRef(tenant_id="default", agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    release = ReleaseIdentity(commit=COMMIT, version="1.14.50", receipt_id="r", session_id="r")
    calls = []

    def lineage(_runtime, **kwargs):
        calls.append((kwargs["legacy_compatibility"], kwargs.get("catalog")))
        return dict(lineage_by_contract[kwargs["legacy_compatibility"]])

    monkeypatch.setattr("eimemory.governance.l5_assessment_v3.build_l5_assessment_v3",
                        lambda _rt, **kw: {"ok": True, "status": "ready", "loop_maturity": "evolving",
                                           "adapter_readiness": {"hermes": "ready"},
                                           "deployment_assurance": {"ok": None, "required": False, "blocking": False}})
    monkeypatch.setattr("eimemory.adapters.hermes.code_implementation.resolve_code_implementation_provider",
                        lambda *a, **k: {"ready": True, "advertisement_fresh": True})
    monkeypatch.setattr("eimemory.storage.code_evolution_store.CodeEvolutionStore",
                        lambda store: SimpleNamespace(list_transactions=lambda **kwargs: []))
    monkeypatch.setattr("eimemory.governance.evidence_contract.current_release_identity", lambda _rt, s: release)
    monkeypatch.setattr("eimemory.governance.release_lineage.current_release_lineage", lineage)
    report = build_l5_effective_report(SimpleNamespace(store=object()), scope=scope, repo_root="/repo",
                                       reader_mode="v3", profile_key="l5.default")
    return report, calls


def test_v3_reader_accepts_lineage_validated_under_recording_contract(monkeypatch) -> None:
    compatible = {"ok": True, "validated": True, "compatible": True, "record_id": "rec_906420bdd686",
                  "current_release": {"commit": COMMIT}}
    incompatible = {"ok": True, "validated": True, "compatible": False, "record_id": "rec_30f4f59c502d",
                    "current_release": {"commit": COMMIT}}
    report, calls = _run(monkeypatch, {False: incompatible, True: compatible})
    assert [legacy for legacy, _ in calls] == [False, True]
    assert calls[1][1] is None  # the legacy contract resolves its own catalog
    assert report["current_lineage"]["record_id"] == "rec_906420bdd686"
    assert report["current_lineage"]["lineage_contract"] == "legacy_compatibility"
    assert "current_lineage_incompatible" not in report["gaps"]
    # Nothing else is fabricated: there is still no qualifying transaction.
    assert report["product_l5_complete"] is False
    assert "no_qualifying_terminal_receipt" in report["gaps"]


@pytest.mark.parametrize("legacy", [
    {"ok": True, "validated": True, "compatible": False},
    {"ok": False, "error": "lineage_attestation_mismatch"},
    {"ok": True, "validated": False, "compatible": True},
])
def test_v3_reader_keeps_incompatible_when_no_contract_validates(monkeypatch, legacy) -> None:
    dynamic = {"ok": True, "validated": True, "compatible": False, "record_id": "dyn"}
    report, _ = _run(monkeypatch, {False: dynamic, True: legacy})
    assert report["current_lineage"]["record_id"] == "dyn"
    assert "current_lineage_incompatible" in report["gaps"]
