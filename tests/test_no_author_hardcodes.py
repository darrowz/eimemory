"""Fail closed: author laptop / private-network literals must not re-enter source."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

BANNED_LITERALS = (
    "100.105.189.120",
    "/home/darrow",
    "FEISHU_DARROW_OPEN_ID",
)

# Author trusted-root default is banned except allowlisted historical docs/fixtures.
BANNED_TRUSTED_DEFAULT = "/dev-project/eimemory"

ALLOWLIST_SUFFIXES = {
    # Historical audit / remediation narrative only.
    "docs/audit/",
    # Explicit test fixtures that configure trust via env (not source defaults).
    "tests/test_no_author_hardcodes.py",
}

SCAN_ROOTS = (
    REPO_ROOT / "eimemory",
    REPO_ROOT / "deploy",
    REPO_ROOT / "integrations",
)

SCAN_SUFFIXES = {".py", ".sh", ".service", ".timer", ".path", ".conf", ".js", ".md", ".json"}


def _is_allowlisted(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT).as_posix()
    return any(rel.startswith(prefix) or rel == prefix.rstrip("/") for prefix in ALLOWLIST_SUFFIXES)


def _iter_source_files():
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in SCAN_SUFFIXES and path.name not in {
                "install_immutable_release.sh",
            }:
                # still include extensionless scripts under deploy/
                if not (root.name == "deploy" and path.suffix == ""):
                    if path.suffix.lower() not in SCAN_SUFFIXES:
                        continue
            if _is_allowlisted(path):
                continue
            yield path


@pytest.mark.parametrize("literal", BANNED_LITERALS)
def test_banned_author_literals_absent(literal: str) -> None:
    hits: list[str] = []
    for path in _iter_source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if literal in text:
            rel = path.relative_to(REPO_ROOT).as_posix()
            hits.append(rel)
    assert hits == [], f"banned literal {literal!r} found in: {hits}"


def test_trusted_repository_root_not_author_path_default() -> None:
    """TRUSTED default must not be the author laptop path in library source."""
    hits: list[str] = []
    for path in _iter_source_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if not rel.startswith("eimemory/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if BANNED_TRUSTED_DEFAULT not in text:
            continue
        # Allow mentions in comments about what NOT to do, and test env wiring docs.
        for i, line in enumerate(text.splitlines(), 1):
            if BANNED_TRUSTED_DEFAULT not in line:
                continue
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or "not" in stripped.lower() and "default" in stripped.lower():
                continue
            # Ban assignment-style defaults
            if "Path(" in line or "=" in line or "or " in line:
                hits.append(f"{rel}:{i}:{stripped[:120]}")
    assert hits == [], f"author trusted-root default leaked: {hits}"
