"""Directory-level and invariant-level path authorization for code evolution.

Single source of truth for whether a relative repo path may be patched by an
autonomous code-evolution transaction. Exact pins, directory globs, hard
denies, and deny-self (evolution-plane) rules compose here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath

# Default allow radius when a v2 policy opts into directory mode without
# supplying its own globs. tests/** and deploy/** stay closed except for the
# explicit exact legacy pins below.
DEFAULT_ALLOWED_PATH_GLOBS: tuple[str, ...] = (
    "eimemory/governance/**",
    "eimemory/ops/**",
)

# Legacy exact pins outside the default directory trees (runtime-identity plan).
DEFAULT_EXACT_ALLOW_FILES: tuple[str, ...] = (
    "deploy/runtime_identity_policy.py",
)

# Hard denials. Exact pins may bypass these directory denials after deny-self.
DEFAULT_DENIED_PATH_GLOBS: tuple[str, ...] = (
    "deploy/**",
    "integrations/**",
    ".github/**",
    "**/secrets/**",
    "**/*secret*",
    "**/*.pem",
    "**/*.key",
)

# Evolution authority plane — never patchable, even under governance/**.
DENY_SELF_PATH_GLOBS: tuple[str, ...] = (
    "eimemory/governance/code_evolution*",
    "eimemory/governance/code_automation_policy*",
    "eimemory/governance/promotion_manager.py",
    "eimemory/governance/promotion_code_apply.py",
    "eimemory/governance/promotion_watch.py",
    "eimemory/governance/isolated_evaluator.py",
    "eimemory/governance/autonomous_learning.py",
    "eimemory/governance/autonomous_evolution.py",
    "eimemory/governance/capability_acceptance.py",
    "eimemory/governance/capability_replay_executor.py",
    "eimemory/governance/capability_replay_packs.py",
    "eimemory/governance/l5_readiness.py",
    "eimemory/storage/code_evolution_store.py",
    "eimemory/adapters/hermes/code_implementation.py",
)

# Concrete catalog retained for tests / documentation (subset of DENY_SELF globs).
DENY_SELF_PATHS: frozenset[str] = frozenset(
    {
        "eimemory/governance/code_evolution.py",
        "eimemory/governance/code_evolution_bridge.py",
        "eimemory/governance/code_evolution_effects.py",
        "eimemory/governance/code_evolution_observation.py",
        "eimemory/governance/code_evolution_path_policy.py",
        "eimemory/governance/code_evolution_repository.py",
        "eimemory/governance/code_evolution_semantic_validation.py",
        "eimemory/governance/code_evolution_test_plans.py",
        "eimemory/governance/code_evolution_transaction.py",
        "eimemory/governance/code_automation_policy.py",
        "eimemory/governance/code_automation_policy_issue.py",
        "eimemory/governance/promotion_manager.py",
        "eimemory/governance/promotion_code_apply.py",
        "eimemory/governance/promotion_watch.py",
        "eimemory/governance/isolated_evaluator.py",
        "eimemory/governance/autonomous_learning.py",
        "eimemory/governance/autonomous_evolution.py",
        "eimemory/governance/capability_acceptance.py",
        "eimemory/governance/capability_replay_executor.py",
        "eimemory/governance/capability_replay_packs.py",
        "eimemory/governance/l5_readiness.py",
        "eimemory/storage/code_evolution_store.py",
        "eimemory/adapters/hermes/code_implementation.py",
    }
)


def normalize_repo_path(path: str) -> str:
    """Return a posix relative path or empty string when the path is illegal."""

    original = str(path or "")
    if "\\" in original:
        return ""
    text = original.strip()
    if not text or text.startswith("/"):
        return ""
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        return ""
    if any(part == "" for part in pure.parts):
        return ""
    return pure.as_posix()


def validate_path_glob(pattern: str) -> str:
    """Validate one allow/deny glob. Returns an error code or empty string."""

    raw = str(pattern or "")
    if not raw or "\\" in raw or raw.startswith("/"):
        return "path_glob_invalid"
    for part in raw.split("/"):
        if part == "":
            return "path_glob_invalid"
        if part == "**":
            continue
        if "**" in part:
            return "path_glob_invalid"
        if ".." == part or part == ".":
            return "path_glob_invalid"
        cleaned = part.replace("*", "")
        if any(ch in cleaned for ch in "[]?{}"):
            return "path_glob_invalid"
    return ""


def match_path_glob(path: str, pattern: str) -> bool:
    """Match a normalized relative path against a ** / * glob."""

    path_parts = PurePosixPath(path).parts
    pattern_parts = tuple(str(pattern).split("/"))

    def walk(pi: int, gi: int) -> bool:
        if gi == len(pattern_parts):
            return pi == len(path_parts)
        token = pattern_parts[gi]
        if token == "**":
            if gi == len(pattern_parts) - 1:
                return True
            for consumed in range(pi, len(path_parts) + 1):
                if walk(consumed, gi + 1):
                    return True
            return False
        if pi >= len(path_parts):
            return False
        if _segment_match(path_parts[pi], token):
            return walk(pi + 1, gi + 1)
        return False

    return walk(0, 0)


def _segment_match(segment: str, token: str) -> bool:
    if token == "*":
        return True
    if "*" not in token:
        return segment == token
    parts = token.split("*")
    if not segment.startswith(parts[0]):
        return False
    cursor = len(parts[0])
    for index, piece in enumerate(parts[1:], start=1):
        if piece == "":
            if index == len(parts) - 1:
                return True
            continue
        found = segment.find(piece, cursor)
        if found < 0:
            return False
        cursor = found + len(piece)
    return True


def matches_any_glob(path: str, patterns: Iterable[str]) -> bool:
    return any(match_path_glob(path, pattern) for pattern in patterns)


def path_denied_by_self(path: str) -> bool:
    normalized = normalize_repo_path(path)
    if not normalized:
        return True
    if normalized in DENY_SELF_PATHS:
        return True
    return matches_any_glob(normalized, DENY_SELF_PATH_GLOBS)


def path_allowed_for_evolution(
    path: str,
    *,
    allowed_path_globs: Sequence[str] = (),
    denied_path_globs: Sequence[str] | None = None,
    exact_allow_files: Sequence[str] = (),
    deny_self_paths: Sequence[str] | None = None,
) -> tuple[bool, str]:
    """Authorize one relative path for code-evolution patching.

    Order:
      1. normalize / reject absolute, ``..``, empty, backslash
      2. deny-self (evolution plane)
      3. exact allow pins (bypass hard directory denials only)
      4. hard denied globs
      5. allowed globs
    """

    normalized = normalize_repo_path(path)
    if not normalized:
        return False, "path_invalid"
    if deny_self_paths is not None:
        deny_set = {normalize_repo_path(item) for item in deny_self_paths}
        deny_set.discard("")
        if normalized in deny_set or matches_any_glob(normalized, DENY_SELF_PATH_GLOBS):
            return False, "deny_self"
    elif path_denied_by_self(normalized):
        return False, "deny_self"

    exact = {normalize_repo_path(item) for item in exact_allow_files}
    exact.discard("")
    if normalized in exact:
        return True, ""

    denied = (
        tuple(denied_path_globs)
        if denied_path_globs is not None
        else DEFAULT_DENIED_PATH_GLOBS
    )
    if matches_any_glob(normalized, denied):
        return False, "path_denied"

    globs = tuple(allowed_path_globs or ())
    if globs and matches_any_glob(normalized, globs):
        return True, ""
    return False, "path_not_allowed"


def plan_allows_path(
    *,
    allowed_files: Sequence[str],
    allowed_path_globs: Sequence[str] = (),
    path: str,
) -> bool:
    """Whether a protected test plan authorizes ``path``."""

    ok, _reason = path_allowed_for_evolution(
        path,
        allowed_path_globs=allowed_path_globs,
        exact_allow_files=allowed_files,
        denied_path_globs=DEFAULT_DENIED_PATH_GLOBS,
    )
    return ok


__all__ = [
    "DEFAULT_ALLOWED_PATH_GLOBS",
    "DEFAULT_DENIED_PATH_GLOBS",
    "DEFAULT_EXACT_ALLOW_FILES",
    "DENY_SELF_PATH_GLOBS",
    "DENY_SELF_PATHS",
    "match_path_glob",
    "matches_any_glob",
    "normalize_repo_path",
    "path_allowed_for_evolution",
    "path_denied_by_self",
    "plan_allows_path",
    "validate_path_glob",
]
