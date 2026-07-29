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
PATCH_VERSION_MARKER = "// eimemory-feishu-api-receipt-patch:v4"
LEGACY_API_V3_PATCH_VERSION_MARKER = "// eimemory-feishu-api-receipt-patch:v3"
LEGACY_API_V2_PATCH_VERSION_MARKER = "// eimemory-feishu-api-receipt-patch:v2"
LEGACY_API_V1_PATCH_VERSION_MARKER = "// eimemory-feishu-api-receipt-patch:v1"
LEGACY_V3_PATCH_VERSION_MARKER = "// eimemory-feishu-message-sent-patch:v3"
LEGACY_V2_PATCH_VERSION_MARKER = "// eimemory-feishu-message-sent-patch:v2"
DISPATCHER_MARKER = "function createFeishuReplyDispatcher(params) {"
DISPATCHER_END_MARKER = "\n//#endregion"
API_RESULT_PATCH_VERSION_MARKER = (
    "// eimemory-feishu-api-result-receipt-patch:v1"
)
API_RESULT_FUNCTION_MARKER = (
    "function toFeishuSendResult(response, chatId, kind) {"
)
API_RESULT_MESSAGE_ID_MARKER = (
    'const messageId = response.data?.message_id ?? "unknown";'
)


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
        "function persistEimemoryFeishuApiAccepted(params) {",
        '\tconst fsApi = process.getBuiltinModule("node:fs");',
        '\tconst pathApi = process.getBuiltinModule("node:path");',
        '\tconst cryptoApi = process.getBuiltinModule("node:crypto");',
        "\tconst spoolDir = String(",
        "\t\tprocess.env.EIMEMORY_FEISHU_API_RECEIPT_SPOOL_DIR",
        '\t\t|| "/var/lib/eimemory/feishu-api-receipts"',
        "\t).trim();",
        '\tif (!pathApi.isAbsolute(spoolDir)) throw new Error("receipt spool path must be absolute");',
        "\tfsApi.mkdirSync(spoolDir, { recursive: true, mode: 0o700 });",
        "\tconst spoolStat = fsApi.lstatSync(spoolDir);",
        "\tif (!spoolStat.isDirectory() || spoolStat.isSymbolicLink()) {",
        '\t\tthrow new Error("receipt spool must be a real directory");',
        "\t}",
        "\tconst acceptedAtMs = Date.now();",
        "\tconst token = `${acceptedAtMs}-${process.pid}-${cryptoApi.randomUUID()}`;",
        "\tconst finalPath = pathApi.join(spoolDir, `${token}.json`);",
        "\tconst temporaryPath = pathApi.join(spoolDir, `.${token}.tmp`);",
        "\tconst payload = {",
        '\t\tschema_version: "eimemory.feishu_api_receipt.v1",',
        "\t\tto: params.to,",
        "\t\tcontent: params.content,",
        "\t\tsuccess: true,",
        "\t\taccountId: params.accountId,",
        "\t\tconversationId: params.conversationId,",
        "\t\tsessionKey: params.sessionKey,",
        "\t\tmessageId: params.messageId,",
        "\t\tacceptedAtMs,",
        '\t\truntimeCommit: String(process.env.EIMEMORY_RUNTIME_COMMIT || "")',
        "\t};",
        "\ttry {",
        "\t\tfsApi.writeFileSync(temporaryPath, `${JSON.stringify(payload)}\\n`, {",
        '\t\t\tencoding: "utf8", mode: 0o600, flag: "wx"',
        "\t\t});",
        "\t\tfsApi.renameSync(temporaryPath, finalPath);",
        "\t} catch (error) {",
        "\t\ttry { fsApi.unlinkSync(temporaryPath); } catch {}",
        "\t\tthrow error;",
        "\t}",
        "\treturn acceptedAtMs;",
        "}",
        "async function emitEimemoryFeishuApiAccepted(params) {",
        '\tconst messageId = String(params.messageId || "").trim();',
        "\tif (!messageId) return;",
        "\ttry {",
        "\t\tpersistEimemoryFeishuApiAccepted({ ...params, messageId });",
        "\t} catch (error) {",
        "\t\tparams.log?.(`eimemory Feishu API receipt spool failed: ${String(error)}`);",
        "\t}",
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


