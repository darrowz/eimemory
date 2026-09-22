"""Fail when bare store._lock / sqlite.conn access appears outside storage."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "eimemory"

# Only storage/ may touch the private lock/connection. Everyone else uses RuntimeStore facades.
ALLOW_PREFIXES = (
    "storage/",
)

PATTERNS = (
    re.compile(r"store\._lock"),
    re.compile(r"\.sqlite\.conn\.(?:execute|executemany|commit|rollback)\b"),
    re.compile(r"(?<![\w.])sqlite\.conn\.(?:execute|executemany|commit|rollback)\b"),
    re.compile(r"\.sqlite\.conn\b(?!\.(?:in_transaction|row_factory|total_changes))"),
)


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _allowed(rel: str) -> bool:
    return any(rel == prefix or rel.startswith(prefix) for prefix in ALLOW_PREFIXES)


def test_no_bare_store_conn_outside_storage() -> None:
    offenders: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = _rel(path)
        if _allowed(rel):
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for pattern in PATTERNS:
                if pattern.search(line):
                    offenders.append(f"{rel}:{lineno}:{stripped}")
                    break
    assert offenders == [], "bare store._lock / sqlite.conn outside storage:\n" + "\n".join(offenders)
