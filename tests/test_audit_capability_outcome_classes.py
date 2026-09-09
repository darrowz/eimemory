from copy import deepcopy

import pytest

from eimemory.governance.capability_attribution import attribute_capability_outcomes, collect_capability_evidence
from eimemory.governance.l5_readiness import _capability_outcome_evidence
from eimemory.governance.outcome_evidence import outcome_evidence
from eimemory.governance.capability_ledger import build_capability_ledger, record_capability_score
from eimemory.models.records import ScopeRef
from test_dynamic_capability_consumers import SCOPE, _runtime_with_catalog


@pytest.mark.parametrize("marker", [
    {"rehearsal": True}, {"outcome": {"rehearsal": True}},
    {"capability_contract": {"probe": True}}, {"evidence_class": "eval_probe"},
    {"provenance": {"evidence_type": "eval_probe"}},
])
def test_replay_markers_override_host_and_verification(marker):
    payload = {"source": "codex.stop", "rehearsal": False, "verifier": {"passed": True}, **marker}
    assert outcome_evidence(payload, require_host=True) == {"evidence_class": "replay", "production_eligible": False}


@pytest.mark.parametrize("payload", [
    {"verifier": {"passed": True}},
    {"source": "codex.stop", "verifier": {"passed": True}},
    {"source": "client.assertion", "rehearsal": False, "verifier": {"passed": True}},
    {"source": "codex.stop", "rehearsal": False, "verifier": {"passed": False}, "verification": "claimed"},
])
def test_capability_production_requires_explicit_verified_host(payload):
    assert outcome_evidence(payload, require_host=True)["production_eligible"] is False


def test_event_adapter_compatibility_does_not_relax_capability_trace_authority():
    payload = {"outcome": "good", "verifier": {"passed": True}}
    assert outcome_evidence(payload)["production_eligible"] is True
    assert outcome_evidence(payload, require_host=True)["production_eligible"] is False
    assert outcome_evidence({"outcome": "bad"})["production_eligible"] is False
    assert outcome_evidence({"outcome": "bad"}, require_host=True)["production_eligible"] is False


@pytest.mark.parametrize("legacy", [False, True])
def test_dynamic_probes_do_not_supply_production_outcome_counts(tmp_path, legacy):
    runtime, definition, _, _, profile, catalog = _runtime_with_catalog(tmp_path)
    for _ in range(3):
        report = runtime.run_capability_acceptance(scope=SCOPE, persist=True, catalog=catalog,
            profile_key=profile.profile_key, capability_scope="global", runtime_scope=SCOPE)
        assert report["ok"] is True
    evidence = collect_capability_evidence(runtime, scope=SCOPE, catalog=catalog)
    assert len(evidence[definition.capability_id]) == 3
    attribution = attribute_capability_outcomes(runtime, scope=SCOPE, catalog=catalog)
    assert attribution["record_count"] == 0
    assert all(item["evidence_class"] == "replay" and not item["production_eligible"]
               and item["evidence_tier"] != "T0" for item in evidence[definition.capability_id])
    counts = _capability_outcome_evidence(runtime, scope=ScopeRef.from_dict(SCOPE), limit=500,
        capability_selection={"capabilities": [{"capability_id": definition.capability_id, "requires_outcome": True}]},
        catalog=catalog, legacy_compatibility=legacy)
    assert counts["production_counts"][definition.capability_id] == 0
    assert counts["replay_counts"][definition.capability_id] == 3
    if not legacy:
        assert counts["counts"][definition.capability_id] == 0
        assert counts["missing"] == [definition.capability_id]
    runtime.close()


def test_verified_host_outcome_counts_but_unverified_trace_does_not(tmp_path):
    runtime, definition, _, _, profile, catalog = _runtime_with_catalog(tmp_path)
    report = runtime.run_capability_acceptance(scope=SCOPE, persist=True, catalog=catalog,
        profile_key=profile.profile_key, capability_scope="global", runtime_scope=SCOPE)
    trace = runtime.store.get_by_id(report["trace_record_ids"][0], scope=SCOPE)
    for verified in (False, True):
        payload = deepcopy(trace.content["payload"])
        payload.update(trace_id=f"production-{verified}", idempotency_key=f"production-{verified}", source="codex.stop")
        payload["outcome"]["rehearsal"] = False
        payload["capability_contract"]["probe"] = False
        payload["verifier"]["passed"] = verified
        result = runtime.record_outcome_trace(payload, scope=SCOPE, catalog=catalog)
        assert result["ok"] is True, result
        if verified:
            # A pre-fix score with the same attribution identity must not stop
            # a verified recalculation from persisting classified evidence.
            historical = record_capability_score(runtime, scope=SCOPE, loop_id="outcome_attribution",
                capability=definition.capability_id, score=0.82, evidence_record_ids=[result["record_id"]],
                evidence_sources=["outcome_trace"])
    attribution = attribute_capability_outcomes(runtime, scope=SCOPE, catalog=catalog)
    assert attribution["capabilities"][definition.capability_id]["evidence_count"] == 1
    counts = _capability_outcome_evidence(runtime, scope=ScopeRef.from_dict(SCOPE), limit=500,
        capability_selection={"capabilities": [{"capability_id": definition.capability_id, "requires_outcome": True}]},
        catalog=catalog, legacy_compatibility=False)
    assert counts["production_counts"][definition.capability_id] == 1
    assert counts["replay_counts"][definition.capability_id] == 1
    assert counts["unverified_counts"][definition.capability_id] == 1
    ledger = build_capability_ledger(runtime, scope=SCOPE, attribute_outcomes=False)
    assert ledger["capabilities"][definition.capability_id]["evidence_count"] == 1
    assert historical in [item["record_id"] for item in ledger["excluded_outcome_scores"]]
    scored = runtime.store.get_by_id(attribution["record_ids"][0], scope=SCOPE)
    assert scored.content["evidence_items"][0]["evidence_class"] == "verified_real_task"
    assert scored.content["evidence_items"][0]["production_eligible"] is True
    runtime.close()


@pytest.mark.parametrize("historical", [True, False])
def test_persisted_replay_score_cannot_leak_into_default_ledger(tmp_path, monkeypatch, historical):
    runtime, definition, _, _, profile, catalog = _runtime_with_catalog(tmp_path)
    report = runtime.run_capability_acceptance(scope=SCOPE, persist=True, catalog=catalog,
        profile_key=profile.profile_key, capability_scope="global", runtime_scope=SCOPE)
    if historical:
        score_id = record_capability_score(runtime, scope=SCOPE, loop_id="outcome_attribution",
            capability=definition.capability_id, score=0.82, evidence_record_ids=report["trace_record_ids"],
            evidence_sources=["outcome_trace"], evidence_tiers=["T0"])
    else:
        score_id = attribute_capability_outcomes(runtime, scope=SCOPE, catalog=catalog,
            legacy_compatibility=True)["record_ids"][0]
    def no_hydration(*args, **kwargs):
        raise AssertionError("ledger must use compact projection")
    monkeypatch.setattr(runtime.store, "get_by_id", no_hydration)
    ledger = build_capability_ledger(runtime, scope=SCOPE, attribute_outcomes=False)
    assert definition.capability_id not in ledger["capabilities"]
    assert ledger["record_count"] == 0
    assert ledger["excluded_outcome_scores"][0]["record_id"] == score_id
    legacy = build_capability_ledger(runtime, scope=SCOPE, attribute_outcomes=False, legacy_compatibility=True)
    assert legacy["capabilities"][definition.capability_id]["evidence_count"] == 1
    runtime.close()
