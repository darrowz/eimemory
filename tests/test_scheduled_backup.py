"""Scheduled backup set: verified, atomic, bounded retention, no secret output."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
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


@pytest.mark.skipif(os.name!='posix',reason='Native registered backup unit')
def test_registered_backup_reads_store_without_application_or_schema_bootstrap(tmp_path,monkeypatch):
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope,ScopeRef
    module=_module();root=tmp_path/'runtime'
    with Runtime.create(root=root) as runtime:
        record=RecordEnvelope.create(kind='memory',title='Native store snapshot',scope=ScopeRef())
        runtime.store.append(record)
    original=Path.read_text
    monkeypatch.setattr(Path,'read_text',lambda p,*a,**kw:'0::/system.slice/eimemory-backup.service\n'
        if p==Path('/proc/self/cgroup') else original(p,*a,**kw))
    monkeypatch.setattr(module.os,'getuid',lambda:1001);monkeypatch.setattr(module.os,'geteuid',lambda:1001)
    monkeypatch.setattr(Runtime,'create',lambda **_:pytest.fail('backup must not bootstrap Runtime'))
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**kw:pytest.fail('native backup must not launch a full application'))
    output=tmp_path/'records';output.mkdir()
    result=module._run_cli('untrusted', ['backup','create',str(output)],{'EIMEMORY_ROOT':str(root)},30)
    assert result['exit_code']==0 and result['payload']['record_count']==1
    checked=module._run_cli('untrusted',['backup','verify',str(output/'backup')],{},30)
    assert checked['exit_code']==0 and checked['payload']['ok']


def _main_with_restored_process_state(module, argv):
    # The real CLI deliberately installs private process defaults. These tests
    # call it in pytest's process, so retain those defaults during the call and
    # restore the caller's umask and signal handler even when the CLI fails.
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_umask = os.umask(0o077)
    try:
        return module.main(argv)
    finally:
        os.umask(previous_umask)
        signal.signal(signal.SIGTERM, previous_sigterm)


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
    rc = _main_with_restored_process_state(module, [
        "--root", str(root), "--config-dir", str(config),
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
    rc = _main_with_restored_process_state(module, [
        "--root", str(root), "--config-dir", str(config),
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


def _native_backup_runtime(tmp_path, *, archived=True):
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope, ScopeRef

    root = tmp_path / 'native-runtime'
    config = tmp_path / 'native-config'
    config.mkdir()
    (config / 'rpc.env').write_text('FIXTURE=1\n')
    scope = ScopeRef(tenant_id='audit', agent_id='agent', workspace_id='workspace', user_id='user')
    with Runtime.create(root=root) as runtime:
        record = RecordEnvelope.create(kind='capability_score', title='Backup authority fixture',
            summary='Backup authority fixture', scope=scope, source='fixture', source_id='fixture',
            content={'samples': ['old-version-' + 'x' * (200000 if archived else 10)]})
        runtime.store.append(record)
        inline = RecordEnvelope.create(kind='memory', title='Inline fixture', summary='Inline fixture',
            content={'text': 'Inline fixture', 'memory_type': 'fact'}, scope=scope, source='fixture')
        runtime.store.append(inline)
    cli = tmp_path / 'native-backup-cli'
    cli.write_text('#!' + sys.executable + '\n'
        'import os, sys, json\n'
        f'sys.path.insert(0, {str(REPO)!r})\n'
        'from eimemory.api.runtime import Runtime\n'
        'from eimemory.compatibility.migration_helpers import backup_create, backup_verify\n'
        'command, path = sys.argv[2:]\n'
        'if command == "create":\n'
        '    with Runtime.create(root=os.environ["EIMEMORY_ROOT"]) as runtime:\n'
        '        result = backup_create(runtime, path)\n'
        'else:\n'
        '    result = backup_verify(path)\n'
        'print(json.dumps(result))\n'
        'raise SystemExit(0 if result["ok"] else 1)\n')
    cli.chmod(0o755)
    return root, config, str(cli), record, inline


def _restored_runtime(tmp_path, backup):
    import shutil
    from eimemory.api.runtime import Runtime

    root = tmp_path / 'restored'
    shutil.copytree(backup / 'sqlite', root / 'state')
    return Runtime.create(root=root)


@pytest.mark.parametrize('archived', [False, True])
def test_native_backup_restores_inline_and_archived_authority(tmp_path, archived):
    module = _module()
    root, config, cli, record, inline = _native_backup_runtime(tmp_path, archived=archived)
    report = module.run_backup(root=root, config_dir=config, backup_root=tmp_path / 'backups',
                               eimemory_bin=cli, keep=2)
    assert report['ok'] is True
    assert report['payload_archive']['validated_record_count'] == 2
    assert report['payload_archive']['archived_record_count'] == int(archived)
    backup = Path(report['path'])
    with _restored_runtime(tmp_path, backup) as restored:
        for expected in (record, inline):
            actual = restored.store.get_by_id(expected.record_id, scope=expected.scope)
            assert actual.to_dict() == expected.to_dict()
    manifest = json.loads((backup / 'backup-set.json').read_text())
    segments = [item for item in manifest['files'] if item['path'].endswith('.seg')]
    assert bool(segments) is archived
    for item in segments:
        assert item['path'].startswith('sqlite/payload_segments/')
        assert module._sha256(backup / item['path']) == item['sha256']


def test_online_update_after_sqlite_snapshot_preserves_snapshot_payload(tmp_path, monkeypatch):
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope

    module = _module()
    root, config, cli, record, inline = _native_backup_runtime(tmp_path)
    original = module._backup_sqlite

    def update_after_snapshot(source, target):
        result = original(source, target)
        with Runtime.create(root=root) as writer:
            changed = RecordEnvelope.from_dict(record.to_dict())
            changed.content = {'samples': ['new-version-' + 'y' * 200000]}
            writer.store.append(changed)
        return result

    monkeypatch.setattr(module, '_backup_sqlite', update_after_snapshot)
    report = module.run_backup(root=root, config_dir=config, backup_root=tmp_path / 'backups',
                               eimemory_bin=cli, keep=2)
    assert report['ok'] is True
    backup = Path(report['path'])
    with _restored_runtime(tmp_path, backup) as restored:
        actual = restored.store.get_by_id(record.record_id, scope=record.scope)
        assert actual.to_dict() == record.to_dict()
    # The separately captured logical export can be newer, but never replaces
    # the version bound by the saved database's pointer.
    logical = [json.loads(line) for line in (backup / 'records/backup.jsonl').read_text().splitlines()]
    assert next(row for row in logical if row['record_id'] == record.record_id)['content'] != record.content
    native_segment = next((root / 'state/payload_segments').glob('*.seg'))
    saved_segment = backup / 'sqlite/payload_segments' / native_segment.name
    assert saved_segment.stat().st_size < native_segment.stat().st_size


@pytest.mark.parametrize('damage', ['missing_source', 'corrupt_source', 'corrupt_staged'])
def test_bad_archive_never_publishes_or_prunes_previous_backup(tmp_path, monkeypatch, damage):
    module = _module()
    root, config, cli, _, _ = _native_backup_runtime(tmp_path)
    backups = tmp_path / 'backups'
    previous = module.run_backup(root=root, config_dir=config, backup_root=backups,
        eimemory_bin=cli, keep=1, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert previous['ok'] is True
    previous_manifest = (Path(previous['path']) / 'backup-set.json').read_bytes()
    segment = next((root / 'state/payload_segments').glob('*.seg'))
    if damage == 'missing_source':
        segment.unlink()
    elif damage == 'corrupt_source':
        blob = segment.read_bytes()
        segment.write_bytes(blob[:-1] + bytes([blob[-1] ^ 0xFF]))
    else:
        original = module._archive_config

        def corrupt_after_initial_verification(config_dir, target):
            result = original(config_dir, target)
            staged = next((target.parent / 'sqlite/payload_segments').glob('*.seg'))
            blob = staged.read_bytes()
            staged.write_bytes(blob[:-1] + bytes([blob[-1] ^ 0xFF]))
            return result

        monkeypatch.setattr(module, '_archive_config', corrupt_after_initial_verification)
    monkeypatch.setattr(module, '_prune', lambda *args: pytest.fail('must not prune after failed verification'))
    with pytest.raises((OSError, ValueError, RuntimeError)):
        module.run_backup(root=root, config_dir=config, backup_root=backups,
            eimemory_bin=cli, keep=1, now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert (Path(previous['path']) / 'backup-set.json').read_bytes() == previous_manifest
    assert sorted(path.name for path in backups.iterdir()) == ['20260101T000000Z']


def test_backup_restores_referenced_frames_across_rotated_segments(tmp_path):
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope

    module = _module()
    root, config, cli, record, inline = _native_backup_runtime(tmp_path, archived=False)
    records = []
    with Runtime.create(root=root) as runtime:
        def configure(store):
            store.payload_archive_inline_bytes = 256
            store.payload_segments.max_segment_bytes = 4096
        runtime.store.run_locked(configure)
        for index in range(3):
            value = RecordEnvelope.create(kind='capability_score', title='Rotated archive fixture',
                summary='Rotated archive fixture', scope=record.scope, source='fixture',
                content={'fixture_index': index, 'samples': [os.urandom(2000).hex()]})
            runtime.store.append(value)
            records.append(value)
    assert len(list((root / 'state/payload_segments').glob('*.seg'))) == 3
    report = module.run_backup(root=root, config_dir=config, backup_root=tmp_path / 'backups',
                               eimemory_bin=cli, keep=2)
    assert report['ok'] is True
    assert report['payload_archive']['archived_record_count'] == 3
    with _restored_runtime(tmp_path, Path(report['path'])) as restored:
        for expected in [record, inline, *records]:
            actual = restored.store.get_by_id(expected.record_id, scope=expected.scope)
            assert actual.to_dict() == expected.to_dict()
