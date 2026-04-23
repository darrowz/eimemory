from __future__ import annotations

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

    assert report == {"ok": True, "unit_path": str(unit), "token": "new-token"}
    text = unit.read_text(encoding="utf-8")
    assert "EIMEMORY_CONSOLE_TOKEN=new-token" in text
    assert "old-token" not in text
