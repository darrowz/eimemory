from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from eimemory.governance.release import release_closure as closure
from eimemory.governance.release.deployment_receipt import _health_identity_diagnostics
from eimemory.retrieval.readiness_contract import FLAGS, ORIGIN, SCHEMA
from test_controlled_production_release_closure import pipeline, controlled_acceptance, live, replay, stability


def measurement(revision="42", watermark="sync-current"):
    return {**dict.fromkeys(FLAGS, True), "schema": SCHEMA, "measurement_origin": ORIGIN,
            "watermark": watermark, "authority_revision": revision, "circuit": "closed",
            "last_error": "", "error": "", "lag_seconds": 0,
            "observed_at": datetime.now(timezone.utc).isoformat()}


@pytest.fixture
def phase(monkeypatch):
    import time
    import eimemory.ops.candidate_readiness_barrier as barrier
    import eimemory.ops.controlled_stability as stability
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(time, "sleep", lambda delay: setattr(clock, "now", clock.now+delay))
    source = object()
    runtime = SimpleNamespace(memory=SimpleNamespace(recall_engine=SimpleNamespace(candidate_source=source)))
    calls = []

    def ready(actual, *, timeout_seconds):
        assert actual is runtime and 0 < timeout_seconds <= 300
        calls.append(timeout_seconds)
        return {"ok": True, "applicable": True, "final_measurement": measurement()}

    monkeypatch.setattr(barrier, "wait_for_candidate_readiness", ready)
    monkeypatch.setattr(stability, "_health", lambda commit, **kwargs:
                        {"ok": True, "postgres_readiness": measurement()})
    return SimpleNamespace(runtime=runtime, clock=clock, barrier=barrier, stability=stability,
                           calls=calls, monkeypatch=monkeypatch)


def test_cached_pre_write_healthy_endpoint_cannot_admit_new_projection(phase):
    health = [measurement("41", "sync-old"), measurement()]
    phase.monkeypatch.setattr(phase.stability, "_health", lambda commit, **kwargs:
                             {"ok": True, "postgres_readiness": health.pop(0)})
    result = closure._wait_for_controlled_projection(phase.runtime, expected_commit="a"*40, timeout_seconds=1)
    assert result["ok"] is True and result["attempt_count"] == 2
    assert result["elapsed_seconds"] == .25
    assert result["error_code_counts"] == {"endpoint_projection_not_current": 1}
    assert len(phase.calls) == 2 and phase.calls[1] < phase.calls[0]
    assert result["recall_budget_changed"] is False and result["read_only"] is True


def test_later_generation_must_converge_on_both_native_observers(phase):
    reports = [measurement(), measurement("43", "sync-new")]
    phase.monkeypatch.setattr(phase.barrier, "wait_for_candidate_readiness", lambda *a, **kw:
                             {"ok": True, "applicable": True, "final_measurement": reports.pop(0)})
    phase.monkeypatch.setattr(phase.stability, "_health", lambda *a, **kw:
                             {"ok": True, "postgres_readiness": measurement("43", "sync-new")})
    result = closure._wait_for_controlled_projection(phase.runtime, expected_commit="a"*40, timeout_seconds=1)
    assert result["ok"] is True and result["attempt_count"] == 2


def test_persistently_stale_endpoint_is_a_real_failure(phase):
    phase.monkeypatch.setattr(phase.stability, "_health", lambda *a, **kw:
                             {"ok": True, "postgres_readiness": measurement("41", "sync-old")})
    result = closure._wait_for_controlled_projection(phase.runtime, expected_commit="a"*40, timeout_seconds=.6)
    assert result["ok"] is False and result["blocked_reason"] == "projection_wait_exhausted"
    assert result["elapsed_seconds"] == .6 and result["attempt_count"] == 3


@pytest.mark.parametrize("late_stage", ["candidate", "endpoint"])
def test_late_positive_proof_cannot_pass_the_phase_deadline(phase, late_stage):
    def candidate(*args, **kwargs):
        if late_stage == "candidate": phase.clock.now += 2
        return {"ok": True, "applicable": True, "final_measurement": measurement()}

    def endpoint(*args, **kwargs):
        assert kwargs["timeout_seconds"] <= 1
        if late_stage == "endpoint": phase.clock.now += 2
        return {"ok": True, "postgres_readiness": measurement()}

    phase.monkeypatch.setattr(phase.barrier, "wait_for_candidate_readiness", candidate)
    phase.monkeypatch.setattr(phase.stability, "_health", endpoint)
    result = closure._wait_for_controlled_projection(phase.runtime, expected_commit="a"*40, timeout_seconds=1)
    assert result["ok"] is False and result["blocked_reason"] == "projection_wait_exhausted"


@pytest.mark.parametrize("stage", ["candidate", "endpoint"])
def test_candidate_source_replacement_is_not_accepted(phase, stage):
    def candidate(*args, **kwargs):
        if stage == "candidate": phase.runtime.memory.recall_engine.candidate_source = object()
        return {"ok": True, "applicable": True, "final_measurement": measurement()}

    def endpoint(*args, **kwargs):
        if stage == "endpoint": phase.runtime.memory.recall_engine.candidate_source = object()
        return {"ok": True, "postgres_readiness": measurement()}

    phase.monkeypatch.setattr(phase.barrier, "wait_for_candidate_readiness", candidate)
    phase.monkeypatch.setattr(phase.stability, "_health", endpoint)
    result = closure._wait_for_controlled_projection(phase.runtime, expected_commit="a"*40, timeout_seconds=1)
    assert result["ok"] is False and result["blocked_reason"] == "candidate_source_changed"


