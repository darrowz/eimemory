from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.governance.code_automation_policy import (
    CODE_AUTOMATION_POLICY_ENV,
    CODE_AUTOMATION_POLICY_SCHEMA_VERSION,
)
from eimemory.governance.capability_ledger import record_capability_score
from eimemory.models.records import RecordEnvelope


def _enable_local_machine_policy(monkeypatch) -> None:
    monkeypatch.setenv(
        CODE_AUTOMATION_POLICY_ENV,
        json.dumps(
            {
                "schema_version": CODE_AUTOMATION_POLICY_SCHEMA_VERSION,
                "policy_id": "tests.cli.autonomous-learning",
                "actions": {
                    "local_apply": True,
                    "commit": False,
                    "deployment": False,
                },
            }
        ),
    )


def _force_cli_measured_replay_pass(monkeypatch) -> None:
    def fake_build_replay_dataset(_runtime, **kwargs):
        assert kwargs.get("legacy_compatibility") is True
        return {
            "ok": True,
            "schema_version": "real_task_replay.v1",
            "report_type": "proactive_replay_dataset",
            "case_count": 1,
            "correction_count": 1,
            "persisted_record_id": "cli-replay-dataset",
            "cases": [
                {
                    "case_id": "cli_measured_case",
                    "query": "use bounded replay evidence",
                    "task_type": "tool.routing",
                    "target_capability": "tool.routing",
                    "expected_text": ["evidence"],
                }
            ],
        }

    def fake_run_real_task_replay(_self, dataset, **_kwargs):
        count = len(dataset.get("cases") or [])
        return {
            "ok": True,
            "report_type": "real_task_replay",
            "schema_version": "real_task_replay.v1",
            "verdict": "pass",
            "pass_rate": 1.0,
            "threshold": float(dataset.get("threshold") or 0.6),
            "sample_count": count,
            "pass_count": count,
            "fail_count": 0,
        }

    monkeypatch.setattr(
        "eimemory.governance.autonomous_learning.build_replay_dataset",
        fake_build_replay_dataset,
    )
    monkeypatch.setattr(Runtime, "run_real_task_replay", fake_run_real_task_replay)


