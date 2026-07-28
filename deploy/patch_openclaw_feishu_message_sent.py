#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat


AFFECTED_VERSION = re.compile(r"^2026\.7\.1-2$")
PATCH_MARKER = "async function emitEimemoryFeishuMessageSent(params)"
DISPATCHER_MARKER = "function createFeishuReplyDispatcher(params) {"
DISPATCHER_END_MARKER = "\n//#endregion"


class PatchError(RuntimeError):
    pass


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.eimemory-{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _helper_source(newline: str) -> str:
    lines = [
        "async function emitEimemoryFeishuMessageSent(params) {",
        '\tconst messageId = String(params.messageId || "").trim();',
        "\tif (!messageId) return;",
        "\ttry {",
        "\t\tconst [{ getGlobalHookRunner }, hooks] = await Promise.all([",
        '\t\t\timport("./plugins/hook-runner-global.js"),',
        '\t\t\timport("./plugin-sdk/hook-runtime.js")',
        "\t\t]);",
        "\t\tconst hookRunner = getGlobalHookRunner();",
        '\t\tif (!hookRunner?.hasHooks("message_sent")) return;',
        "\t\tconst canonical = hooks.buildCanonicalSentMessageHookContext({",
        "\t\t\tto: params.to,",
        "\t\t\tcontent: params.content,",
        "\t\t\tsuccess: true,",
        '\t\t\tchannelId: "feishu",',
        "\t\t\taccountId: params.accountId,",
        "\t\t\tconversationId: params.conversationId,",
        "\t\t\tsessionKey: params.sessionKey,",
        "\t\t\tmessageId",
        "\t\t});",
        "\t\tawait hookRunner.runMessageSent(",
        "\t\t\thooks.toPluginMessageSentEvent(canonical),",
        "\t\t\thooks.toPluginMessageContext(canonical)",
        "\t\t);",
        "\t} catch (error) {",
        "\t\tparams.log?.(`eimemory Feishu message_sent hook failed: ${String(error)}`);",
        "\t}",
        "}",
    ]
    return newline.join(lines) + newline


def _dispatcher_receipt_source(indent: str, newline: str) -> str:
    inner = indent + "\t"
    deep = inner + "\t"
    lines = [
        f'{indent}let eimemoryFeishuReceiptMessageId = "";',
        f"{indent}const rememberEimemoryFeishuReceipt = (result) => {{",
        f'{inner}const messageId = String(result?.messageId || "").trim();',
        f"{inner}if (messageId && !eimemoryFeishuReceiptMessageId) "
        "eimemoryFeishuReceiptMessageId = messageId;",
        f"{inner}return result;",
        f"{indent}}};",
        f"{indent}const emitRememberedEimemoryFeishuReceipt = async "
        "(content, explicitMessageId) => {",
        f"{inner}const messageId = String("
        'explicitMessageId || eimemoryFeishuReceiptMessageId || "").trim();',
        f"{inner}if (!messageId) return;",
        f"{inner}await emitEimemoryFeishuMessageSent({{",
        f"{deep}to: sendTarget,",
        f"{deep}content,",
        f"{deep}messageId,",
        f"{deep}accountId,",
        f"{deep}conversationId: chatId,",
        f"{deep}sessionKey: params.sessionKey,",
        f"{deep}log: (message) => params.runtime?.error?.(message)",
        f"{inner}}});",
        f"{inner}if (messageId === eimemoryFeishuReceiptMessageId) "
        'eimemoryFeishuReceiptMessageId = "";',
        f"{indent}}};",
        f"{indent}const sendMessageFeishuWithEimemoryReceipt = async "
        "(sendParams) => rememberEimemoryFeishuReceipt(",
        f"{inner}await sendMessageFeishu(sendParams)",
        f"{indent});",
        f"{indent}const sendStructuredCardFeishuWithEimemoryReceipt = async "
        "(sendParams) => rememberEimemoryFeishuReceipt(",
        f"{inner}await sendStructuredCardFeishu(sendParams)",
        f"{indent});",
    ]
    return newline.join(lines) + newline


def _patch_dispatcher(text: str, path: Path) -> tuple[str, bool]:
    if PATCH_MARKER in text:
        return text, False
    if text.count(DISPATCHER_MARKER) != 1:
        raise PatchError(f"expected one Feishu reply dispatcher in {path.name}")
    start = text.index(DISPATCHER_MARKER)
    end = text.find(DISPATCHER_END_MARKER, start)
    if end < 0:
        raise PatchError(f"missing Feishu dispatcher end marker in {path.name}")
    region = text[start:end]
    newline = "\r\n" if "\r\n" in text else "\n"

    direct_count = region.count("await sendMessageFeishu({")
    card_count = region.count("await sendStructuredCardFeishu({")
    if direct_count + card_count < 1:
        raise PatchError(f"missing Feishu API send anchors in {path.name}")
    region = region.replace(
        "await sendMessageFeishu({",
        "await sendMessageFeishuWithEimemoryReceipt({",
    ).replace(
        "await sendStructuredCardFeishu({",
        "await sendStructuredCardFeishuWithEimemoryReceipt({",
    )

    params_line = re.compile(
        r"^(?P<indent>[ \t]+)const \{[^\r\n]+\} = params;\r?$",
        re.MULTILINE,
    )
    params_matches = list(params_line.finditer(region))
    if len(params_matches) != 1:
        raise PatchError(f"expected one dispatcher params binding in {path.name}")
    match = params_matches[0]
    indent = match.group("indent")
    insertion = match.end()
    region = (
        region[:insertion]
        + newline
        + _dispatcher_receipt_source(indent, newline)
        + region[insertion + (1 if region[insertion : insertion + 1] == "\n" else 0) :]
    )

    final_anchor = re.compile(
        r'(?P<indent>[ \t]+)if \(paramsLocal\.infoKind === "final"\)',
    )
    final_matches = list(final_anchor.finditer(region))
    if len(final_matches) != 1:
        raise PatchError(f"expected one final chunk receipt anchor in {path.name}")
    final_match = final_matches[0]
    final_indent = final_match.group("indent")
    region = (
        region[: final_match.start()]
        + f'{final_indent}if (paramsLocal.infoKind === "final") '
        f"await emitRememberedEimemoryFeishuReceipt(paramsLocal.text);{newline}"
        + region[final_match.start() :]
    )

    streaming_anchor = re.compile(
        r'(?P<indent>[ \t]+)const contentVisible = await streaming\.close\(text, '
        r"\{ note: [^}\r\n]+ \}\);"
    )
    streaming_matches = list(streaming_anchor.finditer(region))
    if len(streaming_matches) != 1:
        raise PatchError(f"expected one streaming close anchor in {path.name}")
    stream_match = streaming_matches[0]
    stream_indent = stream_match.group("indent")
    original = stream_match.group(0)
    streaming_replacement = newline.join(
        [
            f'{stream_indent}const eimemoryStreamingMessageId = String('
            'streaming?.state?.messageId || "");',
            original,
            f"{stream_indent}if (contentVisible && streamText && "
            "eimemoryStreamingMessageId) await emitRememberedEimemoryFeishuReceipt(",
            f"{stream_indent}\tstreamText,",
            f"{stream_indent}\teimemoryStreamingMessageId",
            f"{stream_indent});",
        ]
    )
    region = (
        region[: stream_match.start()]
        + streaming_replacement
        + region[stream_match.end() :]
    )

    helper = _helper_source(newline)
    patched = text[:start] + helper + region + text[end:]
    return patched, True


def patch_openclaw_feishu_message_sent(openclaw_root: Path) -> dict[str, object]:
    root = Path(os.path.abspath(openclaw_root))
    if root.is_symlink() or not root.is_dir():
        raise PatchError("OpenClaw root must be a real directory")
    package_path = root / "package.json"
    if package_path.is_symlink() or not package_path.is_file():
        raise PatchError("OpenClaw package metadata is missing")
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PatchError("OpenClaw package metadata is invalid") from exc
    version = str(package.get("version") or "")
    if AFFECTED_VERSION.fullmatch(version) is None:
        raise PatchError(f"unsupported OpenClaw version: {version}")
    dist = root / "dist"
    if dist.is_symlink() or not dist.is_dir():
        raise PatchError("OpenClaw dist must be a real directory")
    candidates = [
        path
        for path in sorted(dist.glob("monitor.account-*.js"))
        if DISPATCHER_MARKER in path.read_text(encoding="utf-8")
    ]
    if len(candidates) != 1:
        raise PatchError("expected exactly one Feishu monitor runtime")
    path = candidates[0]
    if path.is_symlink() or not path.is_file():
        raise PatchError("Feishu monitor runtime must be a regular file")
    text = path.read_text(encoding="utf-8")
    patched, changed = _patch_dispatcher(text, path)
    if changed:
        _atomic_write(path, patched)
    return {
        "ok": True,
        "status": "patched" if changed else "already_patched",
        "version": version,
        "runtime": path.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Patch OpenClaw Feishu automatic replies to emit message_sent receipts."
    )
    parser.add_argument("--openclaw-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = patch_openclaw_feishu_message_sent(args.openclaw_root)
    except (OSError, PatchError) as exc:
        parser.exit(2, f"OpenClaw Feishu receipt patch failed: {exc}\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
