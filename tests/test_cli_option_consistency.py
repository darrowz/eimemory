"""Ordinary option parsing and no-op fixture dispatch contracts."""
import argparse
from types import SimpleNamespace

import pytest

from eimemory.cli import doctor
from eimemory.cli import main as cli


def test_standalone_doctor_help_exits_before_checks(capsys):
    with pytest.raises(SystemExit) as caught:
        doctor._build_parser().parse_args(["--help"])
    assert caught.value.code == 0
    assert "--no-systemd" in capsys.readouterr().out


def test_standalone_doctor_rejects_unknown_options(capsys):
    with pytest.raises(SystemExit) as caught:
        doctor._build_parser().parse_args(["--typo"])
    assert caught.value.code == 2


@pytest.mark.parametrize("args,expected", [([], True), (["--json"], True), (["--markdown"], False)])
def test_dashboard_format_selection(args, expected):
    parser = argparse.ArgumentParser()
    cli._add_dashboard_format_options(parser)
    assert (parser.parse_args(args).json is not False) is expected


@pytest.mark.parametrize("args", [["--json", "--markdown"], ["--markdown", "--json"]])
def test_dashboard_format_options_are_mutually_exclusive(capsys, args):
    parser = argparse.ArgumentParser()
    cli._add_dashboard_format_options(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(args)


@pytest.mark.parametrize("apply,dry_run,expected", [(False, False, True), (True, False, False), (False, True, True), (True, True, True)])
def test_explicit_compact_dry_run_wins_over_apply(capsys, apply, dry_run, expected):
    seen = []
    runtime = SimpleNamespace(compact_learning_records=lambda **kwargs: seen.append(kwargs) or {"ok": True})
    parsed = SimpleNamespace(learn_command="compact", apply=apply, dry_run=dry_run)
    assert cli._cmd_learn(parsed, runtime, {}) == 0
    assert seen[0]["dry_run"] is expected


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.1])
def test_source_score_rejects_nonfinite_and_out_of_range_before_call(capsys, score):
    runtime = SimpleNamespace(expand_sources_autonomously=lambda **kwargs: pytest.fail("must not expand"))
    parsed = SimpleNamespace(source_command="expand", max_apply=0, min_score=score, apply=False)
    assert cli._cmd_source(parsed, runtime, {}) == 2
    assert "invalid_min_score" in capsys.readouterr().out


def test_wal_failure_recommendation_does_not_name_nonexistent_command(monkeypatch):
    def execute(sql):
        return SimpleNamespace(fetchall=lambda: [("ok",)] if sql == "PRAGMA integrity_check" else [], fetchone=lambda: [3])
    monkeypatch.setattr(doctor, "locked_connection", lambda runtime: SimpleNamespace(execute=execute))
    monkeypatch.setattr(doctor, "_file_size", lambda path: doctor.WAL_FAIL_BYTES if str(path).endswith("-wal") else 0)
    runtime = SimpleNamespace(store=SimpleNamespace(sqlite=SimpleNamespace(path="synthetic.sqlite")))
    report = doctor.check_sqlite_integrity(runtime)
    assert report.status == doctor.FAIL
    assert "eimemory ops nightly" not in report.recommendation
