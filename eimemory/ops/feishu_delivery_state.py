"""Durable at-most-once state for Feishu reply delivery.

The caller must persist ``sending`` before invoking an external sender.  Once an
intent exists, this module never makes it sendable again: recovery is limited to
receipt reconciliation and escalation.  This trades automatic blind retries for
the stronger invariant that a process crash cannot create duplicate replies.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from eimemory.storage.atomic_file import locked_json_update, read_json_strict


SCHEMA_VERSION = "feishu_delivery_state.v2"
FINAL_SUCCESS_STATE = "platform_accepted"
STATUS_SUCCESS_STATE = "status_notified"
AMBIGUOUS_STATES = frozenset({"sending", "delivery_uncertain"})
NON_SENDABLE_STATES = frozenset(
    {
        "sending",
        "delivery_uncertain",
        "platform_accepted",
        "status_notified",
        "escalated",
    }
)


def _empty_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "entries": {}}


def _normalize_document(document: dict[str, Any]) -> dict[str, Any]:
    entries = document.get("entries")
    if not isinstance(entries, dict):
        raise ValueError("Feishu delivery state entries must be an object")
    document["schema_version"] = SCHEMA_VERSION
    return document


def _update(path: Path, mutate):
    def wrapped(document: dict[str, Any]) -> dict[str, Any]:
        normalized = _normalize_document(document)
        mutate(normalized)
        return normalized

    return locked_json_update(
        path,
        wrapped,
        default=_empty_state(),
        expected_type=dict,
    )


def _legacy_state(entry: dict[str, Any], delivery_kind: str) -> str:
    message_id = str(entry.get("platform_message_id") or entry.get("message_id") or "").strip()
    if message_id and not entry.get("error"):
        return STATUS_SUCCESS_STATE if delivery_kind == "status" else FINAL_SUCCESS_STATE
    # A legacy retry row means an external call may already have happened.  It
    # must not be made sendable merely because the old row lacked a v2 state.
    if entry.get("attempted_at_ms") or entry.get("retry_count"):
        return "delivery_uncertain"
    return ""


def prepare_delivery(
    path: str | Path,
    *,
    key: str,
    delivery_kind: str,
    idempotency_key: str,
    payload_digest: str,
    now_ms: int,
) -> dict[str, Any]:
    """Persist a send intent and return whether the caller may send once."""

    if delivery_kind not in {"final", "status"}:
        raise ValueError("delivery_kind must be final or status")
    decision: dict[str, Any] = {}

    def mutate(document: dict[str, Any]) -> None:
        entries = document["entries"]
        current = entries.get(key)
        if isinstance(current, dict):
            current_kind = str(current.get("delivery_kind") or delivery_kind)
            state = str(current.get("state") or _legacy_state(current, current_kind))
            if state in NON_SENDABLE_STATES:
                current["state"] = state
                current["delivery_kind"] = current_kind
                if state in {FINAL_SUCCESS_STATE, STATUS_SUCCESS_STATE}:
                    receipt = str(
                        current.get("platform_message_id")
                        or current.get("message_id")
                        or ""
                    ).strip()
                    if state == STATUS_SUCCESS_STATE:
                        current["notification_message_id"] = receipt
                        current.pop("platform_message_id", None)
                        current.pop("platform_accepted_at_ms", None)
                    else:
                        current["platform_message_id"] = receipt
                    current["message_id"] = receipt
                    current["ok"] = True
                    current["retry_mode"] = "complete"
                decision.update(send=False, state=state, entry=deepcopy(current))
                return
        attempt_count = int((current or {}).get("attempt_count") or 0) + 1
        entry = {
            **(current if isinstance(current, dict) else {}),
            "state": "sending",
            "delivery_kind": delivery_kind,
            "idempotency_key": idempotency_key,
            "payload_digest": payload_digest,
            "intent_at_ms": int(now_ms),
            "attempt_count": attempt_count,
            "error": "",
        }
        entries[key] = entry
        decision.update(send=True, state="sending", entry=deepcopy(entry))

    _update(Path(path), mutate)
    return decision


def complete_delivery(
    path: str | Path,
    *,
    key: str,
    ok: bool,
    message_id: str,
    error: str,
    now_ms: int,
) -> dict[str, Any]:
    """Record the result of the one allowed external send attempt."""

    receipt = str(message_id or "").strip()
    if ok and not receipt:
        raise ValueError("successful delivery requires a platform message receipt")
    updated: dict[str, Any] = {}

    def mutate(document: dict[str, Any]) -> None:
        entry = document["entries"].get(key)
        if not isinstance(entry, dict) or entry.get("state") != "sending":
            raise ValueError(f"delivery {key!r} has no persisted sending intent")
        kind = str(entry.get("delivery_kind") or "final")
        if ok:
            entry["state"] = STATUS_SUCCESS_STATE if kind == "status" else FINAL_SUCCESS_STATE
            if kind == "status":
                entry.pop("platform_message_id", None)
                entry.pop("platform_accepted_at_ms", None)
                entry["notification_message_id"] = receipt
                entry["status_notified_at_ms"] = int(now_ms)
            else:
                entry["platform_message_id"] = receipt
                entry["platform_accepted_at_ms"] = int(now_ms)
            entry["message_id"] = receipt
            entry["attempted_at_ms"] = int(now_ms)
            entry["ok"] = True
            entry["retry_mode"] = "complete"
            entry["error"] = ""
        else:
            entry["state"] = "delivery_uncertain"
            entry["uncertain_at_ms"] = int(now_ms)
            entry["attempted_at_ms"] = int(now_ms)
            entry["ok"] = False
            entry["retry_mode"] = "reconcile_only"
            entry["error"] = str(error or "external sender returned no platform receipt")
        updated.update(deepcopy(entry))

    _update(Path(path), mutate)
    return updated


def reconcile_delivery(
    path: str | Path,
    *,
    key: str,
    found_message_id: str,
    now_ms: int,
    uncertainty_after_ms: int,
    escalation_after_ms: int,
) -> dict[str, Any]:
    """Resolve an ambiguous intent from a platform receipt, or age it safely."""

    updated: dict[str, Any] = {}

    def mutate(document: dict[str, Any]) -> None:
        entry = document["entries"].get(key)
        if not isinstance(entry, dict):
            raise KeyError(key)
        state = str(entry.get("state") or _legacy_state(entry, str(entry.get("delivery_kind") or "final")))
        entry["state"] = state
        receipt = str(found_message_id or "").strip()
        if receipt:
            kind = str(entry.get("delivery_kind") or "final")
            entry["state"] = STATUS_SUCCESS_STATE if kind == "status" else FINAL_SUCCESS_STATE
            if kind == "status":
                entry.pop("platform_message_id", None)
                entry.pop("platform_accepted_at_ms", None)
                entry["notification_message_id"] = receipt
                entry["status_notified_at_ms"] = int(now_ms)
            else:
                entry["platform_message_id"] = receipt
                entry["platform_accepted_at_ms"] = int(now_ms)
            entry["message_id"] = receipt
            entry["attempted_at_ms"] = int(now_ms)
            entry["ok"] = True
            entry["retry_mode"] = "complete"
            entry["error"] = ""
        elif state in AMBIGUOUS_STATES:
            intent_at = int(entry.get("intent_at_ms") or entry.get("attempted_at_ms") or now_ms)
            age_ms = max(0, int(now_ms) - intent_at)
            if state == "delivery_uncertain" and age_ms >= int(escalation_after_ms):
                entry["state"] = "escalated"
                entry["escalated_at_ms"] = int(now_ms)
                entry["escalation_reason"] = "ambiguous_delivery_without_receipt"
            elif age_ms >= int(uncertainty_after_ms):
                entry["state"] = "delivery_uncertain"
                entry.setdefault("uncertain_at_ms", int(now_ms))
        updated.update(deepcopy(entry))

    _update(Path(path), mutate)
    return updated


def escalate_delivery(
    path: str | Path,
    *,
    key: str,
    now_ms: int,
    reason: str,
) -> dict[str, Any]:
    updated: dict[str, Any] = {}

    def mutate(document: dict[str, Any]) -> None:
        entry = document["entries"].get(key)
        if not isinstance(entry, dict):
            raise KeyError(key)
        if entry.get("state") != STATUS_SUCCESS_STATE:
            raise ValueError("only a status_notified delivery may be workflow-escalated")
        entry["state"] = "escalated"
        entry["escalated_at_ms"] = int(now_ms)
        entry["escalation_reason"] = str(reason)
        updated.update(deepcopy(entry))

    _update(Path(path), mutate)
    return updated


def read_delivery_entries(path: str | Path) -> dict[str, dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return {}
    document = _normalize_document(read_json_strict(target, dict))
    return {
        str(key): value
        for key, value in document["entries"].items()
        if isinstance(value, dict)
    }


class DeliveryStateStore:
    """Small deploy-neutral facade for alternate Feishu delivery workers."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def prepare_send(self, **kwargs: Any) -> dict[str, Any]:
        return prepare_delivery(self.path, **kwargs)

    def complete_send(self, **kwargs: Any) -> dict[str, Any]:
        return complete_delivery(self.path, **kwargs)

    def mark_uncertain(
        self,
        *,
        key: str,
        now_ms: int,
        uncertainty_after_ms: int = 0,
        escalation_after_ms: int = 2**63 - 1,
    ) -> dict[str, Any]:
        return reconcile_delivery(
            self.path,
            key=key,
            found_message_id="",
            now_ms=now_ms,
            uncertainty_after_ms=uncertainty_after_ms,
            escalation_after_ms=escalation_after_ms,
        )

    def escalate(self, **kwargs: Any) -> dict[str, Any]:
        return escalate_delivery(self.path, **kwargs)

    def get(self, key: str) -> dict[str, Any] | None:
        entry = read_delivery_entries(self.path).get(key)
        return deepcopy(entry) if entry is not None else None

    def list_overdue_nonterminal(
        self,
        *,
        now_ms: int,
        sla_ms: int,
    ) -> list[dict[str, Any]]:
        terminal = {FINAL_SUCCESS_STATE, "escalated"}
        overdue = []
        for key, entry in read_delivery_entries(self.path).items():
            reference_ms = int(
                entry.get("intent_at_ms")
                or entry.get("status_notified_at_ms")
                or entry.get("attempted_at_ms")
                or now_ms
            )
            if entry.get("state") not in terminal and now_ms - reference_ms >= sla_ms:
                overdue.append({"key": key, **deepcopy(entry)})
        return overdue
