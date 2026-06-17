"""Candidate promotion state machine: sandbox -> canary -> active (+ rolled_back).

This is Task 1.4 of the Karpathy Loop Phase 1 plan. It enforces a strict
promotion pipeline for autonomous-learning candidates:

    sandbox -> {canary, rolled_back}
    canary  -> {active, rolled_back}
    active  -> {rolled_back}
    rolled_back -> {} (terminal)

A candidate can never skip `canary` (no sandbox -> active jump) and every
non-terminal state must remain reachable to `rolled_back` so the 7-day review
can take a candidate back out.

The on-disk layout is intentionally simple: each state lives in its own
sub-directory under the root, and candidate artifacts are single `.md` files
keyed by their `record_id`. Every transition is appended to
`transitions.jsonl` for audit, mirroring the Karpathy Loop design.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


STATES = ["sandbox", "canary", "active", "rolled_back"]
TRANSITIONS = {
    "sandbox": {"canary", "rolled_back"},
    "canary": {"active", "rolled_back"},
    "active": {"rolled_back"},
    "rolled_back": set(),
}


class PromotionStateMachine:
    """Filesystem-backed candidate promotion state machine."""

    def __init__(self, root: Path) -> None:
        """Initialize root directory and ensure every state sub-dir exists."""
        self.root = Path(root)
        for s in STATES:
            (self.root / s).mkdir(parents=True, exist_ok=True)
        self.log = self.root / "transitions.jsonl"
        if not self.log.exists():
            self.log.touch()

    def _path(self, state: str, record_id: str) -> Path:
        return self.root / state / f"{record_id}.md"

    def current_state(self, record_id: str) -> str | None:
        """Return the state a record currently lives in, or None if not found."""
        for s in STATES:
            if self._path(s, record_id).exists():
                return s
        return None

    def create(self, record_id: str, filename: str, content: str) -> Path:
        """Drop a new candidate into `sandbox/`. Logs the create transition.

        Args:
            record_id: Canonical identifier; the on-disk file is named
                `{record_id}.md` so the state machine can later find it by
                record id.
            filename: Display-style file name (e.g. a category label such as
                `AUTONOMOUS_LEARNING_CANDIDATE.md`). It is embedded in the
                artifact as a leading header line and is *not* used as the
                disk filename — the record id is authoritative.
            content: Markdown body of the candidate.
        """
        path = self.root / "sandbox" / f"{record_id}.md"
        body = content if content.lstrip().startswith("#") else f"# {filename}\n\n{content}"
        path.write_text(body, encoding="utf-8")
        self._log(record_id, None, "sandbox")
        return path

    def promote(self, record_id: str, target: str, **kwargs: object) -> None:
        """Move a candidate to `target` if the transition is legal.

        Args:
            record_id: The candidate identifier (its file basename in the
                current state dir is `{record_id}.md`).
            target: Destination state (one of `STATES`).
            **kwargs: Guard flags. `blast_radius_ok=True` is required when
                promoting to `canary`; `metrics_ok=True` is required when
                promoting to `active`.

        Raises:
            ValueError: If the record is unknown, the transition is not
                permitted, or the required guard flag is missing.
        """
        current = self.current_state(record_id)
        if current is None:
            raise ValueError(f"{record_id} not in any state")
        if target not in TRANSITIONS[current]:
            raise ValueError(f"invalid transition {current} -> {target}")
        if target == "canary" and not kwargs.get("blast_radius_ok"):
            raise ValueError("canary requires blast_radius_ok=True")
        if target == "active" and not kwargs.get("metrics_ok"):
            raise ValueError("active requires metrics_ok=True")
        src = self._path(current, record_id)
        if not src.exists():
            for f in (self.root / current).glob(f"{record_id}*"):
                src = f
                break
        dst = self._path(target, record_id)
        src.rename(dst)
        self._log(record_id, current, target)

    def _log(self, record_id: str, frm: str | None, to: str) -> None:
        with self.log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "record_id": record_id,
                "from": frm,
                "to": to,
                "at": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
