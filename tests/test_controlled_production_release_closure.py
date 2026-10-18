"""Controlled release acceptance has its own authority and operational verdict."""
from copy import deepcopy
from types import ModuleType, SimpleNamespace
import sys

import pytest

from eimemory.governance.release import release_closure as closure
from eimemory.governance.release.release_closure import _wait_for_controlled_window
from eimemory.governance.release.closure_contracts import controlled_production_closure_report_ok
from eimemory.governance.release.closure_verdict import summarize_release_closure
from eimemory.ops.controlled_stability import assess_controlled_stability as assess_stability
from eimemory.ops import controlled_rpc_probe
from eimemory.ei_bridge.protocol import EIMEMORY_RPC_CONTRACT_VERSION
from release_report_fixtures import live, receipt, replay
from test_controlled_stability import samples as stability_samples, seal as seal_stability_sample


SCOPE = dict(tenant_id="default", agent_id="primary", workspace_id="production", user_id="owner")


def controlled_acceptance():
    deployed = receipt()
    return {
        "ok": True, "accepted": True, "status": "accepted", "schema": "controlled_production_acceptance.v1",
        "mode": "controlled_existing_memory", "evidence_class": "operator_controlled_existing_memory",
        "certifies_natural_queries": False, "certifies_no_answer": False, "natural_samples_created": 0,
        "host_delivery_certified": False, "memory_contribution_certified": False, "persisted": True,
        "record_id": "controlled-current", "policy_digest": "d" * 64,
        "release_identity": {"release_commit": deployed["commit"],
                             "release_version": deployed["version"],
                             "deployment_receipt_id": deployed["promotion_request_id"],
                             "release_session_id": deployed["release_session_id"]},
        "agents": [{"agent_id": agent, "scope_digest": str(index) * 64,
                    "source_ids": ["hermes"], "sample_count": 3, "pass_count": 3, "fail_count": 0,
                    "complete": True, "accepted": True, "blocked_reason": ""}
                   for index, agent in enumerate(("primary", "colleague"), start=1)],
    }


def stability():
    observations = stability_samples()
    for sample in observations:
        sample["expected_commit"] = receipt()["commit"]
        seal_stability_sample(sample)
    return assess_stability(observations, expected_commit=receipt()["commit"])


def rpc_probe():
    controlled = controlled_acceptance()
    endpoint = {"schema": "controlled_rpc_release_identity.v1", "ok": True,
        "expected_commit": receipt()["commit"], "version": receipt()["version"],
        "package_tree_digest": stability()["samples"][0]["health"]["package_tree_digest"],
        "identity_only": True, "retrieval_health_certified": False}
    return {"schema": "controlled_production_rpc_probe.v1", "ok": True,
        "expected_commit": receipt()["commit"], "maintenance_only": True,
        "natural_samples_created": 0, "certifies_natural_queries": False,
        "policy_digest": controlled["policy_digest"], "release_identity": controlled["release_identity"],
        "rpc_contract_version": EIMEMORY_RPC_CONTRACT_VERSION, "endpoint_release_verified": True,
        "endpoint_identity_before": deepcopy(endpoint), "endpoint_identity_after": deepcopy(endpoint),
        "sample_count": len(controlled["agents"]), "pass_count": len(controlled["agents"]),
        "cases": [{**{key: agent[key] for key in ("agent_id", "scope_digest", "source_ids")},
                   "passed": True, "collection_complete": True, "no_fallback": True,
                   "budgets_clear": True, "target_present": True, "exact_boundary_verified": True,
                   "target_version_unchanged": True, "target_in_main_items": True,
                   "rpc_contract_verified": True, "context_digest": "d" * 64,
                   "target_binding_digest": "e" * 64, "target_version_digest": "f" * 64,
                   "query_digest": "c" * 64}
                  for agent in controlled["agents"]]}


