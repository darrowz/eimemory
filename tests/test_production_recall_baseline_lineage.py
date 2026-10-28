"""Native release-chain authority must survive growth without trusting summaries."""
from __future__ import annotations

from copy import deepcopy

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation import real_query_gate as gate
from eimemory.evaluation.production_recall import run_production_recall_eval
from eimemory.models.records import RecallBundle, ScopeRef
from test_production_real_query_gate import (
    BASE_SCOPE, _append_records_and_label_evidence, _case, _digest,
    _receipt, _record, _refresh_dataset_evidence,
)


@pytest.fixture
def lineage(tmp_path, monkeypatch):
    """Persist an anchor and ten accepted releases using the real evaluator."""
    runtime = Runtime.create(root=tmp_path)
    memory = _record("lineage-hermes-memory", "hermes", "source-hermes")
    _append_records_and_label_evidence(runtime, {"hermes": memory})
    dataset = {
        "schema": gate.PRODUCTION_REAL_QUERY_SCHEMA,
        "dataset_kind": "production", "scope": dict(BASE_SCOPE),
        "cases": [_case("hermes", "source-hermes", memory.record_id, index=i)
                  for i in range(3)],
        "baseline_report_id": "",
    }
    _refresh_dataset_evidence(dataset)
    monkeypatch.setattr(runtime.memory, "recall", lambda **_: RecallBundle(
        items=[memory], rules=[], reflections=[], confidence=1.0,
        next_action_hint="", explanation={"fusion": {"policy_version": "rrf-page-pool.v1"}},
    ))
    releases = []
    previous = _receipt(runtime, commit=f"{1:040x}", version="1.14.100", prior_commit=f"{0:040x}")
    releases.append(previous)
    runtime._test_runtime_commit = previous.commit
    monkeypatch.setattr(gate, "_verified_live_prior_release", lambda *_, **__: (previous, ""))
    bootstrap = gate.bootstrap_production_recall_baseline(
        runtime, dataset, candidate_commit=f"{2:040x}", prior_commit=previous.commit,
        persist_report=True,
    )
    assert bootstrap["bootstrap_status"] == "anchor_ready", bootstrap
    nodes = [runtime.store.get_by_id(bootstrap["persisted_record_id"], scope=BASE_SCOPE)]
    for index in range(2, 12):
        dataset["baseline_report_id"] = nodes[-1].record_id
        _refresh_dataset_evidence(dataset)
        release = _receipt(runtime, commit=f"{index:040x}", version=f"1.14.{99 + index}",
                           prior_commit=releases[-1].commit)
        releases.append(release)
        runtime._test_runtime_commit = release.commit
        report = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)
        assert report["accepted"] is True, report
        nodes.append(runtime.store.get_by_id(report["persisted_record_id"], scope=BASE_SCOPE))
    current = _receipt(runtime, commit=f"{12:040x}", version="1.14.111",
                       prior_commit=releases[-1].commit)
    runtime._test_runtime_commit = current.commit
    dataset["baseline_report_id"] = nodes[-1].record_id
    _refresh_dataset_evidence(dataset)
    frozen = gate.freeze_production_recall_dataset(dataset)
    try:
        yield {"runtime": runtime, "dataset": dataset, "frozen": frozen,
               "nodes": nodes, "releases": releases, "current": current}
    finally:
        runtime.close()


def _resolve(lineage):
    return gate._resolve_trusted_baseline(
        lineage["runtime"], scope=ScopeRef.from_dict(BASE_SCOPE),
        report_id=lineage["nodes"][-1].record_id,
        dataset_digest=lineage["frozen"]["dataset_digest"],
        current_release=lineage["current"],
        dataset_evidence=lineage["frozen"]["secure_dataset_evidence"],
    )


def _rewrite_report(runtime, node, *, refresh_digest=True):
    if refresh_digest:
        node.meta["report_payload_digest"] = _digest(node.content["report"])
    runtime.store.rewrite(node)


def _failed_attempt(runtime, node):
    report = deepcopy(node.content["report"])
    report.update({"accepted": False, "gate_status": "blocked",
                   "report_id": "prg_failed_" + node.record_id,
                   "blocked_reason": "production_recall_gate_failed"})
    report["threshold_gate"] = {**report["threshold_gate"], "ok": False,
                                "blocking_metrics": {"recall_at_5": {"actual": 0.0}}}
    return gate._persist_eligible_high_water(
        runtime, report, scope=ScopeRef.from_dict(BASE_SCOPE), persist=True,
    )


