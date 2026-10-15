from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "install_immutable_release.sh"


def test_restored_oneshot_writers_start_without_blocking():
    text = SCRIPT.read_text()
    body = text[text.index("_restart_storage_writers() {"):]
    body = body[:body.index('echo "storage_writer_restart=complete')]
    assert "restore_mode=(--no-block)" in body and "= oneshot" in body
    assert '_user_systemctl start "${restore_mode[@]}" "$unit"' in body
