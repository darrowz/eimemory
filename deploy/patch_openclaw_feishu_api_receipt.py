#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat


AFFECTED_VERSION = re.compile(r"^2026\.7\.1-2$")
PATCH_MARKER = "async function emitEimemoryFeishuApiAccepted(params)"
LEGACY_PATCH_MARKER = "async function emitEimemoryFeishuMessageSent(params)"
PATCH_VERSION_MARKER = "// eimemory-feishu-api-receipt-patch:v1"
LEGACY_V3_PATCH_VERSION_MARKER = "// eimemory-feishu-message-sent-patch:v3"
LEGACY_V2_PATCH_VERSION_MARKER = "// eimemory-feishu-message-sent-patch:v2"
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
        PATCH_VERSION_MARKER,
        "async function emitEimemoryFeishuApiAccepted(params) {",
        '\tconst messageId = String(params.messageId || "").trim();',
        "\tif (!messageId) return;",
        "\ttry {",
        '\t\tconst sink = globalThis[Symbol.for("eimemory.feishu.apiAccepted.v1")];',
        '\t\tif (typeof sink !== "function") {',
        '\t\t\tparams.log?.("eimemory Feishu API receipt sink is unavailable");',
        "\t\t\treturn;",
        "\t\t}",
        "\t\tawait sink({",
        "\t\t\tto: params.to,",
        "\t\t\tcontent: params.content,",
        "\t\t\tsuccess: true,",
        "\t\t\taccountId: params.accountId,",
        "\t\t\tconversationId: params.conversationId,",
        "\t\t\tsessionKey: params.sessionKey,",
        "\t\t\tmessageId",
        "\t\t});",
        "\t} catch (error) {",
        "\t\tparams.log?.(`eimemory Feishu API receipt sink failed: ${String(error)}`);",
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
        f"{inner}if (messageId) "
        "eimemoryFeishuReceiptMessageId = messageId;",
        f"{inner}return result;",
        f"{indent}}};",
        f"{indent}const emitRememberedEimemoryFeishuReceipt = async "
        "(content, explicitMessageId) => {",
        f"{inner}const messageId = String("
        'explicitMessageId || eimemoryFeishuReceiptMessageId || "").trim();',
        f"{inner}if (!messageId) return;",
        f"{inner}await emitEimemoryFeishuApiAccepted({{",
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
    if PATCH_VERSION_MARKER in text:
        return text, False
    if LEGACY_PATCH_MARKER in text:
        return _upgrade_legacy_patch(text, path), True
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

    chunk_anchor = re.compile(
        r"^(?P<indent>[ \t]+)const sendChunkedTextReply = async "
        r"\(paramsLocal\) => \{\r?$",
        re.MULTILINE,
    )
    chunk_matches = list(chunk_anchor.finditer(region))
    if len(chunk_matches) != 1:
        raise PatchError(f"expected one chunked reply anchor in {path.name}")
    chunk_match = chunk_matches[0]
    chunk_insertion = chunk_match.end()
    chunk_indent = chunk_match.group("indent")
    region = (
        region[:chunk_insertion]
        + newline
        + f'{chunk_indent}\teimemoryFeishuReceiptMessageId = "";'
        + region[
            chunk_insertion
            + (1 if region[chunk_insertion : chunk_insertion + 1] == "\n" else 0) :
        ]
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
    patched = _patch_no_visible_reply_fallback(patched, path)
    return patched, True


def _patch_no_visible_reply_fallback(text: str, path: Path) -> str:
    queued_missing = (
        "const queuedFinalMissing = dispatchResult.queuedFinal === true "
        "&& finalCount === 0;"
    )
    patched_predicate = (
        "(emptyEligibleDispatch || queuedFinalFailed || queuedFinalMissing)"
    )
    if queued_missing in text and patched_predicate in text:
        return text
    if queued_missing in text or patched_predicate in text:
        raise PatchError(f"incomplete queued-final fallback patch in {path.name}")
    queued_failed_anchor = re.compile(
        r'^(?P<indent>[ \t]+)const queuedFinalFailed = '
        r'dispatchResult\.queuedFinal === true && failedFinalCount > 0;\r?$',
        re.MULTILINE,
    )
    queued_failed_matches = list(queued_failed_anchor.finditer(text))
    if len(queued_failed_matches) != 1:
        raise PatchError(
            f"expected one queued-final fallback anchor in {path.name}"
        )
    match = queued_failed_matches[0]
    newline = "\r\n" if "\r\n" in text else "\n"
    insertion = match.end()
    indent = match.group("indent")
    text = (
        text[:insertion]
        + newline
        + f"{indent}const queuedFinalMissing = "
        "dispatchResult.queuedFinal === true && finalCount === 0;"
        + text[
            insertion + (1 if text[insertion : insertion + 1] == "\n" else 0) :
        ]
    )
    predicate = "(emptyEligibleDispatch || queuedFinalFailed)"
    replacement = patched_predicate
    if text.count(predicate) != 1:
        raise PatchError(
            f"expected one no-visible-reply predicate in {path.name}"
        )
    return text.replace(predicate, replacement, 1)


def _upgrade_legacy_patch(text: str, path: Path) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    if (
        LEGACY_V3_PATCH_VERSION_MARKER in text
        or LEGACY_V2_PATCH_VERSION_MARKER in text
    ):
        markers = [
            marker
            for marker in (
                LEGACY_V3_PATCH_VERSION_MARKER,
                LEGACY_V2_PATCH_VERSION_MARKER,
            )
            if marker in text
        ]
        if len(markers) != 1 or text.count(LEGACY_PATCH_MARKER) != 1:
            raise PatchError(f"legacy Feishu receipt helper mismatch in {path.name}")
        helper_start = text.index(markers[0])
        dispatcher_start = text.index(DISPATCHER_MARKER, helper_start)
        upgraded = (
            text[:helper_start]
            + _helper_source(newline)
            + text[dispatcher_start:]
        )
        return _patch_no_visible_reply_fallback(upgraded, path)
    legacy_assignment = (
        "if (messageId && !eimemoryFeishuReceiptMessageId) "
        "eimemoryFeishuReceiptMessageId = messageId;"
    )
    if text.count(legacy_assignment) != 1:
        raise PatchError(f"legacy Feishu receipt assignment mismatch in {path.name}")
    upgraded = text.replace(
        legacy_assignment,
        "if (messageId) eimemoryFeishuReceiptMessageId = messageId;",
        1,
    )
    chunk_anchor = re.compile(
        r"^(?P<indent>[ \t]+)const sendChunkedTextReply = async "
        r"\(paramsLocal\) => \{\r?$",
        re.MULTILINE,
    )
    chunk_matches = list(chunk_anchor.finditer(upgraded))
    if len(chunk_matches) != 1:
        raise PatchError(f"legacy chunked reply anchor mismatch in {path.name}")
    match = chunk_matches[0]
    insertion = match.end()
    indent = match.group("indent")
    upgraded = (
        upgraded[:insertion]
        + newline
        + f'{indent}\teimemoryFeishuReceiptMessageId = "";'
        + upgraded[
            insertion + (1 if upgraded[insertion : insertion + 1] == "\n" else 0) :
        ]
    )
    if upgraded.count(LEGACY_PATCH_MARKER) != 1:
        raise PatchError(f"legacy Feishu receipt helper mismatch in {path.name}")
    helper_start = upgraded.index(LEGACY_PATCH_MARKER)
    dispatcher_start = upgraded.index(DISPATCHER_MARKER, helper_start)
    upgraded = (
        upgraded[:helper_start]
        + _helper_source(newline)
        + upgraded[dispatcher_start:]
    )
    return _patch_no_visible_reply_fallback(upgraded, path)


def patch_openclaw_feishu_api_receipt(openclaw_root: Path) -> dict[str, object]:
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
        description="Patch OpenClaw Feishu replies to record API receipts directly."
    )
    parser.add_argument("--openclaw-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = patch_openclaw_feishu_api_receipt(args.openclaw_root)
    except (OSError, PatchError) as exc:
        parser.exit(2, f"OpenClaw Feishu receipt patch failed: {exc}\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
