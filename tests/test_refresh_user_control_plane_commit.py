from pathlib import Path

from deploy.refresh_user_control_plane_commit import RefreshError, refresh_commit
import pytest


SAMPLE = """
[Service]
Environment=EIMEMORY_RUNTIME_COMMIT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
Environment=PYTHONPYCACHEPREFIX=/var/lib/eimemory/.pycache/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
ExecStartPre=/opt/eimemory/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/.venv/bin/python -c true /opt/eimemory/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
PrivateUsers=yes
EnvironmentFile=/etc/eimemory/rpc.env

"""


def test_refresh_replaces_only_exact_commit_tokens():
    rendered = refresh_commit(SAMPLE.encode(), "b" * 40).decode()
    assert rendered.count("b" * 40) == 4
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in rendered
    assert "PrivateUsers=yes" in rendered
    assert "EnvironmentFile=/etc/eimemory/rpc.env" in rendered


def test_refresh_rejects_missing_or_customized_commit_assignment():
    with pytest.raises(RefreshError):
        refresh_commit(b"[Service]\nPrivateUsers=yes\n", "b" * 40)
    customized = SAMPLE.replace(
        "Environment=EIMEMORY_RUNTIME_COMMIT=",
        "Environment=EIMEMORY_RUNTIME_COMMIT=custom-",
    )
    with pytest.raises(RefreshError):
        refresh_commit(customized.encode(), "b" * 40)


def test_refresh_is_idempotent_for_same_commit(tmp_path: Path):
    target = tmp_path / "zzzzzz-eimemory-user-control-plane.conf"
    target.write_bytes(SAMPLE.encode())
    before = target.read_bytes()
    from deploy.refresh_user_control_plane_commit import write_refresh
    write_refresh(target, "a" * 40)
    assert target.read_bytes() == before
