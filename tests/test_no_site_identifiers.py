"""Portability guard: no new site-specific identifiers in shipped, non-test code.

Hostnames, tailnet IPs, usernames, home paths and agent/profile names belong in
configuration (env, ``settings.json``) or runtime discovery. The baseline below
records legacy compatibility occurrences that are env-overridable; it may only
shrink. Adding a new occurrence, or a new file containing one, fails CI.
See docs/deployment/agent-discovery.md ("Portability").
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).with_name("site_identifier_baseline.json")
SCAN_DIRS = ("eimemory", "deploy", "integrations", "scripts", ".github", "examples")
SUFFIXES = {".py", ".sh", ".js", ".ts", ".json", ".yml", ".yaml", ".toml", ".service", ".timer", ".conf", ".md", ""}
SKIP_SUFFIXES = {".patch"}
PATTERN = re.compile(
    r"(?i)\b(?:honrui|honxin|hongrui|hongxin|hongtai|xiaomage|hongtu|darrow)\b"
    r"|\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b(?!/)"
    r"|/dev-project\b"
    r"|/home/(?!<)[a-z_][a-z0-9_-]*"
)


def scan() -> dict[str, int]:
    counts: dict[str, int] = {}
    for top in SCAN_DIRS:
        base = ROOT / top
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix in SKIP_SUFFIXES or path.suffix not in SUFFIXES:
                continue
            if any(part in {"node_modules", "__pycache__"} for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            hits = len(PATTERN.findall(text))
            if hits:
                counts[path.relative_to(ROOT).as_posix()] = hits
    return counts


def test_no_new_site_specific_identifiers() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = scan()
    grown = {f: (baseline.get(f, 0), n) for f, n in current.items() if n > baseline.get(f, 0)}
    assert not grown, (
        "new hard-coded site-specific identifiers (host/IP/user/home path/agent name); "
        f"move them to config or discovery: {grown}"
    )


def test_pattern_catches_examples() -> None:
    for sample in ("ssh honrui", "100.71.12.69", "/home/alice/x", "/dev-project/x", "agent hongxin"):
        assert PATTERN.search(sample), sample
    for sample in ("/home/<user>/x", "127.0.0.1:8091", "<agent-id>"):
        assert not PATTERN.search(sample), sample
