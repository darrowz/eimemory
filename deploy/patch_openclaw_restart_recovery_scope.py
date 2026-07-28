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
AGENT_TOOL_GATEWAY_LEGACY = (
    'const gatewayCall = opts?.callGateway ?? ((request) => callGateway({ '
    '...request, clientName: "cli", mode: "cli" }));'
)
AGENT_TOOL_GATEWAY_STORED_AUTH = (
    'const gatewayCall = opts?.callGateway ?? ((request) => callGateway({ '
    '...request, clientName: "cli", mode: "cli", useStoredDeviceAuth: true }));'
)
AGENT_TOOLS_DEPS_DEFAULT = "let openClawToolsDeps = { callGateway };"
AGENT_TOOLS_DEPS_LEGACY = (
    'const callGatewayAsCli = (request) => callGateway({ ...request, clientName: "cli", mode: "cli" });\n'
    "let openClawToolsDeps = { callGateway: callGatewayAsCli };"
)
AGENT_TOOLS_DEPS_STORED_AUTH = (
    'const callGatewayAsCli = (request) => callGateway({ ...request, clientName: "cli", '
    'mode: "cli", useStoredDeviceAuth: true });\n'
    "let openClawToolsDeps = { callGateway: callGatewayAsCli };"
)
GATEWAY_TOOL_MARKERS = (
    "const AGENT_RUNTIME_IDENTITY_METHODS",
    "async function callGatewayTool(method, opts, params, extra)",
)
GATEWAY_TOOL_READ_IDENTITY_MARKER = "const useLocalOperatorReadIdentity ="
GATEWAY_TOOL_URL_DEFAULT = "url: gateway.url,"
GATEWAY_TOOL_URL_STORED_AUTH = (
    "url: useLocalOperatorReadIdentity ? void 0 : gateway.url,"
)
GATEWAY_TOOL_TOKEN_DEFAULT = "token: gateway.token,"
GATEWAY_TOOL_TOKEN_STORED_AUTH = (
    "token: useLocalOperatorReadIdentity ? void 0 : gateway.token,"
)
GATEWAY_TOOL_STORED_AUTH_MARKER = (
    "useStoredDeviceAuth: useLocalOperatorReadIdentity,"
)
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
CALL_GATEWAY_LEGACY_READ_SCOPE_MARKER = "const defaultReadScopes ="
CALL_GATEWAY_INTERMEDIATE_STORED_AUTH_MARKER = "const defaultLocalContext ="
CALL_GATEWAY_STORED_AUTH_MARKER = "const localStoredAuthContext ="
RECOVERY_QUARANTINE_HELPER_MARKER = "function takeEimemoryRecoveryQuarantine()"
RECOVERY_QUARANTINE_BRANCH_MARKER = (
    "shouldQuarantineRestartRecovery(params.recoveryQuarantine, entry, sessionKey)"
)
RECOVERY_QUARANTINE_LOAD_MARKER = (
    "const recoveryQuarantine = takeEimemoryRecoveryQuarantine();"
)
RECOVERY_QUARANTINE_FINALIZE_MARKER = (
    "finalizeEimemoryRecoveryQuarantine(recoveryQuarantine);"
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
            rf'await callGateway\(\{{\s*clientName: "cli",\s*mode: "cli",\s*'
            rf'useStoredDeviceAuth: true,\s*method: "{escaped}",'
        )
        if len(patched.findall(text)) == 1:
            continue
        legacy = re.compile(
            rf'(?P<prefix>await callGateway\(\{{\s*clientName: "cli",\s*mode: "cli",\s*)'
            rf'(?P<indent>[ \t]+)method: "{escaped}",'
        )
        legacy_matches = list(legacy.finditer(text))
        if len(legacy_matches) == 1:
            match = legacy_matches[0]
            text = legacy.sub(
                f'{match.group("prefix")}{match.group("indent")}useStoredDeviceAuth: true,'
                f'{newline}{match.group("indent")}method: "{method}",',
                text,
                count=1,
            )
            changed = True
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
                f"{indent}useStoredDeviceAuth: true,{newline}"
                f'{indent}method: "{method}",'
            )

        text = original.sub(replace, text, count=1)
        changed = True
    if changed:
        _atomic_write(path, text)
    return changed


