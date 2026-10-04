"""Synthetic CLI exit, dispatch, cleanup and diagnostic contracts."""
from types import SimpleNamespace

import pytest

from eimemory.cli import doctor, l1_worker
from eimemory.cli import main as cli


@pytest.mark.parametrize("action", ["eva1", "repairr"])
def test_unknown_worker_action_is_rejected_without_drain(monkeypatch, capsys, action):
    monkeypatch.setenv("EIMEMORY_L1_WORKER_ACTION", action)
    monkeypatch.setenv("EIMEMORY_ROOT", "/synthetic-root")
    monkeypatch.setattr(l1_worker, "drain_l1", lambda **kwargs: pytest.fail("must not drain"))
    assert l1_worker.main() == 2
    assert "invalid_worker_action" in capsys.readouterr().out


@pytest.mark.parametrize("ok,overall,expected", [(False, "HEALTHY", 2), (False, "DEGRADED", 2), (True, "DEGRADED", 0), (True, "UNKNOWN", 0)])
def test_doctor_exit_agrees_with_final_ok(ok, overall, expected):
    assert doctor.doctor_exit_code({"ok": ok, "overall_status": overall}) == expected
    assert doctor._health_overall_status(overall, {"ok": ok}) == (overall if ok else "UNHEALTHY")


@pytest.mark.parametrize("migrations", [1, 2])
def test_wal_warning_is_not_suppressed_by_small_migration_count(monkeypatch, migrations):
    def execute(sql):
        return SimpleNamespace(fetchall=lambda: [("ok",)] if sql == "PRAGMA integrity_check" else [],
            fetchone=lambda: [migrations if "schema_migrations" in sql else 1])
    monkeypatch.setattr(doctor, "locked_connection", lambda runtime: SimpleNamespace(execute=execute))
    monkeypatch.setattr(doctor, "_file_size", lambda path: doctor.WAL_WARN_BYTES + 1 if str(path).endswith("-wal") else 0)
    runtime = SimpleNamespace(store=SimpleNamespace(sqlite=SimpleNamespace(path="synthetic.sqlite")))
    assert doctor.check_sqlite_integrity(runtime).status == doctor.WARN


def test_human_report_includes_orphan_check_details():
    text = doctor.render_human({"overall_status": "DEGRADED", "checks": {
        "promotion_watch_orphans": {"status": "WARN", "details": "synthetic diagnostic"}}})
    assert "promotion_watch_orphans" in text and "synthetic diagnostic" in text


@pytest.mark.parametrize("fail", [False, True])
def test_main_closes_runtime_after_dispatch(monkeypatch, capsys, fail):
    closed = []
    runtime = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(cli, "_build_parser", lambda: SimpleNamespace(parse_args=lambda args: SimpleNamespace(command="synthetic")))
    monkeypatch.setattr(cli, "_ops_before_settings", lambda parsed: None)
    monkeypatch.setattr(cli, "_storage_before_runtime", lambda parsed, settings: None)
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(root="/synthetic-root", default_agent_id="a", default_workspace_id="w"))
    monkeypatch.setattr(cli, "Runtime", SimpleNamespace(create=lambda **kwargs: runtime))
    monkeypatch.setattr(cli, "hongtu_scope", lambda scope: scope)
    monkeypatch.setattr(cli, "COMMAND_REGISTRY", {"synthetic": object()})
    def dispatch(*args):
        if fail:
            raise RuntimeError("synthetic dispatch failure")
        return 0
    monkeypatch.setattr(cli, "dispatch", dispatch)
    if fail:
        with pytest.raises(RuntimeError, match="synthetic dispatch failure"):
            cli.main([])
    else:
        assert cli.main([]) == 0
    assert closed == [True]
