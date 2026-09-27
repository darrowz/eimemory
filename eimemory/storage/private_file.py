"""Create private temporary files without relying on Windows chmod emulation.

Parents must be trusted. POSIX uses mkstemp's 0600; Windows creates the file
with a protected DACL granting only the process user and LocalSystem access.
No permissive fallback is allowed on filesystems without persistent ACLs.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile


def private_temporary_file(*, prefix: str, directory: str | Path) -> tuple[int, str]:
    if not prefix or any(c in prefix for c in ("/", "\\", "\0")):
        raise ValueError("invalid_private_temp_prefix")
    if os.name != "nt":
        return tempfile.mkstemp(prefix=prefix, dir=directory)
    if any(c in prefix for c in (":", "*", "?", '"', "<", ">", "|")):
        raise ValueError("invalid_private_temp_prefix")
    return _windows_private_temporary_file(prefix=prefix, directory=directory)


def _windows_private_temporary_file(*, prefix: str, directory: str | Path) -> tuple[int, str]:
    import ctypes as c
    from ctypes import wintypes as w
    import msvcrt
    import uuid

    kernel = c.WinDLL("kernel32", use_last_error=True)
    security = c.WinDLL("advapi32", use_last_error=True)

    class SecurityAttributes(c.Structure):
        _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", c.c_void_p), ("bInheritHandle", w.BOOL)]

    class SidAndAttributes(c.Structure):
        _fields_ = [("Sid", c.c_void_p), ("Attributes", w.DWORD)]

    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    kernel.GetVolumePathNameW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
    kernel.GetVolumePathNameW.restype = w.BOOL
    kernel.GetVolumeInformationW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD, c.POINTER(w.DWORD),
                                           c.POINTER(w.DWORD), c.POINTER(w.DWORD), w.LPWSTR, w.DWORD]
    kernel.GetVolumeInformationW.restype = w.BOOL
    security.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    security.OpenProcessToken.restype = w.BOOL
    security.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    security.GetTokenInformation.restype = w.BOOL
    security.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
    security.ConvertSidToStringSidW.restype = w.BOOL
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD,
                                                                            c.POINTER(c.c_void_p), c.POINTER(w.DWORD)]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(SecurityAttributes),
                                  w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE

    parent = os.path.abspath(os.fspath(directory))
    volume = c.create_unicode_buffer(32768)
    if not kernel.GetVolumePathNameW(parent, volume, len(volume)):
        raise c.WinError(c.get_last_error())
    flags = w.DWORD()
    if not kernel.GetVolumeInformationW(volume.value, None, 0, None, None, c.byref(flags), None, 0):
        raise c.WinError(c.get_last_error())
    if not flags.value & 0x00000008:  # FILE_PERSISTENT_ACLS
        raise PermissionError("private_state_requires_persistent_acls")

    token = w.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, c.byref(token)):  # TOKEN_QUERY
        raise c.WinError(c.get_last_error())
    sid_text = w.LPWSTR()
    try:
        size = w.DWORD()
        security.GetTokenInformation(token, 1, None, 0, c.byref(size))  # TokenUser
        if not size.value or size.value > 1024 * 1024:
            raise OSError("invalid_token_user_buffer")
        buffer = c.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, buffer, size.value, c.byref(size)):
            raise c.WinError(c.get_last_error())
        user = c.cast(buffer, c.POINTER(SidAndAttributes)).contents
        if not security.ConvertSidToStringSidW(user.Sid, c.byref(sid_text)):
            raise c.WinError(c.get_last_error())
        user_sid = str(sid_text.value)
    finally:
        if sid_text:
            kernel.LocalFree(c.cast(sid_text, c.c_void_p))
        kernel.CloseHandle(token)

    descriptor = c.c_void_p()
    # P disables inherited ACEs. The descriptor is supplied at CREATE_NEW,
    # before any sensitive byte can be written, not chmod'ed after creation.
    sddl = f"D:P(A;;FA;;;SY)(A;;FA;;;{user_sid})"
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None):
        raise c.WinError(c.get_last_error())
    try:
        attrs = SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
        for _ in range(32):
            path = os.path.join(parent, prefix + uuid.uuid4().hex)
            native_path = path
            if not path.startswith("\\\\?\\"):
                native_path = "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path
            handle = kernel.CreateFileW(native_path, 0xC0000000, 0, c.byref(attrs), 1, 0x80, None)
            if handle == c.c_void_p(-1).value:
                error = c.get_last_error()
                if error in (80, 183):
                    continue
                raise c.WinError(error)
            try:
                fd = msvcrt.open_osfhandle(int(handle), os.O_RDWR | os.O_BINARY)
            except BaseException:
                kernel.CloseHandle(handle)
                os.unlink(path)
                raise
            try:
                os.set_inheritable(fd, False)
            except BaseException:
                os.close(fd)
                os.unlink(path)
                raise
            return fd, path
        raise FileExistsError("private_temp_name_collisions")
    finally:
        kernel.LocalFree(descriptor)
