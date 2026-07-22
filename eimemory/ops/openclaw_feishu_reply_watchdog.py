#!/usr/bin/env python3
"""Retry Feishu direct replies that lack a platform delivery receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from eimemory.ops.feishu_delivery_state import (
    complete_delivery,
    escalate_delivery,
    prepare_delivery,
    prune_delivery_entries,
    read_delivery_entries,
    reconcile_delivery,
)


DEFAULT_STATE_PATH = Path("/var/lib/eimemory/openclaw_reply_delivery_state.json")
DEFAULT_ATTEMPTS_PATH = Path("/var/lib/eimemory/openclaw_reply_delivery_attempts.json")
DEFAULT_DELIVERY_TIMEOUT_MS = 5_000
DEFAULT_STALLED_TIMEOUT_MS = 300_000
DEFAULT_INTERVAL_SECONDS = 10
DEFAULT_RAPID_ATTEMPTS = 3
DEFAULT_BACKOFF_MS = 300_000
DEFAULT_ESCALATION_TIMEOUT_MS = 10_800_000
MAX_ATTEMPT_ENTRIES = 2_000
STALLED_NOTICE = "这条消息处理链路异常，系统正在恢复。无需重复发送；恢复后会继续处理。"
GATEWAY_AUTH_ENV_NAMES = ("OPENCLAW_GATEWAY_TOKEN", "OPENCLAW_GATEWAY_PASSWORD")


def _delivery_idempotency_key(inbound_id: str, delivery_kind: str) -> str:
    digest = hashlib.sha256(inbound_id.encode("utf-8")).hexdigest()[:32]
    return f"ei-{delivery_kind}-{digest}"


def _read_json(path: Path, default: dict) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default
    return payload if isinstance(payload, dict) else default


def _parse_command_result(stdout: str) -> dict:
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "sender returned invalid JSON"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "sender returned a non-object payload"}
    message_id = payload.get("messageId") or payload.get("message_id")
    if not message_id and isinstance(payload.get("data"), dict):
        message_id = payload["data"].get("messageId") or payload["data"].get("message_id")
    if not message_id and isinstance(payload.get("receipt"), dict):
        message_id = payload["receipt"].get("primaryPlatformMessageId")
    message_id = str(message_id or "").strip()
    if payload.get("ok") is True and not message_id:
        return {**payload, "ok": False, "messageId": "", "error": "sender returned success without messageId"}
    if message_id and payload.get("ok") is not False:
        return {**payload, "ok": True, "messageId": message_id}
    return {**payload, "messageId": message_id}


def _canonical_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _gateway_main_pid() -> int:
    try:
        result = subprocess.run(
            [
                "systemctl", "--user", "show", "openclaw-gateway.service",
                "--property", "MainPID", "--value",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def _read_process_environment(pid: int) -> dict[str, str]:
    if pid <= 0:
        return {}
    try:
        values = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return {}
    environment: dict[str, str] = {}
    for value in values:
        if b"=" not in value:
            continue
        name, raw = value.split(b"=", 1)
        environment[name.decode("utf-8", errors="ignore")] = raw.decode(
            "utf-8", errors="ignore"
        )
    return environment


def _openclaw_command_env() -> dict[str, str]:
    environment = os.environ.copy()
    if any(environment.get(name) for name in GATEWAY_AUTH_ENV_NAMES):
        return environment
    gateway_environment = _read_process_environment(_gateway_main_pid())
    for name in GATEWAY_AUTH_ENV_NAMES:
        if gateway_environment.get(name):
            environment[name] = gateway_environment[name]
    return environment


@contextmanager
def _openclaw_command_environment():
    environment = _openclaw_command_env()
    config_path = Path(
        environment.get("OPENCLAW_CONFIG_PATH")
        or Path.home() / ".openclaw" / "openclaw.json"
    )
    temporary_path: Path | None = None
    try:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = None
        auth = config.get("gateway", {}).get("auth", {}) if isinstance(config, dict) else {}
        stripped_ref = False
        for config_key, environment_key in (
            ("token", "OPENCLAW_GATEWAY_TOKEN"),
            ("password", "OPENCLAW_GATEWAY_PASSWORD"),
        ):
            if isinstance(auth.get(config_key), dict) and environment.get(environment_key):
                auth.pop(config_key, None)
                stripped_ref = True
        if stripped_ref:
            fd, temp_name = tempfile.mkstemp(
                prefix="openclaw-watchdog-config-", suffix=".json"
            )
            temporary_path = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(config, handle, ensure_ascii=False)
                handle.write("\n")
            os.chmod(temporary_path, 0o600)
            environment["OPENCLAW_CONFIG_PATH"] = str(temporary_path)
        yield environment
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _message_body_text(item: dict) -> str:
    body = item.get("body") if isinstance(item.get("body"), dict) else {}
    try:
        content = json.loads(body.get("content") or "{}")
    except json.JSONDecodeError:
        return ""
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    blocks = content.get("content_v2") or content.get("content") if isinstance(content, dict) else []
    text_parts: list[str] = []
    for row in blocks if isinstance(blocks, list) else []:
        for block in row if isinstance(row, list) else []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
    return "\n".join(text_parts)


def find_existing_reply(payload: dict) -> dict:
    conversation_id = str(
        payload.get("feishu_chat_id")
        or payload.get("chat_id")
        or payload.get("conversation_id")
        or ""
    ).strip()
    inbound_id = str(payload.get("inbound_message_id") or "").strip()
    expected_text = _canonical_text(payload.get("text"))
    received_at_ms = int(payload.get("received_at_ms") or 0)
    if not inbound_id.startswith("om_") or not expected_text:
        return {"status": "error", "error": "missing reply correlation fields"}
    # OpenClaw DM contexts often carry user:<open_id>. The Feishu message list API
    # needs an oc_* chat container; without it we cannot safely prove absence.
    if not conversation_id.startswith("oc_"):
        return {"status": "not_found"}
    params = {
        "container_id_type": "chat",
        "container_id": conversation_id,
        "sort_type": "ByCreateTimeDesc",
        "page_size": 50,
        "start_time": str(max(0, received_at_ms // 1000)),
    }
    seen_tokens: set[str] = set()
    while True:
        result = subprocess.run(
            [
                "lark-cli", "api", "GET", "/open-apis/im/v1/messages",
                "--params", json.dumps(params, ensure_ascii=False, separators=(",", ":")),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return {"status": "error", "error": (result.stderr or result.stdout).strip()}
        try:
            response = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return {"status": "error", "error": "reply query returned invalid JSON"}
        if not isinstance(response, dict) or response.get("code") != 0:
            return {"status": "error", "error": str(response.get("msg") or "reply query failed")}
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        parent_hit = ""
        same_text_hit = ""
        for item in items:
            if not isinstance(item, dict):
                continue
            sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
            if sender.get("sender_type") not in {"app", "bot"}:
                continue
            if _canonical_text(_message_body_text(item)) != expected_text:
                continue
            message_id = str(item.get("message_id") or "").strip()
            if not message_id:
                continue
            # Prefer threaded/parent-linked receipts, but gateway automatic finals
            # often land without parent_id. Same-text bot replies after the inbound
            # timestamp still prove delivery and must block a second send.
            if item.get("parent_id") == inbound_id:
                parent_hit = message_id
                break
            if not same_text_hit:
                same_text_hit = message_id
        if parent_hit:
            return {"status": "found", "messageId": parent_hit}
        if same_text_hit:
            return {"status": "found", "messageId": same_text_hit}
        page_token = str(data.get("page_token") or "").strip()
        if data.get("has_more") is not True or not page_token:
            return {"status": "not_found"}
        if page_token in seen_tokens:
            return {"status": "error", "error": "reply query pagination loop"}
        seen_tokens.add(page_token)
        params["page_token"] = page_token


def send_payload(payload: dict) -> dict:
    text = str(payload.get("text") or "").strip()
    inbound_message_id = str(payload.get("inbound_message_id") or "").strip()
    if not inbound_message_id.startswith("om_"):
        return {"ok": False, "error": "missing Feishu inbound message target"}
    target = str(payload.get("sender_id") or payload.get("conversation_id") or "").strip()
    if target.startswith("user:"):
        target = target.removeprefix("user:")
    if not target:
        return {"ok": False, "error": "missing Feishu reply recipient"}
    command = [
        "openclaw", "message", "send",
        "--channel", "feishu",
        "--account", "default",
        "--target", target,
        "--reply-to", inbound_message_id,
        "--message", text,
        "--json",
    ]
    with _openclaw_command_environment() as command_environment:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=command_environment,
            timeout=30,
            check=False,
        )
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout).strip()}
    return _parse_command_result(result.stdout)


def scan_once(
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    attempts_path: Path = DEFAULT_ATTEMPTS_PATH,
    now_ms: int | None = None,
    delivery_timeout_ms: int = DEFAULT_DELIVERY_TIMEOUT_MS,
    stalled_timeout_ms: int = DEFAULT_STALLED_TIMEOUT_MS,
    rapid_attempts: int = DEFAULT_RAPID_ATTEMPTS,
    backoff_ms: int = DEFAULT_BACKOFF_MS,
    escalation_timeout_ms: int = DEFAULT_ESCALATION_TIMEOUT_MS,
    send: Callable[[dict], dict] = send_payload,
    find_existing: Callable[[dict], dict] = find_existing_reply,
) -> dict:
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    state = _read_json(state_path, {"entries": {}})
    entries = state.get("entries") if isinstance(state.get("entries"), dict) else {}
    try:
        attempt_entries = read_delivery_entries(attempts_path)
    except (OSError, ValueError):
        return {
            "checked": len(entries),
            "retried": 0,
            "failed": 1,
            "persistence_failed": 1,
        }
    retried = 0
    failed = 0
    persistence_failed = 0
    escalated = 0

    for inbound_id, raw_entry in entries.items():
        if not isinstance(raw_entry, dict) or raw_entry.get("status") in {
            "delivered",
            "platform_accepted",
            "silent",
            "escalated",
        }:
            continue
        conversation_id = str(raw_entry.get("conversation_id") or "").strip()
        sender_id = str(raw_entry.get("sender_id") or "").strip()
        if not str(inbound_id).startswith("om_") or not (
            conversation_id or sender_id
        ):
            continue
        status = str(raw_entry.get("status") or "")
        final_text = str(raw_entry.get("final_text") or "").strip()
        # If the bridge already recorded a successful gateway outbound, close the
        # receipt without another external send.
        existing_receipt = str(
            raw_entry.get("delivery_message_id")
            or raw_entry.get("last_sent_message_id")
            or ""
        ).strip()
        if (
            status in {"answered", "final_ready"}
            and final_text
            and existing_receipt
            and raw_entry.get("last_sent_success") is True
            and _canonical_text(raw_entry.get("last_sent_content")) == _canonical_text(final_text)
        ):
            try:
                decision = prepare_delivery(
                    attempts_path,
                    key=str(inbound_id),
                    delivery_kind="final",
                    idempotency_key=_delivery_idempotency_key(str(inbound_id), "final"),
                    payload_digest=hashlib.sha256(
                        json.dumps(
                            {
                                "inbound_message_id": inbound_id,
                                "text": final_text,
                                "delivery_kind": "final",
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ).encode("utf-8")
                    ).hexdigest(),
                    now_ms=now_ms,
                )
                if decision.get("send") is True or str(
                    (decision.get("entry") or {}).get("state") or ""
                ) in {"sending", "delivery_uncertain"}:
                    recorded = complete_delivery(
                        attempts_path,
                        key=str(inbound_id),
                        ok=True,
                        message_id=existing_receipt,
                        error="",
                        now_ms=now_ms,
                    )
                    attempt_entries[str(inbound_id)] = recorded
                    retried += 1
            except (OSError, ValueError, KeyError):
                failed += 1
                persistence_failed += 1
            continue
        if status in {"answered", "final_ready"} and final_text:
            due_at = int(raw_entry.get("agent_end_at_ms") or raw_entry.get("received_at_ms") or 0)
            if now_ms - due_at < delivery_timeout_ms:
                continue
            text = final_text
        elif status == "pending" and raw_entry.get("suppress_stalled_notice") is not True:
            due_at = max(
                int(raw_entry.get("received_at_ms") or 0),
                int(raw_entry.get("last_progress_at_ms") or 0),
            )
            if now_ms - due_at < stalled_timeout_ms:
                continue
            text = STALLED_NOTICE
        else:
            continue

        delivery_kind = "final" if status == "answered" else "status"
        if status == "final_ready":
            delivery_kind = "final"
        attempt_key = inbound_id if delivery_kind == "final" else f"status:{inbound_id}"
        previous_attempt = attempt_entries.get(attempt_key)
        payload = {
            "conversation_id": conversation_id,
            "feishu_chat_id": str(raw_entry.get("feishu_chat_id") or conversation_id),
            "sender_id": sender_id,
            "text": text,
            "idempotency_key": _delivery_idempotency_key(inbound_id, delivery_kind),
            "inbound_message_id": inbound_id,
            "received_at_ms": int(raw_entry.get("received_at_ms") or 0),
        }
        previous_state = (
            str(previous_attempt.get("state") or "")
            if isinstance(previous_attempt, dict)
            else ""
        )
        if previous_state == "status_notified":
            has_resume_reference = any(
                str(raw_entry.get(name) or "").strip()
                for name in (
                    "resume_reference",
                    "resume_ref",
                    "resume_task_id",
                    "continuation_reference",
                )
            )
            pending_since_ms = max(
                int(raw_entry.get("received_at_ms") or 0),
                int(raw_entry.get("last_progress_at_ms") or 0),
            )
            if delivery_kind == "status" and now_ms - pending_since_ms >= escalation_timeout_ms:
                try:
                    escalate_delivery(
                        attempts_path,
                        key=attempt_key,
                        now_ms=now_ms,
                        reason=(
                            "pending_after_resume_reference"
                            if has_resume_reference
                            else "pending_without_resume_reference"
                        ),
                    )
                    escalated += 1
                    failed += 1
                except (OSError, ValueError, KeyError):
                    failed += 1
                    persistence_failed += 1
            continue
        if previous_state in {"platform_accepted", "escalated"}:
            continue
        if previous_state in {"sending", "delivery_uncertain"}:
            try:
                existing = find_existing(payload)
            except Exception as error:  # pragma: no cover - service boundary
                existing = {"status": "error", "error": str(error)}
            if existing.get("status") == "error":
                failed += 1
                continue
            try:
                reconciled = reconcile_delivery(
                    attempts_path,
                    key=attempt_key,
                    found_message_id=(
                        str(existing.get("messageId") or "")
                        if existing.get("status") == "found"
                        else ""
                    ),
                    now_ms=now_ms,
                    uncertainty_after_ms=delivery_timeout_ms,
                    escalation_after_ms=escalation_timeout_ms,
                )
                attempt_entries[attempt_key] = reconciled
                is_escalated = reconciled.get("state") == "escalated"
                is_unresolved = reconciled.get("state") in {
                    "sending",
                    "delivery_uncertain",
                    "escalated",
                }
                escalated += int(is_escalated)
                failed += int(is_unresolved)
            except (OSError, ValueError, KeyError):
                failed += 1
                persistence_failed += 1
            continue
        try:
            existing = find_existing(payload)
            if existing.get("status") == "found":
                try:
                    decision = prepare_delivery(
                        attempts_path,
                        key=attempt_key,
                        delivery_kind=delivery_kind,
                        idempotency_key=payload["idempotency_key"],
                        payload_digest=hashlib.sha256(
                            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                        ).hexdigest(),
                        now_ms=now_ms,
                    )
                    if decision.get("send") is True:
                        recorded = complete_delivery(
                            attempts_path,
                            key=attempt_key,
                            ok=True,
                            message_id=str(existing.get("messageId") or ""),
                            error="",
                            now_ms=now_ms,
                        )
                        attempt_entries[attempt_key] = recorded
                        retried += 1
                    continue
                except (OSError, ValueError):
                    failed += 1
                    persistence_failed += 1
                    continue
            elif existing.get("status") == "not_found":
                pass
            else:
                failed += 1
                continue
        except Exception as error:  # pragma: no cover - defensive service boundary
            failed += 1
            continue
        try:
            decision = prepare_delivery(
                attempts_path,
                key=attempt_key,
                delivery_kind=delivery_kind,
                idempotency_key=payload["idempotency_key"],
                payload_digest=hashlib.sha256(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                now_ms=now_ms,
            )
        except (OSError, ValueError):
            failed += 1
            persistence_failed += 1
            continue
        if decision.get("send") is not True:
            attempt_entries[attempt_key] = dict(decision.get("entry") or {})
            continue
        try:
            result = send(payload)
        except Exception as error:  # pragma: no cover - defensive service boundary
            result = {"ok": False, "error": str(error)}
        message_id = str(result.get("messageId") or result.get("message_id") or "").strip()
        ok = result.get("ok") is True and bool(message_id)
        try:
            recorded = complete_delivery(
                attempts_path,
                key=attempt_key,
                ok=ok,
                message_id=message_id,
                error=str(
                    result.get("error")
                    or (
                        "sender returned success without messageId"
                        if result.get("ok") is True
                        else ""
                    )
                ),
                now_ms=now_ms,
            )
            attempt_entries[attempt_key] = recorded
        except (OSError, ValueError):
            failed += 1
            persistence_failed += 1
            continue
        retried += int(ok)
        failed += int(not ok)

    protected_attempt_keys: set[str] = set()
    for inbound_id, raw_entry in entries.items():
        if not isinstance(raw_entry, dict) or raw_entry.get("status") in {
            "delivered",
            "platform_accepted",
            "silent",
            "escalated",
        }:
            continue
        protected_attempt_keys.add(str(inbound_id))
        protected_attempt_keys.add(f"status:{inbound_id}")
    try:
        prune_report = prune_delivery_entries(
            attempts_path,
            protected_keys=protected_attempt_keys,
            max_terminal_entries=MAX_ATTEMPT_ENTRIES,
        )
    except (OSError, ValueError):
        failed += 1
        persistence_failed += 1
        prune_report = {"pruned": 0}

    summary = {"checked": len(entries), "retried": retried, "failed": failed}
    if persistence_failed:
        summary["persistence_failed"] = persistence_failed
    if escalated:
        summary["escalated"] = escalated
    if int(prune_report.get("pruned") or 0):
        summary["pruned_attempt_entries"] = int(prune_report["pruned"])
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--attempts", type=Path, default=DEFAULT_ATTEMPTS_PATH)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    args = parser.parse_args()
    while True:
        result = scan_once(state_path=args.state, attempts_path=args.attempts)
        if result["retried"] or result["failed"]:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.once:
            return 1 if result["failed"] else 0
        time.sleep(max(1, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