def _api_result_helper_source(newline: str) -> str:
    lines = [
        API_RESULT_PATCH_VERSION_MARKER,
        "function extractEimemoryFeishuApiResultContent(response) {",
        "\tconst raw = response?.data?.body?.content ?? response?.data?.content;",
        "\tif (raw === undefined || raw === null) return \"\";",
        "\tlet decoded = raw;",
        "\tif (typeof raw === \"string\") {",
        "\t\ttry { decoded = JSON.parse(raw); } catch { return raw.trim(); }",
        "\t}",
        "\tconst fragments = [];",
        "\tconst visit = (value) => {",
        "\t\tif (typeof value === \"string\") {",
        "\t\t\tif (value.trim()) fragments.push(value);",
        "\t\t\treturn;",
        "\t\t}",
        "\t\tif (Array.isArray(value)) {",
        "\t\t\tfor (const item of value) visit(item);",
        "\t\t\treturn;",
        "\t\t}",
        "\t\tif (!value || typeof value !== \"object\") return;",
        "\t\tif (typeof value.text === \"string\") {",
        "\t\t\tvisit(value.text);",
        "\t\t\treturn;",
        "\t\t}",
        "\t\tfor (const key of [\"zh_cn\", \"en_us\", \"content\", \"content_v2\", \"elements\"]) {",
        "\t\t\tif (value[key] !== undefined) visit(value[key]);",
        "\t\t}",
        "\t};",
        "\tvisit(decoded);",
        "\treturn fragments.filter((value, index) => (",
        "\t\tindex === 0 || value !== fragments[index - 1]",
        "\t)).join(\"\\n\").trim();",
        "}",
        "function persistEimemoryFeishuApiResult(response, chatId, kind, messageId) {",
        "\tif (kind !== \"text\") return;",
        "\tconst normalizedMessageId = String(messageId || \"\").trim();",
        "\tif (!normalizedMessageId || normalizedMessageId === \"unknown\") return;",
        "\tconst content = extractEimemoryFeishuApiResultContent(response);",
        "\tconst conversationId = String(response?.data?.chat_id || chatId || \"\").trim();",
        "\tif (!content || !conversationId.startsWith(\"oc_\")) return;",
        '\tconst fsApi = process.getBuiltinModule("node:fs");',
        '\tconst pathApi = process.getBuiltinModule("node:path");',
        '\tconst cryptoApi = process.getBuiltinModule("node:crypto");',
        "\tconst spoolDir = String(",
        "\t\tprocess.env.EIMEMORY_FEISHU_API_RECEIPT_SPOOL_DIR",
        '\t\t|| "/var/lib/eimemory/feishu-api-receipts"',
        "\t).trim();",
        '\tif (!pathApi.isAbsolute(spoolDir)) throw new Error("receipt spool path must be absolute");',
        "\tfsApi.mkdirSync(spoolDir, { recursive: true, mode: 0o700 });",
        "\tconst spoolStat = fsApi.lstatSync(spoolDir);",
        "\tif (!spoolStat.isDirectory() || spoolStat.isSymbolicLink()) {",
        '\t\tthrow new Error("receipt spool must be a real directory");',
        "\t}",
        "\tconst acceptedAtMs = Date.now();",
        "\tconst token = `${acceptedAtMs}-${process.pid}-${cryptoApi.randomUUID()}`;",
        "\tconst finalPath = pathApi.join(spoolDir, `${token}.json`);",
        "\tconst temporaryPath = pathApi.join(spoolDir, `.${token}.tmp`);",
        "\tconst payload = {",
        '\t\tschema_version: "eimemory.feishu_api_receipt.v1",',
        "\t\tcontent,",
        "\t\tsuccess: true,",
        "\t\tmessageId: normalizedMessageId,",
        "\t\tconversationId,",
        "\t\tacceptedAtMs,",
        '\t\truntimeCommit: String(process.env.EIMEMORY_RUNTIME_COMMIT || ""),',
        '\t\tsource: "api_result"',
        "\t};",
        "\ttry {",
        "\t\tfsApi.writeFileSync(temporaryPath, `${JSON.stringify(payload)}\\n`, {",
        '\t\t\tencoding: "utf8", mode: 0o600, flag: "wx"',
        "\t\t});",
        "\t\tfsApi.renameSync(temporaryPath, finalPath);",
        "\t} catch (error) {",
        "\t\ttry { fsApi.unlinkSync(temporaryPath); } catch {}",
        "\t\tthrow error;",
        "\t}",
        "}",
    ]
    return newline.join(lines) + newline


