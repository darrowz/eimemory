"""Independent checks that unavailable projections never certify a no-op."""
from types import SimpleNamespace

from test_capability_gap_diagnostics import SCOPE, registered


def test_explicit_ok_with_unknown_projection_identity_cannot_certify_noop(registered, monkeypatch):
    runtime, profile, catalog = registered
    projection = {"ok": True, "snapshots": [{"snapshot_id": "unknown"}], "blocked": []}
    monkeypatch.setattr("eimemory.governance.evolution.dynamic_capability_evolution.CapabilityStateProjector.project",
        lambda *args, **kwargs: SimpleNamespace(to_dict=lambda: projection))
    execution = runtime.execute_dynamic_capability_evolution(profile_key=profile.profile_key, scope=SCOPE, catalog=catalog)
    assert execution["ok"] is False and execution["status"] == "blocked"
    assert execution["results"] == [] and execution["applied_count"] == 0


def test_nightly_does_not_call_blocked_empty_execution_no_dynamic_gaps(monkeypatch):
    from eimemory.scheduler.jobs import _run_dynamic_capability_evolution
    monkeypatch.setattr("eimemory.scheduler.jobs._capability_v3_profile_key", lambda: "profile.current")
    runtime = SimpleNamespace(execute_dynamic_capability_evolution=lambda **kwargs: {
        "ok": False, "status": "blocked", "results": [], "applied_count": 0,
        "profile_projection": {"projection_ok": False, "projection_snapshot_count": 0,
                               "projection_blocked_count": 0}})
    report = _run_dynamic_capability_evolution(runtime, scope=SCOPE)
    assert report["ok"] is False and report["status"] == "blocked"