def test_native_long_lineage_and_formal_gate_validate_every_ancestor(lineage, monkeypatch):
    original = gate._independent_real_query_metrics_valid
    validated = []

    def checked(report, **kwargs):
        validated.append(report["attempt_id"])
        return original(report, **kwargs)

    monkeypatch.setattr(gate, "_independent_real_query_metrics_valid", checked)
    baseline, reason = _resolve(lineage)
    assert reason == ""
    assert baseline["persisted_record_id"] == lineage["nodes"][-1].record_id
    assert validated == [node.record_id for node in lineage["nodes"]]
    report = run_production_recall_eval(
        lineage["runtime"], lineage["dataset"], seed=False, persist_report=True,
    )
    assert report["accepted"] is True
    assert report["sample_count"] == 3
    assert gate.verify_current_production_recall_gate(lineage["runtime"], scope=BASE_SCOPE)["ok"] is True


@pytest.mark.parametrize("mutation,reason", [
    ("payload", "baseline_report_payload_tampered"),
    ("source_partition", "baseline_report_untrusted"),
    ("scope", "baseline_report_scope_mismatch"),
    ("release_authority", "baseline_deployment_receipt_invalid"),
    ("physical_attempt", "baseline_physical_attempt_required"),
    ("dataset", "baseline_report_contract_invalid"),
    ("metrics", "baseline_metrics_invalid"),
    ("parent_identity_extra", "baseline_parent_identity_mismatch"),
    ("parent_identity_missing", "baseline_parent_identity_mismatch"),
    ("parent_identity_value", "baseline_parent_identity_mismatch"),
])
def test_deep_forgery_fails_closed_with_recomputed_payload_digest(lineage, monkeypatch, mutation, reason):
    # This accepted ancestor lies beyond the former eight-edge limit.
    runtime, node = lineage["runtime"], lineage["nodes"][1]
    report = node.content["report"]
    if mutation == "source_partition":
        node.source_id = "another-source"
        lookup = runtime.store.latest_record_by_meta_value_exact_scope

        def wrong_partition(**kwargs):
            result = lookup(**kwargs)
            return node if result is not None and result.record_id == node.record_id else result

        monkeypatch.setattr(runtime.store, "latest_record_by_meta_value_exact_scope", wrong_partition)
    elif mutation == "scope":
        report["scope"]["agent_id"] = "another-agent"
    elif mutation == "release_authority":
        report["release_identity"]["release_session_id"] = "another-session"
    elif mutation == "physical_attempt":
        report["attempt_id"] = "prga_forged"
    elif mutation == "dataset":
        report["dataset_digest"] = "f" * 64
    elif mutation in {"metrics", "payload"}:
        report["metrics"]["recall_at_5"] = 0.5
    elif mutation == "parent_identity_extra":
        report["baseline_identity"]["unverified_claim"] = True
    elif mutation == "parent_identity_missing":
        del report["baseline_identity"]["engine_digest"]
    elif mutation == "parent_identity_value":
        report["baseline_identity"]["engine_digest"] = "f" * 64
    if mutation != "source_partition":
        _rewrite_report(runtime, node, refresh_digest=mutation != "payload")
    baseline, actual = _resolve(lineage)
    assert baseline is None
    assert actual.removeprefix("baseline_parent_invalid:") == reason


def test_deep_native_release_cycle_is_detected_before_reusing_an_authority(lineage):
    runtime = lineage["runtime"]
    oldest_accepted = lineage["nodes"][1]
    receipt = runtime.store.get_by_id(lineage["releases"][1].receipt_id, scope=BASE_SCOPE)
    side_effect = receipt.content["side_effect"]
    side_effect["verification"]["prior_commit"] = lineage["releases"][-1].commit
    side_effect["rollback_evidence"]["prior_commit_sha"] = lineage["releases"][-1].commit
    runtime.store.rewrite(receipt)
    # All native node contracts still hold, but its predecessor now closes a ring.
    oldest_accepted.content["report"]["baseline_identity"]["persisted_record_id"] = lineage["nodes"][-1].record_id
    _rewrite_report(runtime, oldest_accepted)
    baseline, reason = _resolve(lineage)
    assert baseline is None
    assert reason == "baseline_chain_cycle"


def test_deep_failed_high_water_supersedes_an_older_accepted_attempt(lineage):
    _failed_attempt(lineage["runtime"], lineage["nodes"][1])
    baseline, reason = _resolve(lineage)
    assert baseline is None
    assert reason == "baseline_parent_invalid:baseline_report_not_latest_prior_high_water"


