"""Private, bounded host handoff state; acknowledgements alone remove payloads.

The profile is supplied by the host, and the namespace includes the effective
authorization scope and RPC endpoint. This store cannot grant remote authority.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
from typing import Any

from eimemory.storage.atomic_file import (
    atomic_write_bytes, atomic_write_json, interprocess_lock, open_regular_binary, read_json_strict,
)

MAX_ENTRIES = 1024
MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRY_BYTES = 2 * 1024 * 1024


def _private_user_mapping() -> bool:
    try:
        rows = [tuple(map(int, line.split())) for line in
                Path('/proc/self/uid_map').read_text().splitlines()]
        return bool(rows) and not any(start <= 0 < start + count
                                     for start, _outside, count in rows)
    except (OSError, ValueError):
        return False


def _host_root_owner(directory: Path, info: Any) -> bool:
    """Resolve an unmapped ancestor through the existing user manager.

    Only a root-owned, identical inode/mode/ctime is admitted. No permissions,
    namespace settings, or files are changed by the fixed stdlib probe.
    Probe failure and any identity mismatch remain fail-closed.
    """
    if info.st_uid != 65534 or not _private_user_mapping():
        return False
    code = ('import os,sys,json; s=os.lstat(sys.argv[1]); '
            'print(json.dumps([s.st_uid,s.st_dev,s.st_ino,s.st_mode,s.st_ctime_ns]))')
    try:
        result = subprocess.run([
            '/usr/bin/systemd-run', '--user', '--wait', '--pipe', '--collect',
            '--quiet', '/usr/bin/python3', '-I', '-B', '-c', code, str(directory)],
            capture_output=True, text=True, timeout=10,
            env={key: value for key, value in os.environ.items()
                 if key in {'PATH', 'HOME', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'}})
        if result.returncode != 0 or len(result.stdout) > 512:
            return False
        values = json.loads(result.stdout)
        return (isinstance(values, list) and all(type(v) is int for v in values)
                and values == [0, info.st_dev, info.st_ino, info.st_mode, info.st_ctime_ns])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def handoff_root() -> Path:
    """Private memory data, independent of the native gateway workspace."""
    configured = os.environ.get('EIMEMORY_HERMES_HANDOFF_ROOT', '')
    root = Path(configured).expanduser() if configured else Path.home()/'.local/state/eimemory/hermes-handoff'
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('outbox_root_invalid')
    return root


class HermesOutbox:
    def __init__(self, home: str, namespace: dict[str, Any]) -> None:
        self.namespace = deepcopy(namespace)
        self.profile = str(Path(home).expanduser().resolve()) if home else ''
        self.legacy_path = (Path(self.profile)/'state'/
            ('eimemory-outbox-'+digest(namespace)[:24]+'.json')) if home else None
        self.path = (handoff_root()/digest({'profile':self.profile,'namespace':namespace})/'outbox.json') if home else None
        self.error = ""
        self.recovered_count = 0
        self._lock = threading.RLock()
        self._consumer = threading.Lock()
        self._legacy_migrated = False
        self._verified_root_ancestors: set[tuple] = set()
        try:
            self.recovered_count = len(self.entries())
        except (OSError, ValueError, TypeError):
            # Preserve unreadable state; never overwrite it with an empty queue.
            self.error = "outbox_path_unconfigured" if self.path is None else "outbox_unavailable"

    def _empty(self) -> dict[str, Any]:
        return {"schema": "hermes_handoff_outbox.v1", "namespace": self.namespace,
                "entries": {}, "completed_count": 0, "rejected_count": 0, "next_sequence": 0}

    def _verified_host_root(self, directory: Path, info: Any) -> bool:
        key = (str(directory), info.st_dev, info.st_ino, info.st_mode,
               info.st_ctime_ns, info.st_uid)
        if key in self._verified_root_ancestors:
            return True
        if not _host_root_owner(directory, info):
            return False
        if len(self._verified_root_ancestors) >= 64:
            self._verified_root_ancestors.clear()
        self._verified_root_ancestors.add(key)
        return True

    def _directory_tree(self, parent: Path, *, create: bool) -> None:
        # The immediate state directory alone is insufficient: a writable
        # profile/ancestor permits replacement of that supposedly private dir.
        # Sticky shared ancestors (e.g. /tmp) protect our owned descendant.
        for directory in (parent, *parent.parents):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and (
                    (info.st_uid not in {0, os.geteuid()} and not (
                        directory not in {parent, parent.parent}
                        and self._verified_host_root(directory, info)))
                    or (info.st_mode & 0o022 and (
                        directory in {parent, parent.parent} or not info.st_mode & stat.S_ISVTX))))):
                raise ValueError("outbox_ancestor_untrusted")
        # pathlib's parents=True applies mode only to the leaf. Create every
        # missing ancestor explicitly so a permissive umask cannot create a
        # group-writable profile that our next validation rejects. Existing
        # directories retain their permissions and must pass the checks above.
        for directory in reversed((parent, *parent.parents)) if create else ():
            if directory.exists():continue
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("outbox_ancestor_untrusted")
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and (
                info.st_uid != os.geteuid() or info.st_mode & 0o022)):
            raise ValueError("outbox_directory_untrusted")

    def _prepare(self) -> None:
        if self.path is None:
            raise ValueError("outbox_path_unconfigured")
        self._directory_tree(self.path.parent, create=True)
        if os.path.lexists(self.path):
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (os.name == "posix" and (
                        info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise ValueError("outbox_file_untrusted")

    def _private_bytes(self, path: Path) -> tuple[bytes, tuple]:
        """Read exact private bytes and bind the descriptor to the live name."""
        self._directory_tree(path.parent, create=False)
        with open_regular_binary(path) as handle:
            before = os.fstat(handle.fileno())
            if (before.st_size > MAX_BYTES or os.name == 'posix' and (
                    before.st_uid != os.geteuid() or before.st_mode & 0o077)):
                raise ValueError('outbox_file_untrusted')
            raw = handle.read(MAX_BYTES+1)
            identity = lambda s:(s.st_dev,s.st_ino,s.st_uid,s.st_gid,s.st_mode,
                s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
            original = identity(before)
            if (len(raw)>MAX_BYTES or original != identity(os.fstat(handle.fileno()))
                    or original != identity(path.lstat())):
                raise ValueError('outbox_legacy_changed')
        self._directory_tree(path.parent, create=False)
        return raw, original

    def _migrate_legacy(self) -> None:
        """One exact locked handoff; never merge or replay a changed old queue.

        Archive, new queue, then migration receipt are durable before removing
        the legacy name. Each interrupted publication can resume with exact
        bytes. An older process recreating the legacy queue fails closed.
        """
        assert self.path is not None
        legacy = self.legacy_path
        if self._legacy_migrated:
            if not os.path.lexists(self.path):raise ValueError('outbox_migration_destination_missing')
            if not os.path.lexists(legacy):return
        receipt = self.path.with_name('legacy-migration.json')
        archive = self.path.with_name('legacy.original.json')
        expected = dict(schema='hermes_handoff_outbox_migration.v1',
            profile=self.profile,namespace=self.namespace,legacy_path=str(legacy))
        recorded = None
        if os.path.lexists(receipt):
            self._private_bytes(receipt)
            recorded = read_json_strict(receipt,dict,max_bytes=65536)
            if (not all(recorded.get(key)==value for key,value in expected.items())
                    or set(recorded)!=set(expected)|{'sha256'}
                    or not isinstance(recorded.get('sha256'),str) or len(recorded['sha256'])!=64):
                raise ValueError('outbox_migration_receipt_invalid')
            archived,_ = self._private_bytes(archive)
            if sha256(archived).hexdigest()!=recorded['sha256']:
                raise ValueError('outbox_migration_archive_changed')
            # The archive predates acknowledgements. It cannot restore a lost
            # current queue without potentially replaying completed entries.
            if not os.path.lexists(self.path):raise ValueError('outbox_migration_destination_missing')
        if legacy is None or not os.path.lexists(legacy):
            self._legacy_migrated = recorded is not None
            return
        # A cooperative old-version writer uses this same legacy lock. It is
        # acquired only after the independent private destination lock.
        self._directory_tree(legacy.parent,create=False)
        # The old consumer can hold an in-flight RPC outside its data lock.
        # Wait for that ownership too, before copying an unacknowledged entry.
        with interprocess_lock(legacy.with_suffix('.consumer.lock'),timeout=2.0), \
                interprocess_lock(legacy.with_suffix('.lock'),timeout=2.0):
            if not os.path.lexists(legacy):return
            raw,original = self._private_bytes(legacy)
            source_digest = sha256(raw).hexdigest()
            state = read_json_strict(legacy,dict,max_bytes=MAX_BYTES)
            self._validate(state)
            if recorded is not None:
                if source_digest != recorded['sha256']:
                    raise ValueError('outbox_legacy_changed_after_migration')
            else:
                if os.path.lexists(archive):
                    archived,_ = self._private_bytes(archive)
                    if archived != raw:raise ValueError('outbox_migration_archive_changed')
                else:atomic_write_bytes(archive,raw)
                if os.path.lexists(self.path):
                    current,_ = self._private_bytes(self.path)
                    if current != raw:raise ValueError('outbox_migration_destination_conflict')
                else:atomic_write_bytes(self.path,raw)
                atomic_write_json(receipt,{**expected,'sha256':source_digest})
            # Re-read/CAS before retirement; do not delete a concurrent rewrite.
            latest,identity = self._private_bytes(legacy)
            if identity != original or latest != raw:raise ValueError('outbox_legacy_changed')
            legacy.unlink()
            if os.name=='posix':
                descriptor=os.open(legacy.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
            self._legacy_migrated = True

    def _validate(self, state: dict[str, Any]) -> None:
        if (state.get("schema") != "hermes_handoff_outbox.v1"
                or state.get("namespace") != self.namespace
                or not isinstance(state.get("entries"), dict)
                or len(state["entries"]) > MAX_ENTRIES
                or any(type(state.get(key)) is not int or state[key] < 0
                       for key in ("completed_count", "rejected_count", "next_sequence"))):
            raise ValueError("outbox_schema_invalid")
        for key, entry in state["entries"].items():
            if (not isinstance(key, str) or not key or len(key) > 200
                    or not isinstance(entry, dict) or entry.get("kind") not in {"write", "delivery"}
                    or not isinstance(entry.get("payload"), dict)
                    or type(entry.get("sequence")) is not int or entry["sequence"] < 0
                    or entry["sequence"] >= state["next_sequence"]
                    or type(entry.get("stage")) is not int or entry["stage"] not in {0, 1}
                    or type(entry.get("attempts")) is not int or entry["attempts"] < 0
                    or not isinstance(entry.get("last_error"), str)
                    or entry.get("status") not in {"pending", "rejected"}):
                raise ValueError("outbox_entry_invalid")

    @contextmanager
    def _transaction(self, *, write: bool = False):
        with self._lock:
            try:
                self._prepare()
                assert self.path is not None
                with interprocess_lock(self.path.with_suffix(".lock"), timeout=2.0):
                    self._migrate_legacy()
                    state = (read_json_strict(self.path, dict, max_bytes=MAX_BYTES)
                             if self.path.exists() else self._empty())
                    self._validate(state)
                    yield state
                    if write:
                        atomic_write_json(self.path, state, max_bytes=MAX_BYTES)
                self.error = ""
            except (OSError, ValueError, TypeError):
                self.error = "outbox_path_unconfigured" if self.path is None else "outbox_unavailable"
                raise

    @contextmanager
    def consumer(self):
        """Separate ownership lock: enqueue is possible during a slow RPC."""
        if not self._consumer.acquire(blocking=False):
            yield False
            return
        try:
            self._prepare()
            assert self.path is not None
            with ExitStack() as stack:
                try:
                    stack.enter_context(interprocess_lock(self.path.with_suffix(".consumer.lock"), timeout=0))
                except TimeoutError:
                    yield False
                    return
                yield True
        finally:
            self._consumer.release()

    def put(self, key: str, kind: str, payload: dict[str, Any]) -> bool:
        if kind not in {"write", "delivery"}:
            raise ValueError("outbox_kind_invalid")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > MAX_ENTRY_BYTES:
            raise ValueError("outbox_payload_oversized")
        with self._transaction(write=True) as state:
            old = state["entries"].get(key)
            if old is not None:
                if old["kind"] != kind or old["payload"] != payload:
                    raise ValueError("outbox_event_conflict")
                return False
            if len(state["entries"]) >= MAX_ENTRIES:
                raise ValueError("outbox_capacity_exceeded")
            sequence = state["next_sequence"]
            state["next_sequence"] += 1
            state["entries"][key] = {"kind": kind, "payload": deepcopy(payload),
                                      "stage": 0, "attempts": 0,
                                      "sequence": sequence,
                                      "status": "pending", "last_error": ""}
        return True

    def entries(self, kind: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        with self._transaction() as state:
            entries = []
            for key, entry in state["entries"].items():
                if entry["status"] == "pending" and (kind is None or entry["kind"] == kind):
                    entries.append((key, deepcopy(entry)))
            return sorted(entries, key=lambda item: item[1]["sequence"])

    def update(self, key: str, *, stage: int | None = None, outcome: str = "",
               error: str = "") -> None:
        with self._transaction(write=True) as state:
            entry = state["entries"].get(key)
            if entry is None:
                return
            if stage is not None:
                entry["stage"] = stage
            if outcome == "completed":
                state["entries"].pop(key)
                state["completed_count"] += 1
            elif outcome:
                entry["attempts"] += 1
                entry["last_error"] = error
                if outcome == "rejected":
                    entry["status"] = "rejected"
                    state["rejected_count"] += 1

    def status(self) -> dict[str, Any]:
        try:
            with self._transaction() as state:
                values = list(state["entries"].values())
                pending = [e for e in values if e.get("status") == "pending"]
                return {"schema": "hermes_handoff_outbox.v1", "durable": self.path is not None,
                        "pending_writes": sum(e.get("kind") == "write" for e in pending),
                        "pending_deliveries": sum(e.get("kind") == "delivery" for e in pending),
                        "rejected_count": state["rejected_count"],
                        "completed_count": state["completed_count"],
                        "attempt_count": sum(e.get("attempts", 0) for e in values),
                        "recovered_count": self.recovered_count, "error": self.error}
        except (OSError, ValueError, TypeError):
            return {"schema": "hermes_handoff_outbox.v1", "durable": self.path is not None,
                    "pending_writes": None, "pending_deliveries": None,
                    "recovered_count": self.recovered_count, "error": self.error}
