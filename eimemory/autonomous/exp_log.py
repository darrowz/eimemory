"""Compounding experiment log. JSONL append-only.

Per the 2026-06-17 Karpathy Loop plan, every experiment in the
single-experiment runner (``loop.py``) writes a row here. The
compounding context builder (``compounding.py``) reads
``recent_kept()`` to assemble the next experiment's prior context.

Append-only by design: the log is never rewritten. Reads stream the
JSONL file line by line.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(slots=True, frozen=True)
class ExpLogEntry:
    """One row in the compounding experiment log.

    ``timestamp`` is auto-filled with the current UTC ISO-8601 string
    when the caller does not provide one. ``frozen=True`` makes the
    row immutable once written; ``__post_init__`` uses
    ``object.__setattr__`` to assign the default timestamp because
    frozen dataclasses reject normal attribute assignment.
    """

    hypothesis: str
    kept: bool
    elapsed: float
    primary_metric_before: float
    primary_metric_after: float
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            object.__setattr__(
                self, "timestamp", datetime.now(timezone.utc).isoformat()
            )


class ExpLog:
    """Append-only JSONL experiment log.

    The path is created on first use. ``read_all`` and ``recent_kept``
    are read-only; only ``append`` mutates the file.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def append(self, entry: ExpLogEntry) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry)) + "\n")

    def read_all(self) -> list[ExpLogEntry]:
        entries: list[ExpLogEntry] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entries.append(ExpLogEntry(**json.loads(line)))
        return entries

    def recent_kept(self, n: int = 5) -> list[ExpLogEntry]:
        """Return the last ``n`` entries (the compounding window).

        Note: this returns the recent window, not a ``kept``-filtered
        slice. Callers wanting only kept experiments must filter
        themselves. The default ``n=5`` matches the 2026-06-17
        spec's "Last 5 kept experiments" compounding cap.
        """
        all_entries = self.read_all()
        return all_entries[-n:]