@pytest.mark.parametrize("bad", [{"ok": False}, {"ok": True, "applicable": False},
                                 {"ok": True, "applicable": True, "final_measurement": {"ready": True}}])
def test_missing_or_changed_native_projection_contract_fails_closed(phase, bad):
    phase.monkeypatch.setattr(phase.barrier, "wait_for_candidate_readiness", lambda *a, **kw: deepcopy(bad))
    result = closure._wait_for_controlled_projection(phase.runtime, expected_commit="a"*40, timeout_seconds=1)
    assert result["ok"] is False


@pytest.mark.parametrize("value", [0, -1, 301, True, float("inf"), float("nan")])
def test_phase_wait_budget_is_finite_and_bounded(phase, value):
    with pytest.raises(ValueError, match="invalid_projection_wait_budget"):
        closure._wait_for_controlled_projection(phase.runtime, expected_commit="a"*40, timeout_seconds=value)


def test_health_failure_retains_ready_and_presence_without_copying_private_fields():
    data = {"ok": False, "checks": {"ready": False, "private": "do-not-copy"},
            "commit": "a"*40, "version": "1.14.150", "paths": {"current": "/current", "release": "/release"},
            "import_root": "/release/eimemory", "package_tree_digest": "b"*64,
            "api_key": "never-copy-me", "retrieval": {"candidate_source": {
                "current_readiness": {**measurement(), "ready": False, "error": "authority_revision_changed"},
                "connection_string": "private://credential"}}}
    result = _health_identity_diagnostics(data)
    assert result["health_ok"] is result["checks_ready"] is False
    assert all(result["identity_fields_present"].values())
    assert result["postgres_readiness"]["error"] == "authority_revision_changed"
    assert set(result) == {"schema", "health_ok", "checks_ready", "identity_fields_present", "postgres_readiness"}
    assert "never-copy-me" not in repr(result) and "private://" not in repr(result)


def _require_pg_phase(pipeline):
    def accepted(*args, **kwargs):
        return {**controlled_acceptance(), "candidate_readiness_barrier": {"applicable": True}}
    pipeline.core.run_controlled_production_acceptance = accepted
    pipeline.core.verify_controlled_production_acceptance = accepted


def test_release_waits_after_mutating_replay_and_after_full_observation_window(pipeline, monkeypatch):
    _require_pg_phase(pipeline)
    events = []
    samples = iter(stability()["samples"])
    def collect(**kwargs):
        events.append("sample"); return next(samples)
    monkeypatch.setattr(pipeline.monitor, "collect_controlled_stability_sample", collect)
    monkeypatch.setattr(pipeline.runtime, "run_weak_capability_replay_gate",
                        lambda **kwargs: events.append("replay") or replay())
    monkeypatch.setattr(pipeline.runtime, "run_live_task_acceptance",
                        lambda **kwargs: events.append("live") or live())
    channel = pipeline.runtime.record_external_channel_acceptance
    monkeypatch.setattr(pipeline.runtime, "record_external_channel_acceptance",
                        lambda **kwargs: events.append("channel") or channel(**kwargs))
    monkeypatch.setattr(closure, "_wait_for_controlled_window", lambda started: events.append("window"))
    monkeypatch.setattr(closure, "_wait_for_controlled_projection",
                        lambda *args, **kwargs: events.append("barrier") or {"ok": True})
    result = pipeline.run()
    assert result["ok"] is True
    assert events == ["sample", "replay", "barrier", "live", "channel", "window", "barrier", "sample"]
    assert set(result["projection_readiness"]) == {"after_replay", "before_final_sample"}


@pytest.mark.parametrize("failed_phase", ["after_replay", "before_final_sample"])
def test_failed_projection_wait_never_becomes_a_successful_release(pipeline, monkeypatch, failed_phase):
    _require_pg_phase(pipeline)
    seen = []
    def wait(*args, **kwargs):
        name = "after_replay" if not seen else "before_final_sample"
        seen.append(name)
        return {"ok": name != failed_phase, "blocked_reason": "projection_wait_exhausted"}
    monkeypatch.setattr(closure, "_wait_for_controlled_projection", wait)
    if failed_phase == "after_replay":
        monkeypatch.setattr(pipeline.runtime, "run_live_task_acceptance",
                            lambda **kwargs: pytest.fail("live tasks ran before the projection was current"))
    result = pipeline.run()
    assert result["ok"] is result["closure_complete"] is False
    assert result["blocked_stage"] == "projection_readiness"
    assert result["projection_readiness"][failed_phase]["ok"] is False
    assert seen == (["after_replay"] if failed_phase == "after_replay" else ["after_replay", "before_final_sample"])


def test_an_actual_failed_first_sample_is_never_hidden_by_later_waits(pipeline, monkeypatch):
    _require_pg_phase(pipeline)
    bad = deepcopy(stability()["samples"][0]); bad["ok"] = False
    monkeypatch.setattr(pipeline.monitor, "collect_controlled_stability_sample", lambda **kwargs: bad)
    monkeypatch.setattr(closure, "_wait_for_controlled_projection",
                        lambda *args, **kwargs: pytest.fail("a failed sample was retried or hidden"))
    result = pipeline.run()
    assert result["ok"] is False and result["blocked_stage"] == "stability"
    assert result["stability"] is bad
