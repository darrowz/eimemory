#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys


AFFECTED_VERSION = re.compile(r"^2026\.7\.1-(?:beta\.[2-6]|2)$")
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
AGENT_TOOLS_DEPS_DEFAULT = "let openClawToolsDeps = { callGateway };"
AGENT_TOOLS_DEPS_CLI = (
    'const callGatewayAsCli = (request) => callGateway({ ...request, clientName: "cli", mode: "cli" });\n'
    "let openClawToolsDeps = { callGateway: callGatewayAsCli };"
)
GATEWAY_TOOL_MARKERS = (
    "const AGENT_RUNTIME_IDENTITY_METHODS",
    "async function callGatewayTool(method, opts, params, extra)",
)
GATEWAY_TOOL_READ_IDENTITY_MARKER = "const useLocalOperatorReadIdentity ="
GATEWAY_TOOL_CLIENT_DEFAULT = (
    "clientName: GATEWAY_CLIENT_NAMES.GATEWAY_CLIENT,"
)
GATEWAY_TOOL_CLIENT_READ_ONLY = (
    "clientName: useLocalOperatorReadIdentity ? GATEWAY_CLIENT_NAMES.CLI : "
    "GATEWAY_CLIENT_NAMES.GATEWAY_CLIENT,"
)
GATEWAY_TOOL_MODE_DEFAULT = "mode: GATEWAY_CLIENT_MODES.BACKEND,"
GATEWAY_TOOL_MODE_READ_ONLY = (
    "mode: useLocalOperatorReadIdentity ? GATEWAY_CLIENT_MODES.CLI : "
    "GATEWAY_CLIENT_MODES.BACKEND,"
)
GATEWAY_TOOL_AGENT_TOKEN_DEFAULT = (
    "agentRuntimeIdentityToken ? { agentRuntimeIdentityToken } : {}"
)
GATEWAY_TOOL_AGENT_TOKEN_READ_ONLY = (
    "agentRuntimeIdentityToken && !useLocalOperatorReadIdentity "
    "? { agentRuntimeIdentityToken } : {}"
)
CALL_GATEWAY_MARKERS = (
    "async function callGateway(opts)",
    "return await callGatewayLeastPrivilege({",
)
CALL_GATEWAY_READ_SCOPE_MARKER = "const defaultReadScopes ="


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
    newline = "\r\n" if "\r\n" in text else "\n"
    original_count = text.count(AGENT_TOOL_GATEWAY_DEFAULT)
    patched_count = text.count(AGENT_TOOL_GATEWAY_CLI)
    expected_count = len(AGENT_TOOL_MARKERS)
    changed = False
    if original_count == expected_count and patched_count == 0:
        text = text.replace(AGENT_TOOL_GATEWAY_DEFAULT, AGENT_TOOL_GATEWAY_CLI)
        changed = True
    elif original_count != 0 or patched_count != expected_count:
        raise PatchError(
            f"expected {expected_count} consistent agent tool gateway defaults in {path.name}"
        )

    deps_default_count = text.count(AGENT_TOOLS_DEPS_DEFAULT)
    deps_patched_count = text.count(AGENT_TOOLS_DEPS_CLI.replace("\n", newline))
    if deps_default_count == 1 and deps_patched_count == 0:
        text = text.replace(AGENT_TOOLS_DEPS_DEFAULT, AGENT_TOOLS_DEPS_CLI.replace("\n", newline))
        changed = True
    elif deps_default_count != 0 or deps_patched_count != 1:
        raise PatchError(f"expected one consistent agent tools dependency boundary in {path.name}")

    if changed:
        _atomic_write(path, text)
    return changed


def _replace_gateway_tool_fragment(
    text: str,
    *,
    original: str,
    patched: str,
    path: Path,
) -> tuple[str, bool]:
    original_count = text.count(original)
    patched_count = text.count(patched)
    if original_count == 1 and patched_count == 0:
        return text.replace(original, patched, 1), True
    if original_count == 0 and patched_count == 1:
        return text, False
    raise PatchError(f"expected one consistent gateway tool fragment in {path.name}")


