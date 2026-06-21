from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.governance.capability_ledger import record_capability_score


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

    assert cli_main(["reflect", "log", "tool.routing", "Bad route", "Use memory first for stable personal facts"]) == 0
    capsys.readouterr()
    assert cli_main(["learn", "cycle", "--apply", "--force"]) == 0
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

    assert cli_main(["learn", "ledger", "--limit", "1", "--since", "2099-01-02"]) == 0
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


def test_cli_learn_promote_applies_candidate(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main(["reflect", "log", "tool.routing", "Bad route", "Use memory first"]) == 0
    capsys.readouterr()
    assert cli_main(["learn", "cycle", "--force"]) == 0
    cycle = json.loads(capsys.readouterr().out)
    assert cycle["promotion"]["applied"] is False

    assert cli_main(["learn", "promote", cycle["candidate_id"], "--apply"]) == 0
    promotion = json.loads(capsys.readouterr().out)

    assert promotion["ok"] is True
    assert promotion["applied"] is True


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

    assert cli_main(["learn", "dashboard"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    assert "## Capability Ledger" in report["markdown"]
    assert "trend" in report["markdown"].lower()


def test_cli_learn_think_persists_supervisor_contract(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main(["learn", "think", "--persist", "--max-items", "1"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    for key in ("last_success_at", "last_error_at", "duration_ms", "memory_peak", "produced_count", "promoted_count", "rolled_back_count"):
        assert key in report["supervisor_summary"]

    assert cli_main(["doctor"]) == 0
    doctor = json.loads(capsys.readouterr().out)

    assert doctor["supervisor"]["runs"]["learn-think"]["last_success_at"]
    assert doctor["supervisor"]["runs"]["learn-think"]["error"] == ""
    assert doctor["supervisor"]["runs"]["learn-watch"]["error"] == "no_run_record"
