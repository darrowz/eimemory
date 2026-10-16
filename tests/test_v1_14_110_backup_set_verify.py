import json

from eimemory.cli import main as cli


def test_backup_verify_accepts_backup_set_dir(tmp_path, monkeypatch, capsys):
    (tmp_path / "records").mkdir()
    (tmp_path / "records" / "backup.manifest.json").write_text("{}")
    (tmp_path / "backup-set.json").write_text("{}")
    seen = []
    monkeypatch.setattr(cli, "backup_verify", lambda path: seen.append(path) or {"ok": True, "record_count": 1})
    parsed = type("P", (), {"backup_command": "verify", "path": str(tmp_path)})()
    assert cli._cmd_backup(parsed, None, {}) == 0
    assert seen == [str(tmp_path / "records" / "backup")]
    assert json.loads(capsys.readouterr().out)["backup_set"] == str(tmp_path)


def test_backup_verify_plain_path_unchanged(tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli, "backup_verify", lambda path: seen.append(path) or {"ok": False})
    parsed = type("P", (), {"backup_command": "verify", "path": str(tmp_path)})()
    assert cli._cmd_backup(parsed, None, {}) == 1
    assert seen == [str(tmp_path)]
