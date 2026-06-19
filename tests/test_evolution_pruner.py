from __future__ import annotations

from eimemory.api.runtime import Runtime
import eimemory.governance.autonomy_controller as autonomy_controller
from eimemory.governance.evolution_pruner import PRODUCTIVE_MODULES, classify_evolution_modules


def test_productive_modules_are_fixed_loop_policy() -> None:
    assert PRODUCTIVE_MODULES == [
        "memory_retrieval",
        "task_replay",
        "autonomous_patch",
        "safety_gate",
    ]


def test_classify_keeps_productive_modules_and_demotes_zero_success_modules() -> None:
    report = classify_evolution_modules(
        online_evidence=[
            {"module": "memory_retrieval", "success_count": 0},
            {"module": "task_replay", "success_count": -1},
            {"module": "curiosity", "success_count": 0},
            {"module": "world_watchers", "success_count": 2},
        ]
    )

    assert report["ok"] is True
    assert report["keep"] == PRODUCTIVE_MODULES
    assert report["demote"] == ["curiosity"]
    assert report["observe"] == ["world_watchers"]


def test_classify_accepts_mapping_evidence() -> None:
    report = classify_evolution_modules(
        online_evidence={
            "safety_gate": {"success_count": 0},
            "research_planner": {"success_count": "0"},
            "replay_quality": {"success_count": "3"},
        }
    )

    assert report["ok"] is True
    assert "safety_gate" in report["keep"]
    assert report["demote"] == ["research_planner"]
    assert report["observe"] == ["replay_quality"]


def test_autonomy_cycle_surfaces_loop_policy_when_online_evidence_available(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)

    def fake_learning(_runtime, **_kwargs):
        return {
            "ok": True,
            "replay_dataset": {},
            "real_task_replay": {},
            "promotions": [],
            "online_evidence": [
                {"module": "memory_retrieval", "success_count": 0},
                {"module": "world_watchers", "success_count": 0},
                {"module": "task_replay", "success_count": 1},
            ],
        }

    monkeypatch.setattr(autonomy_controller, "_legacy_run_autonomous_learning_cycle", fake_learning)
    monkeypatch.setattr(runtime, "build_learning_dashboard", lambda **_kwargs: {"ok": True, "report_type": "autonomous_learning_daily_dashboard", "period_type": "daily"})

    report = autonomy_controller.run_autonomy_cycle(runtime)

    assert report["ok"] is True
    assert report["loop_policy"]["productive_modules"] == PRODUCTIVE_MODULES
    assert report["loop_policy"]["demoted_modules"] == ["world_watchers"]
    assert report["demoted_modules"] == ["world_watchers"]