def _recovery_function_regions(
    text: str,
    path: Path,
) -> tuple[tuple[int, int], tuple[int, int]]:
    store_start_marker = "async function recoverStore(params) {"
    store_end_marker = "async function resolveRestartRecoveryStorePaths("
    outer_start_marker = "async function recoverRestartAbortedMainSessions(params = {}) {"
    outer_end_marker = "async function recoverStartupOrphanedMainSessions(params = {}) {"
    markers = (
        store_start_marker,
        store_end_marker,
        outer_start_marker,
        outer_end_marker,
    )
    if any(text.count(marker) != 1 for marker in markers):
        raise PatchError(f"expected recovery entrypoint anchors in {path.name}")

    store_start = text.index(store_start_marker)
    store_end = text.index(store_end_marker)
    outer_start = text.index(outer_start_marker)
    outer_end = text.index(outer_end_marker)
    if not store_start < store_end <= outer_start < outer_end:
        raise PatchError(f"unexpected recovery entrypoint ordering in {path.name}")
    return (store_start, store_end), (outer_start, outer_end)


def _validate_recovery_quarantine_patch(text: str, path: Path) -> None:
    store_region, outer_region = _recovery_function_regions(text, path)
    store_body = text[slice(*store_region)]
    outer_body = text[slice(*outer_region)]
    helper_markers = (
        RECOVERY_QUARANTINE_HELPER_MARKER,
        "function isValidEimemoryRecoveryQuarantine(quarantine, requireCurrent) {",
        'quarantine?.schema === "openclaw_recovery_quarantine.v1"',
        'claimedText = fs.readFileSync(EIMEMORY_RECOVERY_QUARANTINE_CLAIM_PATH, "utf8");',
        "if (!isValidEimemoryRecoveryQuarantine(claimed, false)) {",
        "if (!isValidEimemoryRecoveryQuarantine(quarantine, true)) {",
        "fs.renameSync(EIMEMORY_RECOVERY_QUARANTINE_PATH, EIMEMORY_RECOVERY_QUARANTINE_CLAIM_PATH);",
        "function finalizeEimemoryRecoveryQuarantine(quarantine) {",
        "fs.unlinkSync(quarantine[EIMEMORY_RECOVERY_QUARANTINE_CLAIM_PROPERTY]);",
        "function shouldQuarantineRestartRecovery(quarantine, entry, sessionKey) {",
        "return quarantine.session_ids.includes(entry.sessionId) || quarantine.session_ids.includes(sessionKey);",
    )
    if any(text.count(marker) != 1 for marker in helper_markers):
        raise PatchError(f"incomplete recovery quarantine patch in {path.name}")

    branch_start_marker = f"if ({RECOVERY_QUARANTINE_BRANCH_MARKER}) {{"
    resume_marker = (
        "if (entry.pendingFinalDelivery === true && "
        "entry.pendingFinalDeliveryText) {"
    )
    if store_body.count(branch_start_marker) != 1 or store_body.count(resume_marker) != 1:
        raise PatchError(f"incomplete recovery quarantine patch in {path.name}")
    branch_start = store_body.index(branch_start_marker)
    resume_start = store_body.index(resume_marker, branch_start)
    gate_markers = (
        'if (!entry || entry.status !== "running" || entry.abortedLastRun !== true) continue;',
        "if (shouldSkipMainRecovery(entry, sessionKey))",
        "if (!isRoutableRecoveryStore(",
        "if (hasCurrentProcessOwner(",
        "const resumeDedupeKey = sessionKey;",
        "if (params.resumedSessionKeys.has(resumeDedupeKey))",
    )
    if any(store_body.count(marker) != 1 for marker in gate_markers):
        raise PatchError(f"incomplete recovery quarantine patch in {path.name}")
    gate_positions = [store_body.index(marker) for marker in gate_markers]
    if gate_positions != sorted(gate_positions) or not gate_positions[-1] < branch_start:
        raise PatchError(f"incomplete recovery quarantine patch in {path.name}")
    branch_body = store_body[branch_start:resume_start]
    ordered_branch_markers = (
        "await markSessionFailed({",
        "params.resumedSessionKeys.add(resumeDedupeKey);",
        "result.failed++;",
        "await sendUnresumableSessionNotice({",
        "continue;",
    )
    positions = [branch_body.find(marker) for marker in ordered_branch_markers]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        raise PatchError(f"incomplete recovery quarantine patch in {path.name}")

    store_pass = re.compile(
        r"const storeResult = await recoverStore\(\{\s*recoveryQuarantine,",
    )
    if (
        outer_body.count(RECOVERY_QUARANTINE_LOAD_MARKER) != 1
        or outer_body.count(RECOVERY_QUARANTINE_FINALIZE_MARKER) != 1
        or len(store_pass.findall(outer_body)) != 1
    ):
        raise PatchError(f"incomplete recovery quarantine patch in {path.name}")
    load_position = outer_body.index(RECOVERY_QUARANTINE_LOAD_MARKER)
    store_position = store_pass.search(outer_body)
    finalize_position = outer_body.index(RECOVERY_QUARANTINE_FINALIZE_MARKER)
    if (
        store_position is None
        or not load_position < store_position.start() < finalize_position
    ):
        raise PatchError(f"incomplete recovery quarantine patch in {path.name}")