def _patch_gateway_tool(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    changed = False

    marker_count = text.count(GATEWAY_TOOL_READ_IDENTITY_MARKER)
    if marker_count == 0:
        anchor = re.compile(
            r"^(?P<indent>[ \t]+)const agentRuntimeIdentityToken =",
            re.MULTILINE,
        )
        matches = list(anchor.finditer(text))
        if len(matches) != 1:
            raise PatchError(
                f"expected one agent runtime identity anchor in {path.name}"
            )
        indent = matches[0].group("indent")
        read_identity = newline.join(
            (
                f"{indent}const useLocalOperatorReadIdentity =",
                f'{indent}    gateway.target === "local" &&',
                f"{indent}    trimToUndefined(opts.gatewayUrl) === void 0 &&",
                f"{indent}    trimToUndefined(opts.gatewayToken) === void 0 &&",
                f"{indent}    scopes.length > 0 &&",
                f'{indent}    scopes.every((scope) => scope === "operator.read");',
            )
        )
        anchor_start = matches[0].start()
        text = text[:anchor_start] + read_identity + newline + text[anchor_start:]
        changed = True
    elif marker_count != 1:
        raise PatchError(f"expected one gateway read identity marker in {path.name}")

    for original, patched in (
        (GATEWAY_TOOL_CLIENT_DEFAULT, GATEWAY_TOOL_CLIENT_READ_ONLY),
        (GATEWAY_TOOL_MODE_DEFAULT, GATEWAY_TOOL_MODE_READ_ONLY),
        (GATEWAY_TOOL_AGENT_TOKEN_DEFAULT, GATEWAY_TOOL_AGENT_TOKEN_READ_ONLY),
    ):
        text, fragment_changed = _replace_gateway_tool_fragment(
            text,
            original=original,
            patched=patched,
            path=path,
        )
        changed = changed or fragment_changed

    if changed:
        _atomic_write(path, text)
    return changed


def _patch_call_gateway(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    marker_count = text.count(CALL_GATEWAY_READ_SCOPE_MARKER)
    if marker_count == 1:
        return False
    if marker_count != 0:
        raise PatchError(f"expected one gateway read scope marker in {path.name}")

    anchor = re.compile(
        r"^(?P<indent>[ \t]+)return await callGatewayLeastPrivilege\(\{",
        re.MULTILINE,
    )
    matches = list(anchor.finditer(text))
    if len(matches) != 1:
        raise PatchError(f"expected one default gateway call anchor in {path.name}")
    indent = matches[0].group("indent")
    read_fallback = newline.join(
        (
            f"{indent}const defaultReadScopes =",
            f"{indent}    opts.mode === void 0 && opts.clientName === void 0",
            f"{indent}        ? resolveLeastPrivilegeOperatorScopesForMethod(opts.method, opts.params)",
            f"{indent}        : null;",
            f"{indent}if (",
            f"{indent}    defaultReadScopes?.length &&",
            f'{indent}    defaultReadScopes.every((scope) => scope === "operator.read")',
            f"{indent}) {{",
            f"{indent}    return await callGatewayCli({{ ...opts, scopes: defaultReadScopes }});",
            f"{indent}}}",
        )
    )
    anchor_start = matches[0].start()
    text = text[:anchor_start] + read_fallback + newline + text[anchor_start:]
    _atomic_write(path, text)
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

    gateway_tool_candidates: list[Path] = []
    for candidate in sorted(dist.glob("gateway-*.js")):
        if candidate.is_symlink() or candidate.resolve(strict=True).parent != dist:
            raise PatchError(f"unsafe gateway tool module path: {candidate.name}")
        candidate_text = candidate.read_text(encoding="utf-8")
        if all(marker in candidate_text for marker in GATEWAY_TOOL_MARKERS):
            gateway_tool_candidates.append(candidate)
    if len(gateway_tool_candidates) != 1:
        raise PatchError(
            f"expected one gateway tool implementation, found {len(gateway_tool_candidates)}"
        )

    call_gateway_candidates: list[Path] = []
    for candidate in sorted(dist.glob("call-*.js")):
        if candidate.is_symlink() or candidate.resolve(strict=True).parent != dist:
            raise PatchError(f"unsafe gateway call module path: {candidate.name}")
        candidate_text = candidate.read_text(encoding="utf-8")
        if all(marker in candidate_text for marker in CALL_GATEWAY_MARKERS):
            call_gateway_candidates.append(candidate)
    if len(call_gateway_candidates) != 1:
        raise PatchError(
            f"expected one gateway call implementation, found {len(call_gateway_candidates)}"
        )

    changed = _patch_runtime(candidates[0])
    agent_tools_changed = _patch_agent_tools(agent_tool_candidates[0])
    gateway_tool_changed = _patch_gateway_tool(gateway_tool_candidates[0])
    call_gateway_changed = _patch_call_gateway(call_gateway_candidates[0])
    return {
        "status": (
            "patched"
            if changed
            or agent_tools_changed
            or gateway_tool_changed
            or call_gateway_changed
            else "already_patched"
        ),
        "version": version,
        "module": candidates[0].name,
        "agent_tools_module": agent_tool_candidates[0].name,
        "gateway_tool_module": gateway_tool_candidates[0].name,
        "call_gateway_module": call_gateway_candidates[0].name,
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
