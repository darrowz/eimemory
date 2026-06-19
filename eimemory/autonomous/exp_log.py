"""Append-only compounding experiment log."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True, frozen=True)
class ExpLogEntry:
    """One row in the compounding experiment log."""

    hypothesis: str
    kept: bool
    elapsed: float
    primary_metric_before: float
    primary_metric_after: float
    experiment_id: str = ""
    outcome: str = ""
    status: str = ""
    primary_metric_name: str = "recall_view.hit@1"
    error: str = ""
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            object.__setattr__(self, "timestamp", datetime.now(timezone.utc).isoformat())
        if not self.outcome:
            object.__setattr__(self, "outcome", "kept" if self.kept else "discarded")


class ExpLog:
    """Small JSONL log used by the Karpathy-loop compounding context."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def append(self, entry: ExpLogEntry) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def read_all(self) -> list[ExpLogEntry]:
        entries: list[ExpLogEntry] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                entry = _normalize_row(payload)
                if entry is not None:
                    entries.append(entry)
        return entries

    def recent_kept(self, n: int = 5) -> list[ExpLogEntry]:
        """Return the last ``n`` rows; callers can filter ``entry.kept``."""
        if n <= 0:
            return []
        return self.read_all()[-n:]


def entry_from_experiment_result(result: Any) -> ExpLogEntry:
    """Build an :class:`ExpLogEntry` from loop/runner result payloads."""
    payload = result if isinstance(result, dict) else dataclasses.asdict(result) if dataclasses.is_dataclass(result) else None
    if not isinstance(payload, dict):
        raise TypeError("experiment result must be a mapping or dataclass payload")
    return _normalize_row(payload) or ExpLogEntry(
        hypothesis="",
        kept=False,
        elapsed=0.0,
        primary_metric_before=0.0,
        primary_metric_after=0.0,
        error="invalid_experiment_result",
    )


def _normalize_row(row: dict[str, Any]) -> ExpLogEntry | None:
    try:
        payload = dict(row)
    except Exception:
        return None
    outcome = str(payload.get("outcome") or payload.get("decision") or payload.get("status") or "").strip().lower()
    kept = _coerce_kept(payload.get("kept"), outcome=outcome)
    return ExpLogEntry(
        experiment_id=str(payload.get("experiment_id") or payload.get("id") or ""),
        hypothesis=_coerce_hypothesis(payload.get("hypothesis")),
        kept=kept,
        elapsed=_coerce_float(payload.get("elapsed"), payload.get("duration_seconds")),
        primary_metric_before=_coerce_float(
            payload.get("primary_metric_before"),
            payload.get("baseline_value"),
            payload.get("baseline"),
            payload.get("metric_before"),
        ),
        primary_metric_after=_coerce_float(
            payload.get("primary_metric_after"),
            payload.get("candidate_value"),
            payload.get("after"),
            payload.get("metric_after"),
        ),
        outcome=outcome,
        status=str(payload.get("status") or ""),
        primary_metric_name=str(payload.get("primary_metric_name") or payload.get("metric_name") or "recall_view.hit@1"),
        error=str(payload.get("error") or ""),
        timestamp=str(payload.get("timestamp") or payload.get("finished_at") or payload.get("started_at") or ""),
    )


def _coerce_kept(value: Any, *, outcome: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is not None:
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "kept", "keep"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "discarded", "discard"}:
            return False
    return outcome in {"kept", "keep"}


def _coerce_float(*values: Any) -> float:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _coerce_hypothesis(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)
