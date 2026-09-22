"""A1: shrink runtime: Any in hottest governance modules."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "eimemory" / "governance"

# Files in scope for this gate. Allowlist entries must shrink to empty over time.
SCOPED = (
    "promotion_manager.py",
    "promotion_watch.py",
    "capability_probe_executor.py",
)

# Explicit residual allowlist (line content snippets). Prefer empty.
ALLOW_SNIPPETS: dict[str, tuple[str, ...]] = {
    "promotion_manager.py": (),
    "promotion_watch.py": (),
    "capability_probe_executor.py": (),
}

PATTERN = re.compile(r"\bruntime:\s*Any\b|\b_runtime:\s*Any\b")


def test_hottest_governance_modules_have_no_runtime_any() -> None:
    offenders: list[str] = []
    for name in SCOPED:
        path = ROOT / name
        text = path.read_text(encoding="utf-8")
        allowed = ALLOW_SNIPPETS.get(name, ())
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not PATTERN.search(line):
                continue
            if any(snippet in line for snippet in allowed):
                continue
            offenders.append(f"{name}:{lineno}:{line.strip()}")
    assert offenders == [], "runtime: Any residuals:\n" + "\n".join(offenders)