def _patch_api_result(text: str, path: Path) -> tuple[str, bool]:
    if API_RESULT_PATCH_VERSION_MARKER in text:
        if text.count(API_RESULT_PATCH_VERSION_MARKER) != 1:
            raise PatchError(
                f"current Feishu API result receipt marker mismatch in {path.name}"
            )
        if text.count("persistEimemoryFeishuApiResult(") != 2:
            raise PatchError(
                f"current Feishu API result receipt call is missing in {path.name}"
            )
        return text, False
    if text.count(API_RESULT_FUNCTION_MARKER) != 1:
        raise PatchError(f"expected one Feishu send result function in {path.name}")
    if text.count(API_RESULT_MESSAGE_ID_MARKER) != 1:
        raise PatchError(f"expected one Feishu result message id in {path.name}")
    newline = "\r\n" if "\r\n" in text else "\n"
    function_start = text.index(API_RESULT_FUNCTION_MARKER)
    patched = (
        text[:function_start]
        + _api_result_helper_source(newline)
        + text[function_start:]
    )
    call = newline.join(
        [
            API_RESULT_MESSAGE_ID_MARKER,
            "\ttry {",
            "\t\tpersistEimemoryFeishuApiResult(response, chatId, kind, messageId);",
            "\t} catch (error) {",
            "\t\tconsole.warn(`eimemory Feishu API result receipt spool failed: ${String(error)}`);",
            "\t}",
        ]
    )
    return patched.replace(API_RESULT_MESSAGE_ID_MARKER, call, 1), True


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
        marker_count = text.count(PATCH_VERSION_MARKER)
        if marker_count == 2:
            newline = "\r\n" if "\r\n" in text else "\n"
            duplicated = PATCH_VERSION_MARKER + newline + PATCH_VERSION_MARKER
            if text.count(duplicated) != 1:
                raise PatchError(f"current Feishu API receipt markers are malformed in {path.name}")
            repaired = text.replace(duplicated, PATCH_VERSION_MARKER, 1)
            return _patch_dispatcher(repaired, path)[0], True
        if marker_count != 1:
            raise PatchError(f"current Feishu API receipt marker mismatch in {path.name}")
        if "emitEimemoryFeishuMessageSent(" in text:
            raise PatchError(f"current Feishu API receipt patch calls legacy sink in {path.name}")
        if text.count("emitEimemoryFeishuApiAccepted(") < 2:
            raise PatchError(f"current Feishu API receipt sink call is missing in {path.name}")
        return text, False
    if LEGACY_API_V3_PATCH_VERSION_MARKER in text:
        return _upgrade_api_patch(
            text,
            path,
            marker=LEGACY_API_V3_PATCH_VERSION_MARKER,
        ), True
    if LEGACY_API_V2_PATCH_VERSION_MARKER in text:
        return _upgrade_api_patch(
            text,
            path,
            marker=LEGACY_API_V2_PATCH_VERSION_MARKER,
        ), True
    if LEGACY_API_V1_PATCH_VERSION_MARKER in text:
        return _upgrade_api_patch(
            text,
            path,
            marker=LEGACY_API_V1_PATCH_VERSION_MARKER,
        ), True
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

    final_anchor = re.compile(r'(?P<indent>[ \t]+)if \(paramsLocal\.infoKind === "final"\)')
    final_matches = list(final_anchor.finditer(region))
    if len(final_matches) != 1:
        raise PatchError(f"expected one final chunk receipt anchor in {path.name}")
    final_match = final_matches[0]
    final_indent = final_match.group("indent")
    region = (
        region[: final_match.start()]
        + f"{final_indent}await emitRememberedEimemoryFeishuReceipt("
        f"paramsLocal.text);{newline}"
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


def _upgrade_api_receipt_emission(text: str, path: Path) -> str:
    conditional = (
        'if (paramsLocal.infoKind === "final") '
        "await emitRememberedEimemoryFeishuReceipt(paramsLocal.text);"
    )
    unconditional = "await emitRememberedEimemoryFeishuReceipt(paramsLocal.text);"
    conditional_count = text.count(conditional)
    unconditional_count = text.count(unconditional)
    if conditional_count == 1:
        return text.replace(conditional, unconditional, 1)
    if conditional_count == 0 and unconditional_count == 1:
        return text
    raise PatchError(f"legacy Feishu API receipt emission mismatch in {path.name}")


def _upgrade_api_patch(text: str, path: Path, *, marker: str) -> str:
    if text.count(marker) != 1:
        raise PatchError(f"legacy Feishu API receipt marker mismatch in {path.name}")
    upgraded = text.replace(
        marker,
        PATCH_VERSION_MARKER,
        1,
    )
    legacy_call = "emitEimemoryFeishuMessageSent("
    legacy_call_count = upgraded.count(legacy_call)
    if legacy_call_count > 1:
        raise PatchError(f"legacy Feishu API receipt call mismatch in {path.name}")
    if legacy_call_count == 1:
        upgraded = upgraded.replace(
            legacy_call,
            "emitEimemoryFeishuApiAccepted(",
            1,
        )
    if upgraded.count("emitEimemoryFeishuApiAccepted(") < 2:
        raise PatchError(f"Feishu API receipt sink call is missing in {path.name}")
    upgraded = _upgrade_api_receipt_emission(upgraded, path)
    newline = "\r\n" if "\r\n" in upgraded else "\n"
    marker_start = upgraded.index(PATCH_VERSION_MARKER)
    dispatcher_start = upgraded.index(DISPATCHER_MARKER, marker_start)
    return (
        upgraded[:marker_start]
        + _helper_source(newline)
        + upgraded[dispatcher_start:]
    )


def _replace_legacy_sink_call(text: str, path: Path) -> str:
    legacy_call = "emitEimemoryFeishuMessageSent("
    if text.count(legacy_call) != 1:
        raise PatchError(f"legacy Feishu receipt sink call mismatch in {path.name}")
    return text.replace(
        legacy_call,
        "emitEimemoryFeishuApiAccepted(",
        1,
    )


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
        upgraded = _replace_legacy_sink_call(upgraded, path)
        upgraded = _upgrade_api_receipt_emission(upgraded, path)
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
    upgraded = _replace_legacy_sink_call(upgraded, path)
    upgraded = _upgrade_api_receipt_emission(upgraded, path)
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
    patched, dispatcher_changed = _patch_dispatcher(text, path)
    result_candidates = [
        candidate
        for candidate in sorted(dist.glob("send-result-*.js"))
        if API_RESULT_FUNCTION_MARKER in candidate.read_text(encoding="utf-8")
    ]
    if len(result_candidates) != 1:
        raise PatchError("expected exactly one Feishu send result runtime")
    result_path = result_candidates[0]
    if result_path.is_symlink() or not result_path.is_file():
        raise PatchError("Feishu send result runtime must be a regular file")
    result_text = result_path.read_text(encoding="utf-8")
    patched_result, result_changed = _patch_api_result(result_text, result_path)
    if dispatcher_changed:
        _atomic_write(path, patched)
    if result_changed:
        _atomic_write(result_path, patched_result)
    changed = dispatcher_changed or result_changed
    return {
        "ok": True,
        "status": "patched" if changed else "already_patched",
        "version": version,
        "runtime": path.name,
        "send_result_runtime": result_path.name,
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
