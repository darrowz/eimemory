"""Scheduled backup set: verified, atomic, bounded retention, no secret output."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tarfile
from datetime import datetime, timezone

import pytest

REPO = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location("eimemory_backup", REPO / "deploy" / "eimemory_backup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_cli(tmp_path: Path, *, verify_ok: bool = True) -> str:
    """Stand-in for ``eimemory backup create|verify`` using the real helpers."""
    script = tmp_path / "fake-eimemory"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json, sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from pathlib import Path\n"
        "from eimemory.compatibility.migration_helpers import backup_create, backup_verify\n"
        "cmd, path = sys.argv[2], sys.argv[3]\n"
        "if cmd == 'create':\n"
        "    from eimemory.api.runtime import Runtime\n"
        f"    runtime = Runtime.create(root=Path({str(tmp_path / 'rt')!r}))\n"
        "    if not runtime.store.list_records(limit=1):\n"
        "        runtime.memory.ingest(text='Backup must preserve records', memory_type='fact',\n"
        "            title='Backup note', scope={'agent_id': 'main', 'workspace_id': 'repo-x'})\n"
        "    print(json.dumps(backup_create(runtime, path), default=str)); runtime.close(); sys.exit(0)\n"
        "report = backup_verify(path)\n"
        f"if not {verify_ok!r}: report['ok'] = False\n"
        "print(json.dumps(report, default=str)); sys.exit(0 if report['ok'] else 1)\n"
    )
    script.chmod(0o755)
    return str(script)


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "var"
    state = root / "state"
    state.mkdir(parents=True)
    conn = sqlite3.connect(state / "eimemory.sqlite")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE records (id TEXT)")
    conn.executemany("INSERT INTO records VALUES (?)", [("a",), ("b",), ("c",)])
    conn.commit()
    conn.close()
    (state / "events.jsonl").write_text('{"e": 1}\n')
    (root / "external_channel_delivery_state.json").write_text("{}")
    # Scratch copies in subdirectories are not runtime authority.
    (state / "deploy-jobs").mkdir()
    (state / "deploy-jobs" / "pre-scope.sqlite").write_bytes(b"not a db")
    config = tmp_path / "etc"
    config.mkdir()
    (config / "rpc.env").write_text("EIMEMORY_RPC_TOKEN=super-secret-value\n")
    return root, config


def test_backup_set_is_verified_complete_and_discoverable_by_governance(tmp_path, capsys):
    module = _module()
    root, config = _runtime(tmp_path)
    rc = module.main(["--root", str(root), "--config-dir", str(config),
                      "--eimemory-bin", _fake_cli(tmp_path), "--keep", "3"])
    out = capsys.readouterr().out
    assert rc == 0
    report = json.loads(out)
    assert report["ok"] is True
    assert "super-secret-value" not in out
    assert report["sqlite"] == [{"file": "sqlite/eimemory.sqlite", "ok": True, "integrity_check": "ok",
                                 "records_rows": 3, "seconds": report["sqlite"][0]["seconds"]}]
    set_dir = Path(report["path"])
    assert set_dir.parent == root / "backups"
    assert stat.S_IMODE(set_dir.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((set_dir / "config.tar.gz").stat().st_mode) == 0o600
    with tarfile.open(set_dir / "config.tar.gz") as archive:
        assert "etc/rpc.env" in archive.getnames()
    manifest = json.loads((set_dir / "backup-set.json").read_text())
    paths = {item["path"] for item in manifest["files"]}
    assert {"sqlite/eimemory.sqlite", "records/backup.jsonl", "records/backup.manifest.json",
            "state/state/events.jsonl", "state/root/external_channel_delivery_state.json",
            "config.tar.gz"} <= paths
    assert not any("deploy-jobs" in p or "pre-scope" in p for p in paths)
    # The restored SQLite copy is a working database.
    restored = sqlite3.connect(set_dir / "sqlite" / "eimemory.sqlite")
    assert restored.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 3
    restored.close()
    # Governance discovers and verifies the records manifest.
    from eimemory.governance.snapshot import _collect_backup_reports
    reports = _collect_backup_reports(root)
    assert reports and reports[0]["verified"] is True


def test_failed_verification_never_publishes_a_backup_set(tmp_path, capsys):
    module = _module()
    root, config = _runtime(tmp_path)
    rc = module.main(["--root", str(root), "--config-dir", str(config),
                      "--eimemory-bin", _fake_cli(tmp_path, verify_ok=False)])
    report = json.loads(capsys.readouterr().out)
    assert rc == 1 and report["ok"] is False
    published = [p for p in (root / "backups").iterdir() if module.SET_NAME.match(p.name)]
    assert published == []


def test_retention_keeps_newest_complete_sets_only(tmp_path):
    module = _module()
    root, config = _runtime(tmp_path)
    cli = _fake_cli(tmp_path)
    for day in range(1, 5):
        report = module.run_backup(root=root, config_dir=config, backup_root=root / "backups",
                                   eimemory_bin=cli, keep=2,
                                   now=datetime(2026, 10, day, 2, 40, tzinfo=timezone.utc))
        assert report["ok"] is True
    (root / "backups" / "unrelated").mkdir()
    names = sorted(p.name for p in (root / "backups").iterdir())
    assert names == ["20261003T024000Z", "20261004T024000Z", "unrelated"]


def test_backup_units_are_installed_monitored_and_classified():
    installer = (REPO / "deploy" / "install_immutable_release.sh").read_text()
    assert "deploy/systemd/eimemory-backup.timer" in installer
    assert "deploy/systemd/eimemory-backup.service" in installer
    from eimemory.ops.timer_monitor import DEFAULT_SERVICE_UNITS, DEFAULT_TIMER_UNITS
    assert "eimemory-backup.timer" in DEFAULT_TIMER_UNITS
    assert "eimemory-backup.service" in DEFAULT_SERVICE_UNITS
    service = (REPO / "deploy" / "systemd" / "eimemory-backup.service").read_text()
    assert "UMask=0077" in service and "deploy/eimemory_backup.py" in service
    from eimemory.governance.release.release_impact import DOMAIN_PATHS
    assert "deploy/eimemory_backup.py" in DOMAIN_PATHS["storage.integrity"]
