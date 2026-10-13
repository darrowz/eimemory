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
import threading
from typing import Any

from eimemory.storage.atomic_file import (
    atomic_write_json, interprocess_lock, read_json_strict,
)

MAX_ENTRIES = 1024
MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRY_BYTES = 2 * 1024 * 1024


def digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class HermesOutbox:
    def __init__(self, home: str, namespace: dict[str, Any]) -> None:
        self.namespace = deepcopy(namespace)
        self.path = (Path(home).expanduser().resolve() / "state" /
                     ("eimemory-outbox-" + digest(namespace)[:24] + ".json")) if home else None
        self.error = ""
        self.recovered_count = 0
        self._lock = threading.RLock()
        self._consumer = threading.Lock()
        try:
            self.recovered_count = len(self.entries())
        except (OSError, ValueError, TypeError):
            # Preserve unreadable state; never overwrite it with an empty queue.
            self.error = "outbox_path_unconfigured" if self.path is None else "outbox_unavailable"

    def _empty(self) -> dict[str, Any]:
        return {"schema": "hermes_handoff_outbox.v1", "namespace": self.namespace,
                "entries": {}, "completed_count": 0, "rejected_count": 0, "next_sequence": 0}

    def _prepare(self) -> None:
        if self.path is None:
            raise ValueError("outbox_path_unconfigured")
        parent = self.path.parent
        # The immediate state directory alone is insufficient: a writable
        # profile/ancestor permits replacement of that supposedly private dir.
        # Sticky shared ancestors (e.g. /tmp) protect our owned descendant.
        for directory in (parent, *parent.parents):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and (
                    info.st_uid not in {0, os.geteuid()}
                    or (info.st_mode & 0o022 and (
                        directory in {parent, parent.parent} or not info.st_mode & stat.S_ISVTX))))):
                raise ValueError("outbox_ancestor_untrusted")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and (
                info.st_uid != os.geteuid() or info.st_mode & 0o022)):
            raise ValueError("outbox_directory_untrusted")
        if os.path.lexists(self.path):
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (os.name == "posix" and (
                        info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise ValueError("outbox_file_untrusted")

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
