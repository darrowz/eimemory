import os
from pathlib import Path
import stat
import pytest

from eimemory.storage.atomic_file import atomic_write_json, read_json_strict
from eimemory.storage.private_file import private_temporary_file


@pytest.mark.parametrize("prefix", ["", "../bad", "bad/name", "bad\\name", "bad\0name"])
def test_private_temp_rejects_unsafe_prefix(prefix, tmp_path):
    with pytest.raises(ValueError):
        private_temporary_file(prefix=prefix, directory=tmp_path)
    assert not list(tmp_path.iterdir())


def test_private_atomic_write_and_replace(tmp_path):
    path = tmp_path / "checkpoint.json"
    atomic_write_json(path, {"sequence": 1})
    atomic_write_json(path, {"sequence": 2})
    assert read_json_strict(path, dict) == {"sequence": 2}
    assert list(tmp_path.iterdir()) == [path]
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_private_creation_failure_preserves_existing_bytes(tmp_path, monkeypatch):
    from eimemory.storage import atomic_file
    path = tmp_path / "checkpoint.json"
    path.write_bytes(b'original')
    def denied(**_kwargs):
        raise PermissionError("private_acl_unavailable")
    monkeypatch.setattr(atomic_file, "private_temporary_file", denied)
    with pytest.raises(PermissionError):
        atomic_file.atomic_write_bytes(path, b'new')
    assert path.read_bytes() == b'original'


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows security descriptors")
def test_windows_state_has_protected_current_user_and_system_dacl(tmp_path):
    import ctypes as c
    from ctypes import wintypes as w
    import re
    import subprocess

    path = tmp_path / "checkpoint.json"
    for sequence in (1, 2):
        atomic_write_json(path, {"sequence": sequence})
        security = c.WinDLL("advapi32", use_last_error=True)
        kernel = c.WinDLL("kernel32", use_last_error=True)
        security.GetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
        security.GetFileSecurityW.restype = w.BOOL
        security.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            c.c_void_p, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), c.POINTER(w.DWORD)]
        security.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = w.BOOL
        kernel.LocalFree.argtypes = [c.c_void_p]
        kernel.LocalFree.restype = c.c_void_p
        size = w.DWORD()
        security.GetFileSecurityW(str(path), 4, None, 0, c.byref(size))
        assert size.value > 0
        buffer = c.create_string_buffer(size.value)
        assert security.GetFileSecurityW(str(path), 4, buffer, len(buffer), c.byref(size))
        text = w.LPWSTR()
        assert security.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer, 1, 4, c.byref(text), None)
        try:
            sddl = text.value
        finally:
            kernel.LocalFree(c.cast(text, c.c_void_p))
        identity = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True)
        sid = re.search(r"S-1-(?:\d+-)+\d+", identity).group(0)
        assert sddl.startswith("D:P"), sddl
        assert set(re.findall(r"\(A;;FA;;;([^)]*)\)", sddl)) == {"SY", sid}, sddl
        assert sddl.count("(") == 2, sddl