def complete_report():
    deployed = receipt()
    placeholder = {"ok": False, "status": "not_run", "reason": "upstream_gate_not_run"}
    return {
        "report_type": "controlled_production_release_closure", "ok": True,
        "closure_complete": True, "data_accumulating": False, "scope": dict(SCOPE),
        "acceptance_basis": "controlled_existing_memory", "evidence_class": "operator_controlled_existing_memory",
        "closure_scope": "operational_only", "certifies_natural_queries": False,
        "certifies_no_answer": False, "natural_certification": "not_certified",
        "l5_certification": "not_certified", "natural_samples_created": 0,
        "blocked_stage": "", "blocked_reason": "", "deployment": deployed,
        "deployment_receipt": deployed, "storage_migrations": {"ok": True},
        "controlled_production_acceptance": controlled_acceptance(),
        "rpc_controlled_probe": rpc_probe(),
        "record_ids": {"controlled_production_acceptance": "controlled-current"},
        "production_recall_gate": deepcopy(placeholder), "production_recall_strict_state": deepcopy(placeholder),
        "readiness": deepcopy(placeholder), "closure_rehearsal": deepcopy(placeholder),
        "replay_bootstrap": replay(), "live_acceptance": live(),
        "channel_acceptance": {"ok": True, "record_id": "channel-current",
                               "evidence_class": "external_channel_receipt", "deployment_commit": deployed["commit"],
                               "promotion_request_id": deployed["promotion_request_id"],
                               "release_session_id": deployed["release_session_id"]},
        "stability": stability(),
    }


def summary(report, **execution):
    return summarize_release_closure(report, execution={"expected_commit": receipt()["commit"],
        "expected_scope": SCOPE, "closure_exit_status": 0, **execution})


def test_operational_success_is_not_natural_quality_or_l5_certification():
    report = complete_report()
    result = summary(report)
    assert controlled_production_closure_report_ok(report)
    assert result["ok"] is True and result["contract_ok"] is True and result["closure_complete"] is True
    assert result["ordinary_release_admission"] == "admitted"
    assert result["production_quality"] == "controlled_accepted"
    assert result["l5_certification"] == "not_certified" and result["l5_certified"] is False
    assert result["certifies_natural_queries"] is False and result["long_term_stability_certified"] is False
    assert result["closure_scope"] == "operational_only"


@pytest.mark.parametrize("path,value", [
    (("certifies_natural_queries",), True), (("certifies_no_answer",), True),
    (("l5_certification",), "certified"), (("natural_samples_created",), 1),
    (("closure_scope",), "l5"), (("controlled_production_acceptance", "accepted"), False),
    (("controlled_production_acceptance", "certifies_natural_queries"), True),
    (("controlled_production_acceptance", "schema"), "foreign_contract"),
    (("controlled_production_acceptance", "status"), "not_run"),
    (("controlled_production_acceptance", "persisted"), False),
    (("controlled_production_acceptance", "host_delivery_certified"), True),
    (("controlled_production_acceptance", "memory_contribution_certified"), True),
    (("controlled_production_acceptance", "policy_digest"), "untrusted"),
    (("controlled_production_acceptance", "release_identity", "release_session_id"), "foreign-session"),
    (("controlled_production_acceptance", "release_identity", "release_version"), "old-version"),
    (("controlled_production_acceptance", "agents", 0, "complete"), False),
    (("controlled_production_acceptance", "agents", 0, "accepted"), False),
    (("controlled_production_acceptance", "agents", 0, "sample_count"), 2),
    (("controlled_production_acceptance", "agents", 0, "fail_count"), 1),
    (("controlled_production_acceptance", "agents", 0, "pass_count"), True),
    (("controlled_production_acceptance", "agents", 0, "source_ids"), []),
    (("controlled_production_acceptance", "agents", 1, "scope_digest"), "1" * 64),
    (("rpc_controlled_probe", "ok"), False),
    (("rpc_controlled_probe", "cases", 0, "collection_complete"), False),
    (("rpc_controlled_probe", "cases", 0, "no_fallback"), False),
    (("rpc_controlled_probe", "release_identity", "release_session_id"), "foreign"),
    (("rpc_controlled_probe", "cases"), []),
    (("rpc_controlled_probe", "rpc_contract_version"), "wrong"),
    (("rpc_controlled_probe", "endpoint_release_verified"), False),
    (("rpc_controlled_probe", "endpoint_identity_before", "expected_commit"), "f" * 40),
    (("rpc_controlled_probe", "endpoint_identity_after", "package_tree_digest"), "b" * 64),
    (("rpc_controlled_probe", "cases", 0, "target_in_main_items"), False),
    (("rpc_controlled_probe", "cases", 0, "rpc_contract_verified"), False),
    (("rpc_controlled_probe", "cases", 0, "target_binding_digest"), "invalid"),
    (("stability", "ok"), False), (("stability", "interval_seconds"), 59.9),
    (("stability", "interval_seconds"), float("nan")),
    (("stability", "samples"), []), (("stability", "sample_count"), 3),
    (("stability", "assessed_at"), "not-a-timestamp"),
    (("stability", "samples", 1, "health", "ok"), False),
    (("stability", "expected_commit"), "f" * 40), (("stability", "long_term_stability_certified"), True),
    (("channel_acceptance", "promotion_request_id"), "old-receipt"),
    (("live_acceptance", "cases", 0, "passed"), False),
    (("replay_bootstrap", "capability_acceptance", "results", 0, "validator_passed"), False),
    (("readiness", "status"), "L5"),
    (("production_recall_gate", "status"), "accepted"),
])
def test_controlled_success_rejects_wrong_authority_incomplete_or_inflated_claims(path, value):
    report = complete_report()
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert controlled_production_closure_report_ok(report) is False
    result = summary(report)
    assert result["ok"] is False and result["ordinary_release_admission"] == "denied"
    assert result["l5_certification"] == "not_certified"


