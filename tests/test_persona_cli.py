from __future__ import annotations

import json

from eimemory.cli.main import main as cli_main


def _read_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_persona_cli_smoke_commands(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main(["persona", "show"]) == 0
    assert _read_json(capsys)["identity"] == "hongtu"

    assert cli_main(["persona", "route", "--text", "用 Codex 实现并补测试"]) == 0
    assert _read_json(capsys)["scene"] == "coding_plan"

    assert cli_main(["persona", "guidance", "--text", "回复短一点"]) == 0
    assert "Persona guidance:" in _read_json(capsys)["text"]

    assert cli_main(["persona", "correct", "--text", "戏很多啊"]) == 0
    assert _read_json(capsys)["category"] == "verbosity"

    assert cli_main(["persona", "evolve", "--dry-run"]) == 0
    assert _read_json(capsys)["ok"] is True

    assert cli_main(["persona", "eval"]) == 0
    assert _read_json(capsys)["ok"] is True