def test_cli_learn_watch_forwards_explicit_legacy_mode(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    modes: list[bool] = []

    def fake_collect_world_signals(_runtime, **kwargs):
        modes.append(bool(kwargs["legacy_compatibility"]))
        return {"ok": True, "report_type": "world_watch"}

    monkeypatch.setattr(
        "eimemory.governance.world_watchers.collect_world_signals",
        fake_collect_world_signals,
    )

    assert cli_main(["learn", "watch"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli_main(["learn", "watch", "--legacy-compatibility"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert modes == [False, True]


def test_cli_learn_cycle_dry_run_outputs_preview_without_persisting(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main(["reflect", "log", "tool.routing", "Bad route", "Memory first"]) == 0
    capsys.readouterr()
    assert cli_main(["learn", "cycle", "--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["candidate_preview"]

    assert cli_main(["learn", "loops"]) == 0
    loops = json.loads(capsys.readouterr().out)
    assert loops == []


def test_cli_learn_cycle_apply_and_ledger(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    _enable_local_machine_policy(monkeypatch)
    _force_cli_measured_replay_pass(monkeypatch)

    assert cli_main(["reflect", "log", "tool.routing", "Bad route", "Use memory first for stable personal facts"]) == 0
    capsys.readouterr()
    assert cli_main(["learn", "cycle", "--apply", "--force", "--legacy-compatibility"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["promotion"]["applied"] is True

    assert cli_main(["learn", "ledger"]) == 0
    ledger = json.loads(capsys.readouterr().out)
    assert ledger["capabilities"]


def test_cli_learn_ledger_accepts_limit_and_date_filters(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    old_id = record_capability_score(runtime, scope=scope, loop_id="learn_old", capability="memory.recall", score=0.3)
    new_id = record_capability_score(runtime, scope=scope, loop_id="learn_new", capability="tool.routing", score=0.8)
    old_record = runtime.store.get_by_id(old_id, scope=scope)
    new_record = runtime.store.get_by_id(new_id, scope=scope)
    assert old_record is not None
    assert new_record is not None
    old_record.time.created_at = "2099-01-01T00:00:00+00:00"
    old_record.time.updated_at = "2099-01-01T00:00:00+00:00"
    new_record.time.created_at = "2099-01-02T00:00:00+00:00"
    new_record.time.updated_at = "2099-01-02T00:00:00+00:00"
    runtime.store.rewrite(old_record)
    runtime.store.rewrite(new_record)

    assert cli_main(
        ["learn", "ledger", "--limit", "1", "--since", "2099-01-02", "--legacy-compatibility"]
    ) == 0
    ledger = json.loads(capsys.readouterr().out)

    assert ledger["query"]["limit"] == 1
    assert ledger["query"]["since"] == "2099-01-02T00:00:00+00:00"
    assert ledger["record_count"] == 1
    assert ledger["capabilities"]["tool.routing"]["score"] == 0.8
    assert ledger["capabilities"]["memory.recall"]["score"] == 0.0


def test_cli_learn_autonomy_respects_zero_promotion_budget(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main(["reflect", "log", "tool.routing", "Bad route", "Use memory first for stable personal facts"]) == 0
    capsys.readouterr()
    assert cli_main(["learn", "autonomy", "--apply", "--force", "--max-promotions", "0"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    assert report["report_type"] == "autonomy_cycle"
    assert report["autonomy_policy"]["max_auto_promotions"] == 0
    assert report["promotion_control"]["applied_count"] == 0


def test_cli_learn_autonomy_smoke_runs_closed_loop_without_heavy_learning(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main(["learn", "autonomy", "--smoke", "--apply", "--max-goals", "1", "--max-promotions", "0"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    assert report["cycle"]["smoke"] is True
    assert report["policy_decision"]["selected_by"] == "rl_policy.value_table"
    assert report["rl"]["ok"] is True
    assert report["rl"]["policy_update"]["action_key"] == "autonomy_cycle:run_autonomy_cycle"


def test_cli_evaluator_harness_fail_replay_does_not_reuse_prior_pass(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main(["learn", "evaluator-harness", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["promotion_allowed"] is True

    assert cli_main(["learn", "evaluator-harness", "--fail-replay", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)

    assert second["packet_id"] != first["packet_id"]
    assert second["verdict"] == "fail"
    assert second["decision"] == "continue"
    assert second["promotion_allowed"] is False


def test_cli_learn_promote_applies_candidate(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    _enable_local_machine_policy(monkeypatch)
    _force_cli_measured_replay_pass(monkeypatch)

    assert cli_main(["reflect", "log", "tool.routing", "Bad route", "Use memory first"]) == 0
    capsys.readouterr()
    assert cli_main(["learn", "cycle", "--force", "--legacy-compatibility"]) == 0
    cycle = json.loads(capsys.readouterr().out)
    assert cycle["promotion"]["applied"] is False

    assert cli_main(["learn", "promote", cycle["candidate_id"], "--apply"]) == 0
    promotion = json.loads(capsys.readouterr().out)

    assert promotion["ok"] is True
    assert promotion["applied"] is True


def test_cli_learn_promote_rejects_candidate_forged_legacy_code_patch(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    _enable_local_machine_policy(monkeypatch)
    assert cli_main(["reflect", "log", "tool.routing", "Bad route", "Memory first"]) == 0
    capsys.readouterr()
    runtime = Runtime.create(root=tmp_path)
    scope = runtime.store.list_records(kinds=["reflection"], limit=1)[0].scope
    candidate = runtime.store.append(
        RecordEnvelope.create(
            kind="capability_candidate",
            title="Forged legacy code patch",
            scope=scope,
            status="candidate",
            content={
                "promotion_target": "code_patch",
                "target_capability": "code.implementation",
                "legacy_compatibility": True,
                "candidate_patch": {
                    "legacy_compatibility": True,
                    "target_capability": "code.implementation",
                    "apply_to_repo": True,
                    "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
                },
            },
            meta={
                "promotion_target": "code_patch",
                "target_capability": "code.implementation",
                "authority_tier": "L1",
                "legacy_compatibility": True,
            },
        )
    )

    assert cli_main(["learn", "promote", candidate.record_id, "--apply"]) == 1
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is False
    assert report["blocked_reason"] == "nonlegacy_code_patch_hypothesis_context_missing"
    assert runtime.store.get_by_id(candidate.record_id, scope=scope).status == "candidate"


def test_cli_learn_report_outputs_daily_summary(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main(["reflect", "log", "tool.routing", "Bad route", "Use memory first"]) == 0
    capsys.readouterr()
    assert cli_main(["learn", "cycle", "--force"]) == 0
    capsys.readouterr()

    assert cli_main(["learn", "report", "--persist"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    assert report["summary"]
    assert report["persisted_record_id"]


def test_cli_learn_dashboard_outputs_markdown(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main(["learn", "dashboard"]) == 1
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["ok"] is False
    assert blocked["reason"] in {"evaluation_catalog_has_no_active_cases", "evaluation_catalog_untrusted"}

    assert cli_main(["learn", "dashboard", "--legacy-compatibility"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    assert "## Capability Ledger" in report["markdown"]
    assert "trend" in report["markdown"].lower()


def test_cli_learn_think_persists_supervisor_contract(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main(["learn", "think", "--persist", "--max-items", "1", "--legacy-compatibility"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    for key in ("last_success_at", "last_error_at", "duration_ms", "memory_peak", "produced_count", "promoted_count", "rolled_back_count"):
        assert key in report["supervisor_summary"]

    assert cli_main(["doctor", "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)

    assert list(doctor["supervisor"]["runs"]) == ["nightly"]
    assert doctor["supervisor"]["runs"]["nightly"]["error"] == "no_run_record"