def test_failed_ancestor_inserted_during_validation_invalidates_the_snapshot(lineage, monkeypatch):
    original = gate._independent_real_query_metrics_valid
    validated = []

    def concurrent_attempt(report, **kwargs):
        valid = original(report, **kwargs)
        validated.append(report["attempt_id"])
        if len(validated) == 1:
            _failed_attempt(lineage["runtime"], lineage["nodes"][1])
        return valid

    monkeypatch.setattr(gate, "_independent_real_query_metrics_valid", concurrent_attempt)
    baseline, reason = _resolve(lineage)
    assert baseline is None
    assert reason == "baseline_high_water_changed"
    assert len(validated) == len(lineage["nodes"])


@pytest.mark.parametrize("mutation", ["payload", "source_partition"])
def test_same_id_cached_mutation_cannot_change_the_loaded_authority(lineage, monkeypatch, mutation):
    runtime = lineage["runtime"]
    original_lookup = runtime.store.latest_record_by_meta_value_exact_scope
    original_validate = gate._independent_real_query_metrics_valid
    cache = {}
    validated = []

    def cached_lookup(**kwargs):
        record = original_lookup(**kwargs)
        if record is None:
            return None
        return cache.setdefault(record.record_id, record)

    def concurrent_rewrite(report, **kwargs):
        valid = original_validate(report, **kwargs)
        validated.append(report["attempt_id"])
        if len(validated) == len(lineage["nodes"]):
            cached = cache[lineage["nodes"][1].record_id]
            if mutation == "payload":
                cached.content["report"]["generated_at"] = "2026-10-18T00:00:00+00:00"
                _rewrite_report(runtime, cached)
            else:
                cached.source_id = "another-source"
        return valid

    monkeypatch.setattr(runtime.store, "latest_record_by_meta_value_exact_scope", cached_lookup)
    monkeypatch.setattr(gate, "_independent_real_query_metrics_valid", concurrent_rewrite)
    baseline, reason = _resolve(lineage)
    assert baseline is None
    assert reason == "baseline_high_water_changed"


def _corrupt_contract_invalid_high_water(runtime, tip):
    """Append a newer active HW tip with half-present engine fields (contract_invalid)."""
    corrupt = deepcopy(tip)
    corrupt.record_id = "prga_contract_invalid_" + tip.record_id[-12:]
    report = deepcopy(tip.content["report"])
    report["attempt_id"] = corrupt.record_id
    report["report_id"] = "prg_contract_invalid_" + tip.record_id[-12:]
    # Half-present engine identity: present keys that fail digest/flag checks.
    report["engine_identity_valid"] = False
    report["engine_digest"] = "0" * 64
    report["engine_identity"] = {"broken": True}
    corrupt.content = {"report": report}
    corrupt.meta = dict(tip.meta)
    corrupt.meta["report_payload_digest"] = gate._stable_digest(report)
    corrupt.status = "active"
    runtime.store.append(corrupt)
    return corrupt


def test_contract_invalid_newest_high_water_falls_back_to_older_qualified(lineage):
    runtime = lineage["runtime"]
    tip = lineage["nodes"][-1]
    corrupt = _corrupt_contract_invalid_high_water(runtime, tip)
    assert runtime.store.latest_record_by_meta_value_exact_scope(
        kind="reflection",
        source="eimemory.evaluation.production_recall",
        status="active",
        scope=ScopeRef.from_dict(BASE_SCOPE),
        meta_key="baseline_high_water_key",
        meta_value=tip.meta["baseline_high_water_key"],
    ).record_id == corrupt.record_id
    baseline, reason = _resolve(lineage)
    assert reason == ""
    assert baseline is not None
    assert baseline["persisted_record_id"] == tip.record_id


def test_contract_invalid_tip_without_pointed_id_still_falls_back(lineage):
    runtime = lineage["runtime"]
    tip = lineage["nodes"][-1]
    _corrupt_contract_invalid_high_water(runtime, tip)
    baseline, reason = gate._resolve_trusted_baseline(
        runtime,
        scope=ScopeRef.from_dict(BASE_SCOPE),
        report_id="",
        dataset_digest=lineage["frozen"]["dataset_digest"],
        current_release=lineage["current"],
        dataset_evidence=lineage["frozen"]["secure_dataset_evidence"],
    )
    assert reason == ""
    assert baseline is not None
    assert baseline["persisted_record_id"] == tip.record_id
