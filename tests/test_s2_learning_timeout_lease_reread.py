"""S2: autonomous learning timeout path must call lease-reread helper."""
from __future__ import annotations

from eimemory.scheduler import jobs as jobs_mod


def test_run_autonomous_learning_timeout_uses_effects_unknown_helper(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_effects(report, *, lease_reread=None):
        calls.append({"report": dict(report), "lease_reread": dict(lease_reread or {})})
        out = dict(report)
        out["ok"] = False
        out["effects_unknown"] = True
        out["blocked_reason"] = "scheduler_lease_effects_unknown_after_timeout"
        out["lease_reread"] = lease_reread
        return out

    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_ENABLED", "1")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_TIMEOUT_SECONDS", "30")
    monkeypatch.setattr(jobs_mod, "_effects_unknown_after_timeout", fake_effects)
    monkeypatch.setattr(
        jobs_mod,
        "_reread_autonomous_learning_lease",
        lambda runtime, *, scope, report: {"state": "busy", "side_effects": "possible", "owner": "loop-1"},
    )

    class Runtime:
        def run_autonomous_learning_cycle(self, **kwargs):
            return {"ok": True, "promotions": [{"applied": True}], "candidate_ids": ["c1"]}

    # Force elapsed > timeout by mocking monotonic.
    clock = {"t": 0.0}

    def mono():
        clock["t"] += 100.0
        return clock["t"]

    monkeypatch.setattr(jobs_mod.time, "monotonic", mono)
    report = jobs_mod._run_autonomous_learning(Runtime(), scope={"agent_id": "a"})
    assert calls, "timeout path must invoke _effects_unknown_after_timeout"
    assert report["ok"] is False
    assert report["effects_unknown"] is True
    assert calls[0]["lease_reread"]["state"] == "busy"
