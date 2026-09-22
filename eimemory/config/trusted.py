"""Deployment trust anchors resolved from env/settings — never author laptop paths."""

from __future__ import annotations

import os
from pathlib import Path


class TrustedDeploymentError(RuntimeError):
    """Raised when a required trust anchor is unset or invalid."""


_ROOT_ENV = "EIMEMORY_TRUSTED_REPOSITORY_ROOT"
_REMOTE_ENV = "EIMEMORY_TRUSTED_REMOTE"
_BRANCH_ENV = "EIMEMORY_TRUSTED_BRANCH"
# Alternate names used by install/governance docs
_ROOT_ALT_ENV = "EIMEMORY_DEPLOYMENT_REPO_ROOT"
_BRANCH_ALT_ENV = "EIMEMORY_DEPLOYMENT_BRANCH"


def trusted_repository_root() -> Path:
    """Return the configured trusted repository root.

    Fail closed if unset — never fall back to an author development path.
    """
    raw = (
        os.environ.get(_ROOT_ENV, "").strip()
        or os.environ.get(_ROOT_ALT_ENV, "").strip()
    )
    if not raw:
        try:
            from eimemory.config.loader import load_settings

            settings = load_settings()
            configured = getattr(settings, "trusted_repository_root", "") or ""
            raw = str(configured).strip()
        except Exception:
            raw = ""
    if not raw:
        raise TrustedDeploymentError(
            f"trusted_repository_root_unset: set {_ROOT_ENV} (or {_ROOT_ALT_ENV}) "
            "to the deployment repository path; author laptop paths are not defaults"
        )
    return Path(raw).expanduser().resolve()


def trusted_remote() -> str:
    raw = os.environ.get(_REMOTE_ENV, "").strip()
    if raw:
        return raw
    try:
        from eimemory.config.loader import load_settings

        settings = load_settings()
        configured = getattr(settings, "trusted_remote", "") or ""
        if str(configured).strip():
            return str(configured).strip()
    except Exception:
        pass
    return "origin"


def trusted_branch() -> str:
    """Configured default branch (may be master or main)."""
    raw = (
        os.environ.get(_BRANCH_ENV, "").strip()
        or os.environ.get(_BRANCH_ALT_ENV, "").strip()
    )
    if raw:
        return raw
    try:
        from eimemory.config.loader import load_settings

        settings = load_settings()
        configured = getattr(settings, "trusted_branch", "") or ""
        if str(configured).strip():
            return str(configured).strip()
    except Exception:
        pass
    return "master"


def trusted_branch_allowed(ref: str) -> bool:
    """Accept the configured branch; also accept main↔master synonym."""
    branch = str(ref or "").removeprefix("refs/heads/").strip()
    configured = trusted_branch()
    allowed = {configured}
    if configured == "master":
        allowed.add("main")
    elif configured == "main":
        allowed.add("master")
    return branch in allowed


def trusted_remote_matches(remote: str) -> bool:
    return str(remote or "").strip() == trusted_remote()
