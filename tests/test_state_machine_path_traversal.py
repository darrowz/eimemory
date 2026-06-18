"""Regression tests for the record_id path-traversal defence in
``eimemory.governance.state_machine``.

Covers Bug B from
``2026-06-18-eimemory-six-bug-fix-batch.md`` (R9): the on-disk path for
candidate records was built as ``self.root / "sandbox" /
f"{record_id}.md"`` with no validation of ``record_id``. A caller could
pass ``../../etc/passwd`` (or any path with ``/``, ``\\``, ``..``, or
NUL) and the state machine would happily read or write outside its
declared root. The fix adds:

* a whitelist regex ``^[A-Za-z0-9._:-]{1,200}$`` covering every
  existing record_id format observed in the codebase;
* a substring check for ``..``, ``/``, ``\\`` and ``\\x00``;
* a belt-and-suspenders ``resolve()`` containment check that catches
  edge cases the regex misses (e.g. Windows drive-relative paths like
  ``D:foo``).

The tests below exercise both the regex/fragment rejection and the
``resolve()`` containment path so the contract is enforced from every
entry point.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from eimemory.governance import state_machine
from eimemory.governance.state_machine import (
    FORBIDDEN_FRAGMENTS,
    RECORD_ID_PATTERN,
    PromotionStateMachine,
    _validate_record_id,
)


# ---------------------------------------------------------------------------
# 1. ``..`` parent-dir escape must be rejected at the create entry point.
# ---------------------------------------------------------------------------


def test_create_with_dotdot_raises(tmp_path: Path) -> None:
    sm = PromotionStateMachine(root=tmp_path)
    with pytest.raises(ValueError):
        sm.create("../../etc/passwd", "AUTONOMOUS_LEARNING_CANDIDATE.md", "# x\n")
    # Nothing should have been written outside the root.
    assert not (tmp_path.parent / "etc" / "passwd.md").exists(), (
        f"path-traversal write happened: {(tmp_path.parent / 'etc' / 'passwd.md')!r}"
    )


# ---------------------------------------------------------------------------
# 2. Forward-slash injection must be rejected.
# ---------------------------------------------------------------------------


def test_create_with_forward_slash_raises(tmp_path: Path) -> None:
    sm = PromotionStateMachine(root=tmp_path)
    with pytest.raises(ValueError):
        sm.create("foo/bar", "AUTONOMOUS_LEARNING_CANDIDATE.md", "# x\n")
    # No nested sandbox/ subtree should have been created from the
    # would-be traversal target.
    assert not (tmp_path / "sandbox" / "foo").exists()


# ---------------------------------------------------------------------------
# 3. Backslash injection must be rejected (Windows path separator).
# ---------------------------------------------------------------------------


def test_create_with_backslash_raises(tmp_path: Path) -> None:
    sm = PromotionStateMachine(root=tmp_path)
    with pytest.raises(ValueError):
        sm.create("foo\\bar", "AUTONOMOUS_LEARNING_CANDIDATE.md", "# x\n")
    # No sub-folder should have been created.
    assert not (tmp_path / "sandbox" / "foo").exists()


# ---------------------------------------------------------------------------
# 4. NUL-byte injection must be rejected.
# ---------------------------------------------------------------------------


def test_create_with_nul_raises(tmp_path: Path) -> None:
    sm = PromotionStateMachine(root=tmp_path)
    with pytest.raises(ValueError):
        sm.create("foo\x00bar", "AUTONOMOUS_LEARNING_CANDIDATE.md", "# x\n")


# ---------------------------------------------------------------------------
# 5. Legitimate IDs (matching every existing format) must succeed and
#    the on-disk file must appear in ``sandbox/``.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record_id",
    [
        "lme-case-12345",
        "rec_demo",
        "rec_x",
        "locomo-c0-q0",
        "auth-svc-2025-11-19",
        "candidate-001",
        "capability.v1",
        "agent:hongtu",
        "experiment_42",
        "a",  # single-char lower bound
    ],
)
def test_create_with_legitimate_id_succeeds(tmp_path: Path, record_id: str) -> None:
    sm = PromotionStateMachine(root=tmp_path)
    path = sm.create(
        record_id, "AUTONOMOUS_LEARNING_CANDIDATE.md", "# demo\n"
    )
    assert path.exists()
    assert path.parent.name == "sandbox"
    assert sm.current_state(record_id) == "sandbox"
    # The file must be inside the declared root (resolve containment).
    assert path.resolve().is_relative_to(tmp_path.resolve())


def test_create_with_200_char_id_at_regex_upper_bound_succeeds(tmp_path: Path) -> None:
    """The 200-char whitelist upper bound must accept inputs of that
    length. We don't round-trip the file to disk here because some OS
    path-length caps (Windows = 260 chars by default) would reject a
    203-char filename nested under a long tmp prefix; what we're
    asserting is the regex + fragment validator, not the OS layer.
    """
    long_id = "a" * 200
    # The validator must accept it.
    assert _validate_record_id(long_id) == long_id
    # And the construction of the on-disk path (when the OS can fit it)
    # must not raise for an OS-legal path. We craft a tmp dir that is
    # short enough to leave headroom.
    short_root = Path(tempfile.mkdtemp(prefix="eism_"))
    try:
        sm = PromotionStateMachine(root=short_root)
        # The path needs to be ``<root>/sandbox/<200 chars>.md`` =
        # len(short_root) + 9 + 200. If that's still under the OS limit
        # we exercise the full create; otherwise we settle for the
        # validator assertion (which is what we actually care about).
        full_path = short_root / "sandbox" / f"{long_id}.md"
        if len(str(full_path.resolve())) < 250:
            sm.create(long_id, "AUTONOMOUS_LEARNING_CANDIDATE.md", "# demo\n")
            assert sm.current_state(long_id) == "sandbox"
    finally:
        import shutil

        shutil.rmtree(short_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. ``promote()`` must also validate record_id at its entry point.
# ---------------------------------------------------------------------------


def test_promote_with_invalid_id_raises(tmp_path: Path) -> None:
    sm = PromotionStateMachine(root=tmp_path)
    # Seed a legitimate record so a *valid* promote would otherwise work.
    sm.create("legit", "AUTONOMOUS_LEARNING_CANDIDATE.md", "# x\n")
    # The malicious id must be rejected even though we pass the
    # blast_radius_ok guard flag the existing tests rely on.
    with pytest.raises(ValueError):
        sm.promote("..", "canary", blast_radius_ok=True)
    with pytest.raises(ValueError):
        sm.promote("../escape", "canary", blast_radius_ok=True)
    with pytest.raises(ValueError):
        sm.promote("foo/bar", "canary", blast_radius_ok=True)
    with pytest.raises(ValueError):
        sm.promote("foo\\bar", "canary", blast_radius_ok=True)
    with pytest.raises(ValueError):
        sm.promote("foo\x00bar", "canary", blast_radius_ok=True)
    with pytest.raises(ValueError):
        sm.promote("", "canary", blast_radius_ok=True)
    # And the legit record was not promoted to canary (the failed calls
    # must not have side effects on legitimate state).
    assert sm.current_state("legit") == "sandbox"


# ---------------------------------------------------------------------------
# 7. ``resolve()`` containment: any record_id that would resolve outside
#    the state root must be rejected, even if it passes the regex /
#    fragment checks. We patch ``Path.resolve`` to simulate a malicious
#    value, which is the only cross-platform way to exercise the
#    post-resolve branch (Windows drive-relative ``D:foo`` is the
#    realistic trigger; on POSIX we force the same branch via mock).
# ---------------------------------------------------------------------------


def test_resolve_escape_attempt_raises(tmp_path: Path) -> None:
    sm = PromotionStateMachine(root=tmp_path)

    # Build a fake "escaped" path that is *outside* the resolved root,
    # but whose textual path is constructed through the real
    # ``_path()`` code path so the validator sees a real id.
    escaped = (tmp_path.resolve().parent / "evil.md").resolve()

    # Force ``Path.resolve()`` to return our escaped path during the
    # containment check. We only need the post-resolve branch; the
    # pre-resolve validator runs first with the real id, so we use an
    # id that the regex would happily accept (e.g. ``D:foo`` on
    # Windows) and let the resolve check catch it.
    real_path_resolve = Path.resolve

    def fake_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        # Anything inside ``sandbox/`` and named like ``*foo*`` is
        # reported as outside the root.
        if self.name == "D:foo.md":
            return escaped
        return real_path_resolve(self, *args, **kwargs)

    with patch.object(Path, "resolve", fake_resolve):
        with pytest.raises(ValueError):
            sm._path("sandbox", "D:foo")


# ---------------------------------------------------------------------------
# 8. The public validator is a thin, pure function — it should reject
#    every input the task lists (and a few extra edge cases) and accept
#    every format currently in use. Pin the contract here so any future
#    loosening shows up as a test diff.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "../escape",
        "..\\escape",
        "/absolute",
        "nested/name",
        "a" * 201,  # over the 200-char limit
        "foo\x00bar",
        " ",  # whitespace
        "foo bar",  # space inside
        "foo$bar",  # shell-meta
        None,  # type: ignore[list-item]
        123,  # type: ignore[list-item]
        [],  # type: ignore[list-item]
    ],
)
def test_validate_record_id_rejects_unsafe_inputs(bad_id: object) -> None:
    with pytest.raises(ValueError):
        _validate_record_id(bad_id)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "good_id",
    [
        "exp-123",
        "capability.v1",
        "agent:hongtu",
        "rec_demo",
        "rec_x",
        "a",
        "a" * 200,
        "x" * 1,  # shortest legal
    ],
)
def test_validate_record_id_accepts_safe_inputs(good_id: str) -> None:
    assert _validate_record_id(good_id) == good_id


# ---------------------------------------------------------------------------
# 9. The module-level constants are exposed and frozen: any change is
#    a security regression, so guard them.
# ---------------------------------------------------------------------------


def test_module_constants_are_pinned() -> None:
    assert RECORD_ID_PATTERN.pattern == r"^[A-Za-z0-9._:-]{1,200}$"
    assert FORBIDDEN_FRAGMENTS == ("..", "/", "\\", "\x00")
    # The fragments tuple is documented as a security boundary — if a
    # future refactor accidentally swaps it for a list, the rejection
    # behaviour would silently change. Pin the type.
    assert isinstance(FORBIDDEN_FRAGMENTS, tuple)


# ---------------------------------------------------------------------------
# 10. ``current_state`` must also validate record_id (otherwise the
#     attacker could probe the filesystem via the read-only path).
# ---------------------------------------------------------------------------


def test_current_state_with_invalid_id_raises(tmp_path: Path) -> None:
    sm = PromotionStateMachine(root=tmp_path)
    with pytest.raises(ValueError):
        sm.current_state("../etc/passwd")
    with pytest.raises(ValueError):
        sm.current_state("")


# ---------------------------------------------------------------------------
# 11. Smoke test: the full sandbox -> canary -> active progression still
#     works for a legitimate id (regression guard for the existing
#     happy path that the validator changes touch).
# ---------------------------------------------------------------------------


def test_full_progression_still_works_for_legitimate_id(tmp_path: Path) -> None:
    sm = PromotionStateMachine(root=tmp_path)
    sm.create("rec_smoke", "AUTONOMOUS_LEARNING_CANDIDATE.md", "# demo\n")
    assert sm.current_state("rec_smoke") == "sandbox"
    sm.promote("rec_smoke", "canary", blast_radius_ok=True)
    assert sm.current_state("rec_smoke") == "canary"
    sm.promote("rec_smoke", "active", metrics_ok=True)
    assert sm.current_state("rec_smoke") == "active"
    assert (tmp_path / "active" / "rec_smoke.md").exists()


# ---------------------------------------------------------------------------
# 12. Audit log: a failed create must NOT pollute transitions.jsonl with
#     an entry (the audit log is part of the security story too).
# ---------------------------------------------------------------------------


def test_failed_create_does_not_pollute_audit_log(tmp_path: Path) -> None:
    sm = PromotionStateMachine(root=tmp_path)
    with pytest.raises(ValueError):
        sm.create("../../etc/passwd", "AUTONOMOUS_LEARNING_CANDIDATE.md", "# x\n")
    # Audit log must be empty (only the touch() in __init__).
    log = tmp_path / "transitions.jsonl"
    content = log.read_text(encoding="utf-8")
    assert content == "", f"audit log was polluted: {content!r}"


if __name__ == "__main__":
    unittest.main(module=__name__)