def _patch_recovery_quarantine(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    store_region, outer_region = _recovery_function_regions(text, path)
    helper_count = text.count(RECOVERY_QUARANTINE_HELPER_MARKER)
    if helper_count == 1:
        _validate_recovery_quarantine_patch(text, path)
        return False
    if helper_count != 0:
        raise PatchError(f"incomplete recovery quarantine patch in {path.name}")
    if any(
        marker in text
        for marker in (
            RECOVERY_QUARANTINE_BRANCH_MARKER,
            RECOVERY_QUARANTINE_LOAD_MARKER,
            RECOVERY_QUARANTINE_FINALIZE_MARKER,
        )
    ):
        raise PatchError(f"incomplete recovery quarantine patch in {path.name}")

    store_body = text[slice(*store_region)]
    outer_body = text[slice(*outer_region)]
    guard_anchor = re.compile(
        r'^(?P<indent>[ \t]+)if \(!entry \|\| entry\.status !== "running" \|\| '
        r'entry\.abortedLastRun !== true\) continue;$',
        re.MULTILINE,
    )
    guard_matches = list(guard_anchor.finditer(store_body))
    if len(guard_matches) != 1:
        raise PatchError(f"expected one recovery guard anchor in {path.name}")

    resume_anchor = re.compile(
        r'^(?P<indent>[ \t]+)if \(entry\.pendingFinalDelivery === true && '
        r"entry\.pendingFinalDeliveryText\) \{$",
        re.MULTILINE,
    )
    resume_matches = list(resume_anchor.finditer(store_body))
    if len(resume_matches) != 1:
        raise PatchError(f"expected one first recovery resume anchor in {path.name}")
    gate_markers = (
        "if (shouldSkipMainRecovery(entry, sessionKey))",
        "if (!isRoutableRecoveryStore(",
        "if (hasCurrentProcessOwner(",
        "const resumeDedupeKey = sessionKey;",
        "if (params.resumedSessionKeys.has(resumeDedupeKey))",
    )
    if any(store_body.count(marker) != 1 for marker in gate_markers):
        raise PatchError(f"expected recovery gate anchors in {path.name}")
    gate_positions = [store_body.index(marker) for marker in gate_markers]
    if (
        gate_positions != sorted(gate_positions)
        or not guard_matches[0].start() < gate_positions[0]
        or not gate_positions[-1] < resume_matches[0].start()
    ):
        raise PatchError(f"unexpected recovery gate ordering in {path.name}")

    store_loop_anchor = re.compile(
        r"^(?P<indent>[ \t]+)for \(const storePath of await "
        r"resolveRestartRecoveryStorePaths\(params\)\) \{"
        r"(?=\r?\n(?P=indent)[ \t]+const storeResult = await recoverStore\(\{)",
        re.MULTILINE,
    )
    store_loop_matches = list(store_loop_anchor.finditer(outer_body))
    if len(store_loop_matches) != 1:
        raise PatchError(f"expected one recovery store loop anchor in {path.name}")

    store_call_anchor = re.compile(
        r"^(?P<indent>[ \t]+)const storeResult = await recoverStore\(\{"
        r"(?P<tail>[^\r\n]*)$",
        re.MULTILINE,
    )
    store_call_matches = list(store_call_anchor.finditer(outer_body))
    if len(store_call_matches) != 1:
        raise PatchError(f"expected one recoverStore call anchor in {path.name}")

    finalize_anchor = re.compile(
        r"^(?P<item_indent>[ \t]+)result\.skipped \+= storeResult\.skipped;"
        r"\r?\n(?P<loop_indent>[ \t]+)\}$",
        re.MULTILINE,
    )
    finalize_matches = list(finalize_anchor.finditer(outer_body))
    if len(finalize_matches) != 1:
        raise PatchError(f"expected one recovery finalization anchor in {path.name}")

    helpers = newline.join(
        (
            "const EIMEMORY_RECOVERY_QUARANTINE_PATH =",
            "  process.env.EIMEMORY_OPENCLAW_RECOVERY_QUARANTINE_PATH",
            '  || "/var/lib/eimemory/openclaw_recovery_quarantine.json";',
            "const EIMEMORY_RECOVERY_QUARANTINE_CLAIM_PATH =",
            '  `${EIMEMORY_RECOVERY_QUARANTINE_PATH}.in-progress`;',
            'const EIMEMORY_RECOVERY_QUARANTINE_REASON = "quarantined by eimemory recovery circuit breaker";',
            'const EIMEMORY_RECOVERY_QUARANTINE_CLAIM_PROPERTY = "__eimemoryRecoveryQuarantineClaimPath";',
            "function isEimemoryRecoveryQuarantineStringArray(value) {",
            '  return Array.isArray(value) && value.every((item) => typeof item === "string" && item.length > 0);',
            "}",
            "function isValidEimemoryRecoveryQuarantine(quarantine, requireCurrent) {",
            "  const nowTs = Date.now() / 1e3;",
            "  return quarantine?.schema === \"openclaw_recovery_quarantine.v1\" &&",
            '    typeof quarantine.trigger === "string" && quarantine.trigger.length > 0 &&',
            "    Number.isFinite(quarantine.created_at_ts) &&",
            "    Number.isFinite(quarantine.expires_at_ts) &&",
            "    quarantine.created_at_ts < quarantine.expires_at_ts &&",
            '    (quarantine.mode === "targeted" || quarantine.mode === "all_previous_lifecycle") &&',
            "    isEimemoryRecoveryQuarantineStringArray(quarantine.session_ids) &&",
            "    quarantine.consumed === false &&",
            '    (quarantine.mode !== "targeted" || quarantine.session_ids.length > 0) &&',
            '    (quarantine.mode !== "all_previous_lifecycle" || quarantine.session_ids.length === 0) &&',
            "    (!requireCurrent || (quarantine.created_at_ts <= nowTs && nowTs < quarantine.expires_at_ts));",
            "}",
            "function attachEimemoryRecoveryQuarantineClaim(quarantine) {",
            "  Object.defineProperty(quarantine, EIMEMORY_RECOVERY_QUARANTINE_CLAIM_PROPERTY, {",
            "    value: EIMEMORY_RECOVERY_QUARANTINE_CLAIM_PATH",
            "  });",
            "  return quarantine;",
            "}",
            "function takeEimemoryRecoveryQuarantine() {",
            "  let claimedText;",
            "  try {",
            '    claimedText = fs.readFileSync(EIMEMORY_RECOVERY_QUARANTINE_CLAIM_PATH, "utf8");',
            "  } catch (err) {",
            '    if (err?.code !== "ENOENT") throw err;',
            "  }",
            "  if (claimedText !== void 0) {",
            "    let claimed;",
            "    try {",
            "      claimed = JSON.parse(claimedText);",
            "    } catch (err) {",
            '      throw new Error(`invalid claimed eimemory recovery quarantine: ${String(err)}`);',
            "    }",
            "    if (!isValidEimemoryRecoveryQuarantine(claimed, false)) {",
            '      throw new Error("invalid claimed eimemory recovery quarantine state");',
            "    }",
            "    return attachEimemoryRecoveryQuarantineClaim(claimed);",
            "  }",
            "  let liveText;",
            "  try {",
            '    liveText = fs.readFileSync(EIMEMORY_RECOVERY_QUARANTINE_PATH, "utf8");',
            "  } catch (err) {",
            '    if (err?.code === "ENOENT") return void 0;',
            "    throw err;",
            "  }",
            "  let quarantine;",
            "  try {",
            "    quarantine = JSON.parse(liveText);",
            "  } catch (err) {",
            '    log.warn(`ignored malformed eimemory recovery quarantine: ${String(err)}`);',
            "    return void 0;",
            "  }",
            "  if (!isValidEimemoryRecoveryQuarantine(quarantine, true)) {",
            '    log.warn("ignored invalid or expired eimemory recovery quarantine");',
            "    return void 0;",
            "  }",
            "  fs.renameSync(EIMEMORY_RECOVERY_QUARANTINE_PATH, EIMEMORY_RECOVERY_QUARANTINE_CLAIM_PATH);",
            "  return attachEimemoryRecoveryQuarantineClaim(quarantine);",
            "}",
            "function finalizeEimemoryRecoveryQuarantine(quarantine) {",
            "  if (!quarantine) return;",
            "  fs.unlinkSync(quarantine[EIMEMORY_RECOVERY_QUARANTINE_CLAIM_PROPERTY]);",
            "}",
            "function shouldQuarantineRestartRecovery(quarantine, entry, sessionKey) {",
            "  if (!quarantine) return false;",
            '  if (quarantine.mode === "all_previous_lifecycle") return true;',
            "  return quarantine.session_ids.includes(entry.sessionId) || quarantine.session_ids.includes(sessionKey);",
            "}",
            "",
        )
    )
    resume_match = resume_matches[0]
    indent = resume_match.group("indent")
    indent_unit = "\t" if "\t" in indent else "  "
    child_indent = indent + indent_unit
    branch = newline.join(
        (
            f"{indent}if ({RECOVERY_QUARANTINE_BRANCH_MARKER}) {{",
            f"{child_indent}await markSessionFailed({{",
            f"{child_indent}{indent_unit}storePath: params.storePath,",
            f"{child_indent}{indent_unit}sessionKey,",
            f"{child_indent}{indent_unit}reason: EIMEMORY_RECOVERY_QUARANTINE_REASON",
            f"{child_indent}}});",
            f"{child_indent}params.resumedSessionKeys.add(resumeDedupeKey);",
            f"{child_indent}result.failed++;",
            f"{child_indent}await sendUnresumableSessionNotice({{",
            f"{child_indent}{indent_unit}cfg: params.cfg,",
            f"{child_indent}{indent_unit}entry,",
            f"{child_indent}{indent_unit}sessionKey,",
            f"{child_indent}{indent_unit}reason: EIMEMORY_RECOVERY_QUARANTINE_REASON",
            f"{child_indent}}});",
            f"{child_indent}continue;",
            f"{indent}}}",
            resume_match.group(0),
        )
    )
    patched_store_body = resume_anchor.sub(branch, store_body, count=1)
    text = text[: store_region[0]] + helpers + patched_store_body + text[store_region[1] :]

    store_loop_match = store_loop_matches[0]
    store_loop = newline.join(
        (
            f"{store_loop_match.group('indent')}{RECOVERY_QUARANTINE_LOAD_MARKER}",
            store_loop_match.group(0),
        )
    )
    patched_outer_body = store_loop_anchor.sub(store_loop, outer_body, count=1)

    store_call_match = store_call_matches[0]
    call_indent = store_call_match.group("indent")
    call_tail = store_call_match.group("tail")
    if call_tail:
        store_call = (
            f"{call_indent}const storeResult = await recoverStore("
            f"{{ recoveryQuarantine,{call_tail}"
        )
    else:
        call_indent_unit = "\t" if "\t" in call_indent else "  "
        store_call = newline.join(
            (
                f"{call_indent}const storeResult = await recoverStore({{",
                f"{call_indent}{call_indent_unit}recoveryQuarantine,",
            )
        )
    patched_outer_body = store_call_anchor.sub(store_call, patched_outer_body, count=1)

    finalize_match = finalize_matches[0]
    finalization = newline.join(
        (
            finalize_match.group(0),
            f"{finalize_match.group('loop_indent')}{RECOVERY_QUARANTINE_FINALIZE_MARKER}",
        )
    )
    patched_outer_body = finalize_anchor.sub(finalization, patched_outer_body, count=1)
    _, updated_outer_region = _recovery_function_regions(text, path)
    text = (
        text[: updated_outer_region[0]]
        + patched_outer_body
        + text[updated_outer_region[1] :]
    )
    _validate_recovery_quarantine_patch(text, path)
    _atomic_write(path, text)
    return True


def _patch_agent_tools(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    expected_count = len(AGENT_TOOL_MARKERS)
    original = text
    text = text.replace(
        AGENT_TOOL_GATEWAY_DEFAULT,
        AGENT_TOOL_GATEWAY_STORED_AUTH,
    ).replace(
        AGENT_TOOL_GATEWAY_LEGACY,
        AGENT_TOOL_GATEWAY_STORED_AUTH,
    )
    if text.count(AGENT_TOOL_GATEWAY_STORED_AUTH) != expected_count:
        raise PatchError(
            f"expected {expected_count} consistent agent tool gateway defaults in {path.name}"
        )

    legacy_deps = AGENT_TOOLS_DEPS_LEGACY.replace("\n", newline)
    stored_auth_deps = AGENT_TOOLS_DEPS_STORED_AUTH.replace("\n", newline)
    text = text.replace(AGENT_TOOLS_DEPS_DEFAULT, stored_auth_deps).replace(
        legacy_deps,
        stored_auth_deps,
    )
    if text.count(stored_auth_deps) != 1:
        raise PatchError(f"expected one consistent agent tools dependency boundary in {path.name}")

    changed = text != original
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
        (GATEWAY_TOOL_URL_DEFAULT, GATEWAY_TOOL_URL_STORED_AUTH),
        (GATEWAY_TOOL_TOKEN_DEFAULT, GATEWAY_TOOL_TOKEN_STORED_AUTH),
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

    stored_auth_count = text.count(GATEWAY_TOOL_STORED_AUTH_MARKER)
    if stored_auth_count == 0:
        anchor = re.compile(
            rf"^(?P<indent>[ \t]+){re.escape(GATEWAY_TOOL_CLIENT_READ_ONLY)}",
            re.MULTILINE,
        )
        matches = list(anchor.finditer(text))
        if len(matches) != 1:
            raise PatchError(
                f"expected one local stored auth insertion point in {path.name}"
            )
        indent = matches[0].group("indent")
        replacement = (
            f"{indent}{GATEWAY_TOOL_STORED_AUTH_MARKER}{newline}"
            f"{indent}{GATEWAY_TOOL_CLIENT_READ_ONLY}"
        )
        text = anchor.sub(replacement, text, count=1)
        changed = True
    elif stored_auth_count != 1:
        raise PatchError(f"expected one local stored auth marker in {path.name}")

    if changed:
        _atomic_write(path, text)
    return changed


def _patch_call_gateway(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    marker_count = text.count(CALL_GATEWAY_STORED_AUTH_MARKER)
    if marker_count == 1:
        return False
    if marker_count != 0:
        raise PatchError(f"expected one gateway stored auth marker in {path.name}")

    fallback_anchor = re.compile(
        r"^(?P<indent>[ \t]+)return await callGatewayLeastPrivilege\(\{",
        re.MULTILINE,
    )
    matches = list(fallback_anchor.finditer(text))
    if len(matches) != 1:
        raise PatchError(f"expected one default gateway call anchor in {path.name}")
    indent = matches[0].group("indent")

    for legacy_marker in (
        CALL_GATEWAY_INTERMEDIATE_STORED_AUTH_MARKER,
        CALL_GATEWAY_LEGACY_READ_SCOPE_MARKER,
    ):
        legacy_count = text.count(legacy_marker)
        if legacy_count == 0:
            continue
        if legacy_count != 1:
            raise PatchError(
                f"expected at most one legacy gateway auth marker in {path.name}"
            )
        legacy = re.compile(
            rf"^{re.escape(indent)}{re.escape(legacy_marker)}.*?"
            rf"^{re.escape(indent)}\}}{re.escape(newline)}",
            re.MULTILINE | re.DOTALL,
        )
        legacy_matches = list(legacy.finditer(text))
        if len(legacy_matches) != 1:
            raise PatchError(f"expected one legacy gateway auth block in {path.name}")
        text = legacy.sub("", text, count=1)

    cli_condition = (
        r"callerMode === GATEWAY_CLIENT_MODES\.CLI \|\| "
        r"callerName === GATEWAY_CLIENT_NAMES\.CLI"
    )
    cli_branch = re.compile(
        rf"^{re.escape(indent)}if \({cli_condition}\) "
        rf"(?:\{{{re.escape(newline)}"
        rf"{re.escape(indent)}    return await callGatewayCli\(opts\);"
        rf"{re.escape(newline)}{re.escape(indent)}\}}|"
        rf"return await callGatewayCli\(opts\);){re.escape(newline)}",
        re.MULTILINE,
    )
    cli_matches = list(cli_branch.finditer(text))
    if len(cli_matches) != 1:
        raise PatchError(f"expected one CLI gateway call branch in {path.name}")

    stored_auth_context = newline.join(
        (
            f"{indent}const localStoredAuthContext =",
            f"{indent}    opts.useStoredDeviceAuth === void 0 &&",
            f"{indent}    opts.url === void 0 &&",
            f"{indent}    opts.token === void 0 &&",
            f"{indent}    opts.password === void 0 &&",
            f"{indent}    (",
            f"{indent}        callerMode === GATEWAY_CLIENT_MODES.CLI ||",
            f"{indent}        callerName === GATEWAY_CLIENT_NAMES.CLI ||",
            f"{indent}        (opts.mode === void 0 && opts.clientName === void 0)",
            f"{indent}    )",
            f"{indent}        ? await resolveGatewayCallContext(opts)",
            f"{indent}        : null;",
            f"{indent}const useLocalStoredDeviceAuth = Boolean(",
            f"{indent}    localStoredAuthContext &&",
            f"{indent}    !localStoredAuthContext.urlOverride &&",
            f"{indent}    !localStoredAuthContext.isRemoteMode",
            f"{indent});",
            f"{indent}if (callerMode === GATEWAY_CLIENT_MODES.CLI || callerName === GATEWAY_CLIENT_NAMES.CLI) {{",
            f"{indent}    return await callGatewayCli(",
            f"{indent}        useLocalStoredDeviceAuth",
            f"{indent}            ? {{ ...opts, useStoredDeviceAuth: true }}",
            f"{indent}            : opts",
            f"{indent}    );",
            f"{indent}}}",
        )
    )
    text = cli_branch.sub(stored_auth_context + newline, text, count=1)

    matches = list(fallback_anchor.finditer(text))
    if len(matches) != 1:
        raise PatchError(f"expected one post-patch gateway call anchor in {path.name}")
    stored_auth_fallback = newline.join(
        (
            f"{indent}if (useLocalStoredDeviceAuth) {{",
            f"{indent}    return await callGatewayCli({{ ...opts, useStoredDeviceAuth: true }});",
            f"{indent}}}",
        )
    )
    anchor_start = matches[0].start()
    text = text[:anchor_start] + stored_auth_fallback + newline + text[anchor_start:]
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

    recovery_quarantine_changed = _patch_recovery_quarantine(candidates[0])
    changed = _patch_runtime(candidates[0])
    agent_tools_changed = _patch_agent_tools(agent_tool_candidates[0])
    gateway_tool_changed = _patch_gateway_tool(gateway_tool_candidates[0])
    call_gateway_changed = _patch_call_gateway(call_gateway_candidates[0])
    return {
        "status": (
            "patched"
            if recovery_quarantine_changed
            or changed
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
