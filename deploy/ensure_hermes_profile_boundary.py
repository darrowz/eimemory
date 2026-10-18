"""Manage the standalone Feishu ingress boundary without replacing host files.

The supported legacy entry starts with the native admission call. The known
15-line compatibility wrapper (including installations predating this helper)
is adopted in place. An AST-equivalent native wrapper needs no patch. Other
entry contracts fail closed, while unrelated host edits remain untouched.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

SCHEMA = "hermes_standalone_feishu_profile_boundary.v1"
HEADER = "    async def _handle_message(self, event: MessageEvent) -> Optional[str]:\n"
INNER = "_handle_message_in_profile"
INSERT = '''        # A standalone Feishu gateway belongs to its launch profile.  A contextvar inherited
        # from another profile must never redirect admission, session persistence or the agent.
        if (getattr(getattr(event, "source", None), "platform", None) == Platform.FEISHU
                and not getattr(getattr(self, "config", None), "multiplex_profiles", False)):
            from hermes_constants import (
                get_process_hermes_home, reset_hermes_home_override, set_hermes_home_override,
            )
            token = set_hermes_home_override(get_process_hermes_home())
            try:
                return await self._handle_message_in_profile(event)
            finally:
                reset_hermes_home_override(token)
        return await self._handle_message_in_profile(event)

    async def _handle_message_in_profile(self, event: MessageEvent) -> Optional[str]:
'''
ADMISSION = '''from gateway.run import _AGENT_PENDING_SENTINEL
_admitted = await self._hm_admit_event(event)
if _admitted is None:
    return None
'''


def _unknown(reason):
    raise RuntimeError("hermes_profile_boundary_contract_unknown:" + reason)


def _body(node):
    statements = node.body
    if (statements and isinstance(statements[0], ast.Expr)
            and isinstance(statements[0].value, ast.Constant)
            and isinstance(statements[0].value.value, str)):
        statements = statements[1:]
    return statements


def _dump(nodes):
    return [ast.dump(node, include_attributes=False) for node in nodes]


def _insert_for(source):
    if "\r" in source:
        if source.count("\r\n") != source.count("\n") or source.count("\r") != source.count("\n"):
            _unknown("line_endings")
        return INSERT.replace("\n", "\r\n")
    return INSERT


def _signature(node):
    args = node.args
    return (isinstance(node, ast.AsyncFunctionDef) and not node.decorator_list
            and [arg.arg for arg in args.args] == ["self", "event"]
            and not (args.posonlyargs or args.kwonlyargs or args.defaults
                     or args.kw_defaults or args.vararg or args.kwarg))


def _admission(node):
    if not _signature(node):
        return False
    expected = ast.parse("async def core(self, event):\n" + "\n".join(
        "    " + line for line in ADMISSION.splitlines())).body[0]
    return _dump(_body(node)[:3]) == _dump(expected.body)


def _contract(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        _unknown("syntax")
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef)
              and node.name == "GatewayInboundMixin"]
    if len(owners) != 1:
        _unknown("owner")
    methods = [node for node in owners[0].body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    entries = [node for node in methods if node.name == "_handle_message"]
    if len(entries) != 1 or not _signature(entries[0]):
        _unknown("entry")
    entry = entries[0]
    statements = _body(entry)
    if statements and isinstance(statements[-1], ast.Return):
        tail = statements[-1].value
        if (isinstance(tail, ast.Await) and isinstance(tail.value, ast.Call)
                and isinstance(tail.value.func, ast.Attribute)
                and isinstance(tail.value.func.value, ast.Name)
                and tail.value.func.value.id == "self"):
            name = tail.value.func.attr
            targets = [node for node in methods if node.name == name]
            if name.startswith("_handle_message_") and len(targets) == 1 and _admission(targets[0]):
                template = ast.parse("class Boundary:\n" + HEADER + INSERT + "        pass\n").body[0].body[0]
                for node in ast.walk(template):
                    if isinstance(node, ast.Attribute) and node.attr == INNER:
                        node.attr = name
                if _dump(statements) == _dump(template.body):
                    if name == INNER and source.count(_insert_for(source)) == 1:
                        return "managed_compat", entry
                    return "native_equivalent", entry
    if any(node.name == INNER for node in methods):
        _unknown("partial_or_drifted_wrapper")
    lines = source.splitlines(keepends=True)
    if not _admission(entry) or lines[entry.lineno - 1].rstrip("\r\n") != HEADER.rstrip("\n"):
        _unknown("admission")
    return "legacy_admission", entry


def _atomic(path, content, mode):
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _host_lock(root):
    """Serialize every gateway's startup patch on the shared checkout."""
    if os.name != "posix":
        yield
        return
    import fcntl
    parent = root / "gateway"
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        descriptor = os.open(".eimemory-profile-boundary.lock",
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o022):
            raise RuntimeError("hermes_profile_boundary_lock_untrusted")
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("hermes_profile_boundary_lock_timeout")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def install(host_root, *, apply=False, remove=False, check=False, startup=False):
    root = Path(host_root).resolve(strict=True)
    if apply:
        with _host_lock(root):
            return _install_locked(root, apply=apply, remove=remove, check=check, startup=startup)
    return _install_locked(root, apply=apply, remove=remove, check=check, startup=startup)


