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
import re
from datetime import datetime, timezone
from pathlib import Path


STATES = ["sandbox", "canary", "active", "rolled_back"]
TRANSITIONS = {
    "sandbox": {"canary", "rolled_back"},
    "canary": {"active", "rolled_back"},
    "active": {"rolled_back"},
    "rolled_back": set(),
}

# Whitelist for safe record_id values: letters, digits, dot, underscore,
# colon, hyphen, length 1..200. Covers every existing id format observed in
# the codebase (``lme-case-0``, ``locomo-c0-q0``, ``auth-svc-2025-11-19``,
# ``rec_demo``) and nothing that could plausibly be a path separator.
RECORD_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
# Belt-and-suspenders forbidden substrings. ``..`` catches parent-dir
# escapes, ``/`` and ``\\`` catch explicit path separators, ``\x00`` blocks
# NUL injection. The whitelist above already rejects the single-char
# forms of ``/`` and ``\\`` via the character class; this list also covers
# multi-char cases like ``..foo`` or ``a/b``.
FORBIDDEN_FRAGMENTS = ("..", "/", "\\", "\x00")


def _validate_record_id(record_id: str) -> str:
    """Validate that ``record_id`` is safe to interpolate into a file path.

    Raises :class:`ValueError` for any of:

    * non-string input
    * empty string
    * substring from :data:`FORBIDDEN_FRAGMENTS` (``..``, ``/``, ``\\``,
      NUL)
    * characters outside the :data:`RECORD_ID_PATTERN` whitelist
    * resolved path escaping the caller's ``root`` directory (the
      ``startswith`` check that used to be the only defence; we now use
      ``Path.resolve()`` + ``is_relative_to`` for correctness on
      case-insensitive filesystems and drive-relative paths on Windows)

    Returns the original ``record_id`` on success for call-site chaining.
    """
    if not isinstance(record_id, str) or not record_id:
        raise ValueError(f"invalid record_id: {record_id!r}")
    if any(fragment in record_id for fragment in FORBIDDEN_FRAGMENTS):
        raise ValueError(f"invalid record_id: {record_id!r}")
    if not RECORD_ID_PATTERN.fullmatch(record_id):
        raise ValueError(f"invalid record_id: {record_id!r}")
    return record_id


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
        """Build the on-disk path for ``record_id`` in ``state``.

        Validates ``record_id`` against the whitelist and against the
        post-``resolve()`` containment check before returning, so a
        caller that builds a path and reads or writes to it is guaranteed
        to stay inside ``self.root``.
        """
        _validate_record_id(record_id)
        candidate = self.root / state / f"{record_id}.md"
        # Resolve and verify containment. ``resolve()`` is required to
        # normalise symlinks, ``..`` segments, and Windows drive-relative
        # paths (``D:foo``) that could otherwise read or write outside
        # the state root.
        resolved_root = self.root.resolve()
        resolved_candidate = candidate.resolve()
        try:
            resolved_candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(
                f"invalid record_id: {record_id!r} (resolves outside {resolved_root})"
            ) from exc
        return candidate

    def current_state(self, record_id: str) -> str | None:
        """Return the state a record currently lives in, or None if not found."""
        _validate_record_id(record_id)
        for s in STATES:
            if self._path(s, record_id).exists():
                return s
        return None

    def create(self, record_id: str, filename: str, content: str) -> Path:
        """Drop a new candidate into `sandbox/`. Logs the create transition.

        Args:
            record_id: Canonical identifier; the on-disk file is named
                `{record_id}.md` so the state machine can later find it by
                record id. Must match :data:`RECORD_ID_PATTERN` and
                contain no :data:`FORBIDDEN_FRAGMENTS`.
            filename: Display-style file name (e.g. a category label such as
                `AUTONOMOUS_LEARNING_CANDIDATE.md`). It is embedded in the
                artifact as a leading header line and is *not* used as the
                disk filename — the record id is authoritative.
            content: Markdown body of the candidate.

        Raises:
            ValueError: If ``record_id`` is unsafe.
        """
        path = self._path("sandbox", record_id)
        body = content if content.lstrip().startswith("#") else f"# {filename}\n\n{content}"
        path.write_text(body, encoding="utf-8")
        self._log(record_id, None, "sandbox")
        return path

    def promote(self, record_id: str, target: str, **kwargs: object) -> None:
        """Move a candidate to `target` if the transition is legal.

        Args:
            record_id: The candidate identifier (its file basename in the
                current state dir is `{record_id}.md`). Must be a safe
                record id (see :data:`RECORD_ID_PATTERN`).
            target: Destination state (one of `STATES`).
            **kwargs: Guard flags. `blast_radius_ok=True` is required when
                promoting to `canary`; `metrics_ok=True` is required when
                promoting to `active`.

        Raises:
            ValueError: If the record id is unsafe, the record is
                unknown, the transition is not permitted, or the required
                guard flag is missing.
        """
        _validate_record_id(record_id)
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