@pytest.mark.parametrize("execution", [{"closure_exit_status": 1}, {"closure_exit_status": 2},
    {"expected_commit": "e" * 40}, {"expected_scope": {**SCOPE, "user_id": "foreign"}}])
def test_controlled_verdict_preserves_process_and_caller_failures(execution):
    assert summary(complete_report(), **execution)["ok"] is False


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Only orchestration is mocked; native core/security contracts have separate tests."""
    core = ModuleType("eimemory.evaluation.controlled_production_acceptance")
    core.load_controlled_policy = lambda runtime, scope: {
        "stable_before": "2026-01-01T00:00:00Z", "agents": [{"scope": SCOPE}]}
    core.run_controlled_production_acceptance = lambda *args, **kwargs: controlled_acceptance()
    core.verify_controlled_production_acceptance = lambda *args, **kwargs: controlled_acceptance()
    monkeypatch.setitem(sys.modules, core.__name__, core)
    monitor = ModuleType("eimemory.ops.controlled_stability")
    observations = iter(stability()["samples"])
    monitor.collect_controlled_stability_sample = lambda **kwargs: next(observations)
    monitor.assess_controlled_stability = assess_stability
    monkeypatch.setitem(sys.modules, monitor.__name__, monitor)
    monkeypatch.setattr(controlled_rpc_probe, "run_controlled_rpc_probe", lambda *args, **kwargs: rpc_probe())
    from eimemory.evaluation import production_query_dataset
    from eimemory.governance.l5 import l5_readiness
    from eimemory.governance.release import release_closure_pending
    from eimemory.governance.safety import prompt_safety_executor
    monkeypatch.setattr(production_query_dataset, "resolve_bound_logical_scope",
                        lambda scope: (dict(scope), {"rewritten": False}))
    monkeypatch.setattr(l5_readiness, "_storage_migration_status", lambda runtime: {"ok": True})
    monkeypatch.setattr(release_closure_pending, "supersede_release_closure_pending",
                        lambda **kwargs: {"ok": True})
    monkeypatch.setattr(prompt_safety_executor, "bind_prompt_safety_from_service", lambda runtime: None)
    monkeypatch.setattr(closure, "_wait_for_controlled_health_refresh", lambda: None)
    monkeypatch.setattr(closure, "_wait_for_controlled_window", lambda started: None)
    monkeypatch.setattr(closure, "_controlled_natural_progress", lambda *args, **kwargs: {
        "certifies_natural_queries": False, "natural_samples_created": 0,
        "agents": [{"agent_id": "colleague", "accepted_case_count": 0, "required_case_count": 3}]})
    runtime = SimpleNamespace(store=SimpleNamespace(sqlite=SimpleNamespace(path=tmp_path / "sqlite.db")),
        verify_and_record_deployment=lambda **kwargs: receipt(),
        run_configured_production_recall_gate=lambda **kwargs: pytest.fail("Natural gate must not be relabeled"),
        run_weak_capability_replay_gate=lambda **kwargs: replay(),
        run_live_task_acceptance=lambda **kwargs: live(),
        record_external_channel_acceptance=lambda **kwargs: complete_report()["channel_acceptance"])
    def run():
        return closure.run_release_closure(runtime, scope=SCOPE, repo_root="/fixture/repo",
            current_link="/fixture/current", health_url="http://127.0.0.1:8091/health",
            prior_commit="b" * 40, pending_path=tmp_path / "pending.json")
    return SimpleNamespace(core=core, monitor=monitor, transport=controlled_rpc_probe, runtime=runtime, run=run)


def test_selected_pipeline_uses_fresh_native_verifier_and_keeps_natural_pending(pipeline):
    calls = []
    def verify(runtime, **kwargs):
        calls.append(kwargs)
        return controlled_acceptance()
    pipeline.core.verify_controlled_production_acceptance = verify
    report = pipeline.run()
    assert report["ok"] is True and summary(report)["ok"] is True
    assert calls[0]["record_id"] == "controlled-current"
    assert calls[0]["release"].receipt_id == receipt()["promotion_request_id"]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert report["natural_evidence"]["agents"][0]["accepted_case_count"] == 0
    assert report["natural_evidence"]["agents"][0]["required_case_count"] == 3


@pytest.mark.parametrize("failed", ["policy", "run", "verify", "transport", "replay", "live", "channel", "stability"])
def test_selected_pipeline_cannot_bypass_failed_native_gate(pipeline, failed):
    failure = {"ok": False, "accepted": False, "error": "native_gate_failed"}
    if failed == "policy":
        def invalid(*args):
            raise ValueError("invalid operator policy")
        pipeline.core.load_controlled_policy = invalid
    elif failed in {"run", "verify"}:
        setattr(pipeline.core, failed + "_controlled_production_acceptance", lambda *args, **kwargs: failure)
    elif failed == "stability":
        pipeline.monitor.assess_controlled_stability = lambda *args, **kwargs: failure
    elif failed == "transport":
        pipeline.transport.run_controlled_rpc_probe = lambda *args, **kwargs: failure
    else:
        method = {"replay": "run_weak_capability_replay_gate", "live": "run_live_task_acceptance",
                  "channel": "record_external_channel_acceptance"}[failed]
        setattr(pipeline.runtime, method, lambda **kwargs: failure)
    report = pipeline.run()
    assert report["ok"] is False and report["closure_complete"] is False
    assert summary(report)["ok"] is False


def test_absent_policy_preserves_existing_natural_gate(pipeline):
    pipeline.core.load_controlled_policy = lambda *args: None
    pipeline.runtime.run_configured_production_recall_gate = lambda **kwargs: {
        "ok": False, "accepted": False, "blocked_reason": "actual_retrieval_failure"}
    report = pipeline.run()
    assert report["report_type"] == "l5_release_closure"
    assert report["blocked_reason"] == "actual_retrieval_failure"


def test_source_or_policy_change_during_system_checks_cannot_reuse_earlier_pass(pipeline):
    observations = iter([controlled_acceptance(), {"ok": False, "accepted": False,
        "blocked_reason": "controlled_acceptance_source_version_changed"}])
    pipeline.core.verify_controlled_production_acceptance = lambda *args, **kwargs: next(observations)
    report = pipeline.run()
    assert report["ok"] is False and report["closure_complete"] is False
    assert report["blocked_reason"] == "controlled_acceptance_source_version_changed"


def test_successful_rpc_probe_expires_health_cache_before_first_stability_sample(pipeline, monkeypatch):
    calls = []
    original_collect = pipeline.monitor.collect_controlled_stability_sample
    monkeypatch.setattr(pipeline.transport, "run_controlled_rpc_probe",
                        lambda *args, **kwargs: calls.append("probe") or rpc_probe())
    monkeypatch.setattr(closure, "_wait_for_controlled_health_refresh",
                        lambda: calls.append("health_cache_expired"))

    def collect(**kwargs):
        calls.append("sample")
        return original_collect(**kwargs)

    monkeypatch.setattr(pipeline.monitor, "collect_controlled_stability_sample", collect)
    assert pipeline.run()["ok"] is True
    assert calls == ["probe", "health_cache_expired", "sample", "sample"]


def test_unhealthy_first_sample_after_cache_expiry_still_blocks_without_retry(pipeline, monkeypatch):
    calls = []
    failure = {"ok": False, "blocked_reason": "controlled_stability_health_failed"}
    monkeypatch.setattr(closure, "_wait_for_controlled_health_refresh",
                        lambda: calls.append("health_cache_expired"))
    monkeypatch.setattr(pipeline.monitor, "collect_controlled_stability_sample",
                        lambda **kwargs: calls.append("sample") or failure)
    report = pipeline.run()
    assert report["ok"] is False and report["closure_complete"] is False
    assert report["stability"] == failure
    assert calls == ["health_cache_expired", "sample"]


def test_failed_rpc_probe_does_not_wait_or_collect_stability(pipeline, monkeypatch):
    monkeypatch.setattr(pipeline.transport, "run_controlled_rpc_probe",
                        lambda *args, **kwargs: {"ok": False})
    monkeypatch.setattr(closure, "_wait_for_controlled_health_refresh",
                        lambda: pytest.fail("A failed RPC probe must block before waiting"))
    monkeypatch.setattr(pipeline.monitor, "collect_controlled_stability_sample",
                        lambda **kwargs: pytest.fail("A failed RPC probe must block before sampling"))
    report = pipeline.run()
    assert report["ok"] is False and report["closure_complete"] is False
    assert report["blocked_reason"] == "controlled_rpc_probe_failed"


def test_health_refresh_wait_uses_native_ttl_and_short_monotonic_slices(monkeypatch):
    from eimemory.adapters.eibrain import health_probe
    import time

    clock = {"now": 100.0}
    sleeps = []
    monkeypatch.setattr(health_probe, "_FRAGMENT_TTL_SECONDS", 2.25)
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(time, "sleep", sleep)
    closure._wait_for_controlled_health_refresh()
    assert clock["now"] - 100.0 > health_probe.health_fragment_cache_ttl_seconds()
    assert all(0 < seconds <= 1.0 for seconds in sleeps)


def test_long_mutating_gates_are_followed_by_a_full_stability_window(pipeline, monkeypatch):
    import time

    clock = {"now": 100.0}
    samples_at = []
    sleeps = []
    finished_at = {}
    original_collect = pipeline.monitor.collect_controlled_stability_sample
    replay_result, live_result = replay(), live()
    channel_result = complete_report()["channel_acceptance"]
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    def collect(**kwargs):
        samples_at.append(clock["now"])
        return original_collect(**kwargs)

    def gate(name, duration, result):
        def run(**kwargs):
            clock["now"] += duration
            finished_at[name] = clock["now"]
            return result
        return run

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(closure, "_wait_for_controlled_window", _wait_for_controlled_window)
    monkeypatch.setattr(pipeline.monitor, "collect_controlled_stability_sample", collect)
    monkeypatch.setattr(pipeline.runtime, "run_weak_capability_replay_gate", gate("replay", 80, replay_result))
    monkeypatch.setattr(pipeline.runtime, "run_live_task_acceptance", gate("live", 12, live_result))
    monkeypatch.setattr(pipeline.runtime, "record_external_channel_acceptance", gate("channel", 1, channel_result))
    report = pipeline.run()
    assert report["ok"] is True
    assert samples_at[0] == 100.0
    assert finished_at == {"replay": 180.0, "live": 192.0, "channel": 193.0}
    assert samples_at[1] - finished_at["channel"] == 60.0
    assert sum(sleeps) == 60.0 and all(0 < seconds <= 1.0 for seconds in sleeps)
