#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys


AFFECTED_VERSION = re.compile(r"^2026\.7\.1-beta\.[2-6]$")
RECOVERY_METHODS = ("message.action", "agent")
AGENT_TOOL_MARKERS = (
    "function createSessionsHistoryTool",
    "function createSessionsListTool",
    "function createSessionsSendTool",
)
AGENT_TOOL_GATEWAY_DEFAULT = "const gatewayCall = opts?.callGateway ?? callGateway;"
AGENT_TOOL_GATEWAY_CLI = (
    'const gatewayCall = opts?.callGateway ?? ((request) => callGateway({ '
    '...request, clientName: "cli", mode: "cli" }));'
)


class PatchError(RuntimeError):
    pass


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.eimemory-{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _patch_runtime(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    changed = False
    for method in RECOVERY_METHODS:
        escaped = re.escape(method)
        patched = re.compile(
            rf'await callGateway\(\{{\s*clientName: "cli",\s*mode: "cli",\s*method: "{escaped}",'
        )
        if len(patched.findall(text)) == 1:
            continue
        original = re.compile(
            rf'(?P<prefix>await callGateway\(\{{\r?\n)(?P<indent>[ \t]+)method: "{escaped}",'
        )
        matches = list(original.finditer(text))
        if len(matches) != 1:
            raise PatchError(f"expected one unpatched {method} recovery call in {path.name}")

        def replace(match: re.Match[str]) -> str:
            indent = match.group("indent")
            return (
                f'{match.group("prefix")}{indent}clientName: "cli",{newline}'
                f'{indent}mode: "cli",{newline}'
                f'{indent}method: "{method}",'
            )

        text = original.sub(replace, text, count=1)
        changed = True
    if changed:
        _atomic_write(path, text)
    return changed


def _patch_agent_tools(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    original_count = text.count(AGENT_TOOL_GATEWAY_DEFAULT)
    patched_count = text.count(AGENT_TOOL_GATEWAY_CLI)
    expected_count = len(AGENT_TOOL_MARKERS)
    if original_count == 0 and patched_count == expected_count:
        return False
    if original_count != expected_count or patched_count != 0:
        raise PatchError(
            f"expected {expected_count} unpatched agent tool gateway defaults in {path.name}"
        )
    _atomic_write(path, text.replace(AGENT_TOOL_GATEWAY_DEFAULT, AGENT_TOOL_GATEWAY_CLI))
    return True


def patch_openclaw(openclaw_root: Path) -> dict[str, str]:
    if openclaw_root.is_symlink():
        raise PatchError("OpenClaw root must not be a symlink")
    root = openclaw_root.resolve(strict=True)
    package_path = root / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    version = str(package.get("version") or "")
    if not AFFECTED_VERSION.fullmatch(version):
        return {"status": "not_affected", "version": version}

    dist = root / "dist"
    if dist.is_symlink():
        raise PatchError("OpenClaw dist must not be a symlink")
    dist = dist.resolve(strict=True)
    candidates: list[Path] = []
    for candidate in sorted(dist.glob("main-session-restart-recovery-*.js")):
        if candidate.is_symlink() or candidate.resolve(strict=True).parent != dist:
            raise PatchError(f"unsafe recovery module path: {candidate.name}")
        if "async function resumeMainSession" in candidate.read_text(encoding="utf-8"):
            candidates.append(candidate)
    if len(candidates) != 1:
        raise PatchError(f"expected one recovery implementation, found {len(candidates)}")

    agent_tool_candidates: list[Path] = []
    for candidate in sorted(dist.glob("openclaw-tools-*.js")):
        if candidate.is_symlink() or candidate.resolve(strict=True).parent != dist:
            raise PatchError(f"unsafe agent tools module path: {candidate.name}")
        candidate_text = candidate.read_text(encoding="utf-8")
        if all(marker in candidate_text for marker in AGENT_TOOL_MARKERS):
            agent_tool_candidates.append(candidate)
    if len(agent_tool_candidates) != 1:
        raise PatchError(f"expected one agent tools implementation, found {len(agent_tool_candidates)}")

    changed = _patch_runtime(candidates[0])
    agent_tools_changed = _patch_agent_tools(agent_tool_candidates[0])
    return {
        "status": "patched" if changed or agent_tools_changed else "already_patched",
        "version": version,
        "module": candidates[0].name,
        "agent_tools_module": agent_tool_candidates[0].name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Patch affected OpenClaw internal gateway scope handling.")
    parser.add_argument("--openclaw-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = patch_openclaw(args.openclaw_root)
    except (OSError, ValueError, PatchError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