def _install_locked(host_root, *, apply=False, remove=False, check=False, startup=False):
    if sum(bool(value) for value in (apply, check)) > 1 or (check and remove):
        raise ValueError("profile boundary operation invalid")
    if startup and (not apply or remove):
        raise ValueError("profile boundary startup requires apply")
    root = Path(host_root).resolve(strict=True)
    manager = root / "gateway/run_inbound.py"
    if (manager.parent.is_symlink() or manager.is_symlink()
            or not stat.S_ISREG(manager.lstat().st_mode) or manager.stat().st_nlink != 1):
        raise RuntimeError("hermes_profile_boundary_path_untrusted")
    original = manager.read_bytes()
    if len(original) > 2_000_000:
        raise RuntimeError("hermes_profile_boundary_source_too_large")
    source = original.decode("utf-8")
    managed_insert = _insert_for(source)
    contract, entry = _contract(source)
    if check and contract == "legacy_admission":
        raise RuntimeError("hermes_profile_boundary_missing")
    target = source
    backup_source = None
    if contract == "managed_compat":
        backup_source = source.replace(managed_insert, "", 1)
        if _contract(backup_source)[0] != "legacy_admission":
            _unknown("unpatch")
        if remove:
            target = backup_source
    elif contract == "legacy_admission" and not remove:
        backup_source = source
        lines = source.splitlines(keepends=True)
        offset = sum(map(len, lines[:_body(entry)[0].lineno - 1]))
        target = source[:offset] + managed_insert + source[offset:]
        if _contract(target)[0] != "managed_compat":
            _unknown("patched_readback")
    backup = None
    mode = stat.S_IMODE(manager.stat().st_mode)
    if backup_source is not None:
        digest = hashlib.sha256(backup_source.encode()).hexdigest()
        backup = manager.with_name("run_inbound.py.pre-eimemory-profile-boundary-" + digest[:16])
        if backup.is_symlink() or (backup.exists() and backup.read_bytes() != backup_source.encode()):
            raise RuntimeError("hermes_profile_boundary_backup_conflict")
    if apply:
        # Recheck the original before writing; never overwrite concurrent host edits.
        if manager.is_symlink() or manager.read_bytes() != original:
            raise RuntimeError("hermes_profile_boundary_source_changed")
        if backup is not None and not backup.exists() and not (startup and contract == "managed_compat"):
            _atomic(backup, backup_source.encode(), mode)
        if target != source:
            if manager.is_symlink() or manager.read_bytes() != original:
                raise RuntimeError("hermes_profile_boundary_source_changed")
            _atomic(manager, target.encode(), mode)
        if manager.read_bytes() != target.encode():
            raise RuntimeError("hermes_profile_boundary_readback_failed")
    return {"schema": SCHEMA, "ok": True, "applied": apply, "remove": remove, "startup": startup,
            "already_installed": contract != "legacy_admission", "contract": contract,
            "host_file": "gateway/run_inbound.py", "all_profiles_share_host_boundary": True,
            "source_sha256": hashlib.sha256(original).hexdigest(),
            "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
            "backup": str(backup) if backup is not None else ""}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-agent-root", required=True)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--apply", action="store_true")
    operation.add_argument("--check", action="store_true")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--startup", action="store_true")
    args = parser.parse_args()
    try:
        report = install(args.hermes_agent_root, apply=args.apply, remove=args.remove, check=args.check, startup=args.startup)
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "error": str(exc)}, sort_keys=True))
        raise SystemExit(2)
    print(json.dumps(report, sort_keys=True))
