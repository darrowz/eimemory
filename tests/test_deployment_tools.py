from __future__ import annotations

from pathlib import Path

from deploy.rotate_console_token import main as rotate_main
from deploy.rotate_console_token import rotate_token


def test_rotate_console_token_updates_unit_file(tmp_path) -> None:
    unit = tmp_path / "eimemory-console.service"
    unit.write_text(
        "\n".join(
            [
                "[Service]",
                "Environment=EIMEMORY_CONSOLE_TOKEN=old-token",
                "Environment=EIMEMORY_CONSOLE_PORT=8765",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = rotate_token(unit, token="new-token")

    assert report == {"ok": True, "unit_path": str(unit)}
    text = unit.read_text(encoding="utf-8")
    assert "EIMEMORY_CONSOLE_TOKEN=new-token" in text
    assert "old-token" not in text


def test_rotate_console_token_main_hides_token_by_default(tmp_path, capsys) -> None:
    unit = tmp_path / "eimemory-console.service"
    unit.write_text(
        "[Service]\nEnvironment=EIMEMORY_CONSOLE_TOKEN=old-token\n",
        encoding="utf-8",
    )

    assert rotate_main(["--unit", str(unit), "--token", "secret-token"]) == 0

    output = capsys.readouterr().out
    assert "secret-token" not in output
    assert "token_rotated" in output


def test_rotate_console_token_main_can_explicitly_show_token(tmp_path, capsys) -> None:
    unit = tmp_path / "eimemory-console.service"
    unit.write_text(
        "[Service]\nEnvironment=EIMEMORY_CONSOLE_TOKEN=old-token\n",
        encoding="utf-8",
    )

    assert rotate_main(["--unit", str(unit), "--token", "secret-token", "--show-token"]) == 0

    output = capsys.readouterr().out
    assert "secret-token" in output
    assert "new_url=http://<host>:8765/secret-token" in output


def test_console_systemd_uses_packaged_console_entrypoint() -> None:
    unit_text = Path("deploy/systemd/eimemory-console.service").read_text(encoding="utf-8")

    assert "/var/lib/eimemory/governance/serve_console.py" not in unit_text
    assert "python -m eimemory.governance.serve_console" in unit_text
    assert "EIMEMORY_CONFIG_DIR=/etc/eimemory" in unit_text
