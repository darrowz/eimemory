from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
from threading import Barrier
from types import SimpleNamespace

import pytest

from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.api.runtime import Runtime
from eimemory.cli.main import dispatch
from eimemory.governance.capability_attribution import normalize_explicit_capability_outcomes
from eimemory.governance.closed_loop import _safe_rl_update
from eimemory.governance.outcome_evidence import outcome_evidence
from eimemory.governance.promotion_manager import promote_candidate
from test_capability_storage_v3 import SCOPE, STAMP, _binding, _definition, _revision
from test_promotion_watch import _intent_pattern, _passing_eval, _policy_candidate


def _trace(*, passed=True):
    return {"trace_id": "closure-trace", "idempotency_key": "closure-trace",
            "task_type": "repo.edit", "input_summary": "Run a task assertion",
            "outcome": {"status": "success" if passed else "failed"},
            "verifier": {"passed": passed}}


def _attributed_trace(runtime, *, passed=True):
    definition = _definition()
    revision = _revision(definition)
    binding = _binding(definition, revision)

    def seed(repository):
        repository.register_definition(definition, scope=SCOPE)
        repository.register_revision(revision, scope=SCOPE)
        repository.register_binding(binding, scope=SCOPE)

    runtime.store.mutate_capabilities_atomically(seed)
    return {**_trace(passed=passed),
            "verifier": {"passed": passed, "independent": True, "id": "verifier", "revision": "v1",
                         "contract_digest": "a" * 64},
            "capability_attribution": {
                "capability_id": definition.capability_id, "capability_revision_id": revision.revision_id,
                "provider_binding_id": binding.binding_id, "idempotency_key": "closure-observation",
                "observed_at": STAMP, "evidence_refs": ["artifact://test/assertion.json"],
                "environment_fingerprint": {"runtime": "isolated"}, "provenance": {"source": "test"},
            }}


def test_changed_trace_retry_cannot_create_contradictory_observation(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        first = runtime.record_outcome_trace(_trace(passed=False), scope=SCOPE)
        retry = runtime.record_outcome_trace(_attributed_trace(runtime), scope=SCOPE)
        assert retry["idempotent"] is True
        assert retry["record_id"] == first["record_id"]
        assert retry["capability_dual_write"]["status"] == "not_applicable"
        assert runtime.store.get_by_id(first["record_id"], scope=SCOPE).content["payload"]["verifier"]["passed"] is False
        assert runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_observations").fetchone()[0] == 0


def test_trace_retry_and_projection_recovery_use_persisted_payload(tmp_path, monkeypatch):
    from eimemory.capabilities.observations import CapabilityObservations

    with Runtime.create(root=tmp_path) as runtime:
        payload = _attributed_trace(runtime, passed=False)
        append = CapabilityObservations.append
        with monkeypatch.context() as failure:
            failure.setattr(CapabilityObservations, "append", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
            first = runtime.record_outcome_trace(payload, scope=SCOPE)
        assert first["capability_dual_write"]["status"] == "blocked"
        assert CapabilityObservations.append is append
        # Retry has both a later server timestamp and a changed caller verdict.
        retry_payload = deepcopy(payload)
        retry_payload["recorded_at"] = "2030-01-01T00:00:00+00:00"
        retry_payload["verifier"]["passed"] = True
        retry_payload["outcome"]["status"] = "success"
        retry = runtime.record_outcome_trace(retry_payload, scope=SCOPE)
        assert retry["idempotent"] is True and retry["capability_dual_write"]["status"] == "aligned"
        row = runtime.store.sqlite.conn.execute("SELECT verdict FROM capability_observations").fetchone()
        assert row["verdict"] == "fail"
        again = runtime.record_outcome_trace(payload, scope=SCOPE)
        assert again["capability_dual_write"]["idempotent"] is True
        recovered = normalize_explicit_capability_outcomes(runtime, scope=SCOPE)
        assert recovered["ok"] is True and recovered["idempotent"] == 1




@pytest.mark.parametrize("change", [
    {"rehearsal": True}, {"source": "client.assertion"},
    {"verifier": {"passed": False}},
    {"verifier": {"passed": False, "method": "codex.stop", "evidence_refs": ["event"], "checks": {"verification": "not_run"}}},
    {"verifier": {"passed": False, "method": "codex.stop", "evidence_refs": ["event"], "checks": {"verification": "not executed"}}},
])
def test_unexecuted_untrusted_or_replay_failures_stay_excluded(change):
    payload = {"source": "codex.stop", "outcome": "bad", "rehearsal": False,
               "verifier": {"passed": False, "method": "codex.stop", "evidence_refs": ["event"],
                            "checks": {"verification": "pytest assertion executed"}}, **change}
    assert outcome_evidence(payload)["production_eligible"] is False


@pytest.mark.parametrize("field", ["verification", "result"])
@pytest.mark.parametrize("marker", [
    "skipped", "skip", "unknown", "uncertain", "unavailable", "missing",
    "not run: dependency missing", "NOT-EXECUTED: executor unavailable",
])
def test_host_unexecuted_verification_and_result_are_not_negative_evidence(field, marker):
    checks = {"verification": "pytest assertion executed", "result": "assertion failed", field: marker}
    payload = {"source": "openclaw.task_end", "outcome": "bad", "rehearsal": False,
               "verifier": {"passed": False, "method": "openclaw.task_end", "evidence_refs": ["event"],
                            "checks": checks}}
    assert outcome_evidence(payload, require_host=True)["production_eligible"] is False
    # A second verifier identity cannot override an explicit non-execution signal.
    payload["verifier"].update(independent=True, id="verifier", revision="v1", contract_digest="a" * 64)
    assert outcome_evidence(payload)["production_eligible"] is False




def _reward(runtime):
    return _safe_rl_update(runtime, scope=SCOPE, state={"record_id": "source"},
                           action={"type": "experience_feedback", "id": "success"},
                           eval_result={"ok": True}, outcome={"success": True},
                           next_state={}, source_record_id="source")


def test_reward_write_failure_rolls_back_transition_and_retry_recovers(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        upsert = runtime.store.sqlite.upsert

        def failing_policy(record, **kwargs):
            if record.kind == "rl_policy_value":
                raise RuntimeError("policy write failed")
            return upsert(record, **kwargs)

        with monkeypatch.context() as failure:
            failure.setattr(runtime.store.sqlite, "upsert", failing_policy)
            assert _reward(runtime)["ok"] is False
        assert runtime.store.list_records(kinds=["rl_transition", "rl_policy_value"], scope=SCOPE) == []
        assert _reward(runtime)["ok"] is True
        assert _reward(runtime)["idempotent"] is True
        assert len(runtime.store.list_records(kinds=["rl_transition", "rl_policy_value"], scope=SCOPE)) == 2


def test_concurrent_feedback_from_two_runtimes_is_applied_once(tmp_path):
    with Runtime.create(root=tmp_path) as first, Runtime.create(root=tmp_path) as second:
        barrier = Barrier(2)

        def write(runtime):
            barrier.wait()
            return _reward(runtime)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, [first, second]))
        assert all(result["ok"] for result in results)
        assert sorted(result["idempotent"] for result in results) == [False, True]
        assert len(first.store.list_records(kinds=["rl_transition", "rl_policy_value"], scope=SCOPE)) == 2
