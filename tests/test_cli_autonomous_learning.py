from __future__ import annotations

import json

from eimemory.cli.main import main as cli_main


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
