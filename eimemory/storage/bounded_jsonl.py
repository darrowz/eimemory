"""Bounded, process-safe diagnostic JSONL; not a governance/evidence ledger."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eimemory.core.strict_json import loads as strict_json_loads
from eimemory.storage.atomic_file import (
    atomic_write_bytes,
    interprocess_lock,
    open_regular_binary,
)


def append_bounded_jsonl(
    path: str | Path, entry: dict[str, Any], *, max_bytes: int,
    lock_timeout: float | None = None,
) -> bool:
    """Retain complete newest lines and atomically append one validated entry.

    Requires trusted parent directories. A shared sidecar lock serializes all
    participating processes/instances; an existing final line without a newline
    is treated as an interrupted write and dropped. Oversized entries are not
    written. Filesystem errors are returned to the caller, not mislabeled as a
    successful durable write.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(entry, dict):
        raise TypeError("entry must be an object")
    encoded = (json.dumps(entry, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > max_bytes:
        return False
    strict_json_loads(encoded, max_bytes=max_bytes)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with interprocess_lock(target.with_name(f"{target.name}.lock"), timeout=lock_timeout):
        keep = max_bytes - len(encoded)
        try:
            with open_regular_binary(target) as handle:
                size = handle.seek(0, 2)
                start = max(0, size - keep)
                # Read one boundary byte so a cut exactly after '\n' does not
                # discard an additional valid line.
                handle.seek(max(0, start - 1))
                tail = handle.read(keep + (1 if start else 0))
            if start:
                boundary, tail = tail[:1], tail[1:]
                if boundary != b"\n":
                    newline = tail.find(b"\n")
                    tail = tail[newline + 1:] if newline >= 0 else b""
            if tail and not tail.endswith(b"\n"):
                newline = tail.rfind(b"\n")
                tail = tail[:newline + 1] if newline >= 0 else b""
        except FileNotFoundError:
            tail = b""
        atomic_write_bytes(target, tail + encoded)
    return True
