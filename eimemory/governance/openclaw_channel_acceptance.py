from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Mapping

from eimemory.governance.evidence_contract import (
    ReleaseIdentity,
    verified_deployment_receipt_identity,
)
from eimemory.governance.learning_state import (
    append_learning_record_once,
    stable_semantic_key,
)
from eimemory.models.records import ScopeRef
from eimemory.storage.atomic_file import read_json_strict


SCHEMA_VERSION = "openclaw_channel_acceptance.v1"
SOURCE = "eimemory.openclaw.channel_acceptance"
REPORT_TYPE = "openclaw_channel_acceptance"
EVIDENCE_CLASS = "external_channel_receipt"
DELIVERY_STATE_SCHEMA = "openclaw_reply_delivery.v2"
DEFAULT_DELIVERY_STATE_PATH = Path(
    "/var/lib/eimemory/openclaw_reply_delivery_state.json"
)
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


def record_openclaw_channel_acceptance(
    runtime: Any,
    *,
    scope: ScopeRef | dict | None,
    current_release: ReleaseIdentity,
    state_path: str | Path = DEFAULT_DELIVERY_STATE_PATH,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    receipt = runtime.store.get_by_id(current_release.receipt_id, scope=scope_ref)
    if verified_deployment_receipt_identity(receipt) != current_release:
        return {"ok": False, "error": "current_deployment_receipt_invalid"}
    receipt_recorded_at_ms = _iso_timestamp_ms(
        getattr(getattr(receipt, "time", None), "created_at", "")
    )
    if receipt_recorded_at_ms <= 0:
        return {"ok": False, "error": "current_deployment_receipt_timestamp_invalid"}

    if not Path(state_path).exists():
        return {"ok": False, "error": "channel_delivery_state_missing"}
    try:
        document = read_json_strict(Path(state_path), dict)
    except (OSError, ValueError):
        return {"ok": False, "error": "channel_delivery_state_invalid"}
    if (
        str(document.get("schema_version") or "") != DELIVERY_STATE_SCHEMA
        or not isinstance(document.get("entries"), dict)
    ):
        return {"ok": False, "error": "channel_delivery_state_contract_invalid"}

    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for inbound_id, raw_entry in document["entries"].items():
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        accepted_at = _positive_int(entry.get("platform_accepted_at_ms"))
        received_at = _positive_int(entry.get("received_at_ms"))
        delivery_id = str(entry.get("delivery_message_id") or "").strip()
        runtime_commit = str(entry.get("runtime_commit") or "").strip().lower()
        session_key = str(entry.get("session_key") or "").strip()
        if (
            str(entry.get("status") or "") != "platform_accepted"
            or not delivery_id
            or runtime_commit != current_release.commit
            or _COMMIT_RE.fullmatch(runtime_commit) is None
            or accepted_at <= 0
            or received_at <= 0
            or accepted_at < received_at
            or received_at < receipt_recorded_at_ms
            or ":feishu:direct:" not in session_key
        ):
            continue
        candidates.append((accepted_at, str(inbound_id), entry))
    if not candidates:
        return {"ok": False, "error": "current_release_channel_receipt_not_found"}

    accepted_at, inbound_id, entry = max(candidates, key=lambda item: (item[0], item[1]))
    delivery_id = str(entry.get("delivery_message_id") or "")
    session_key = str(entry.get("session_key") or "")
    evidence_payload = {
        "report_type": REPORT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "passed": True,
        "deployment_commit": current_release.commit,
        "deployment_version": current_release.version,
        "promotion_request_id": current_release.receipt_id,
        "release_session_id": current_release.session_id,
        "platform_accepted_at_ms": accepted_at,
        "inbound_message_digest": _digest(inbound_id),
        "delivery_receipt_digest": _digest(delivery_id),
        "channel_session_digest": _digest(session_key),
    }
    record = append_learning_record_once(
        runtime,
        kind="learning_eval",
        title=f"OpenClaw channel acceptance {current_release.commit[:12]}",
        summary="A direct Feishu reply received a platform acceptance receipt.",
        scope=scope_ref,
        loop_id=f"openclaw_channel_acceptance_{current_release.commit[:12]}",
        step_name="channel.openclaw",
        semantic_key=stable_semantic_key(
            "openclaw_channel_acceptance",
            current_release.commit,
            evidence_payload["delivery_receipt_digest"],
        ),
        authority_tier="L0",
        status="active",
        content=evidence_payload,
        meta={
            "report_type": REPORT_TYPE,
            "schema_version": SCHEMA_VERSION,
            "evidence_class": EVIDENCE_CLASS,
            "passed": True,
            "deployment_commit": current_release.commit,
        },
        evidence=[current_release.receipt_id],
        source=SOURCE,
    )
    return {
        "ok": True,
        "record_id": record.record_id,
        "schema_version": SCHEMA_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "scope": asdict(scope_ref),
        "deployment_commit": current_release.commit,
        "deployment_version": current_release.version,
        "promotion_request_id": current_release.receipt_id,
        "release_session_id": current_release.session_id,
        "platform_accepted_at_ms": accepted_at,
    }


def validate_openclaw_channel_acceptance(
    evidence: Any,
    *,
    current_release: ReleaseIdentity,
) -> bool:
    content = (
        evidence.content
        if isinstance(getattr(evidence, "content", None), Mapping)
        else {}
    )
    return bool(
        getattr(evidence, "kind", "") == "learning_eval"
        and str(getattr(evidence, "source", "") or "") == SOURCE
        and str(content.get("report_type") or "") == REPORT_TYPE
        and str(content.get("schema_version") or "") == SCHEMA_VERSION
        and str(content.get("evidence_class") or "") == EVIDENCE_CLASS
        and content.get("passed") is True
        and str(content.get("deployment_commit") or "") == current_release.commit
        and str(content.get("deployment_version") or "") == current_release.version
        and str(content.get("promotion_request_id") or "")
        == current_release.receipt_id
        and str(content.get("release_session_id") or "") == current_release.session_id
        and _positive_int(content.get("platform_accepted_at_ms")) > 0
        and all(
            _DIGEST_RE.fullmatch(str(content.get(field) or "")) is not None
            for field in (
                "inbound_message_digest",
                "delivery_receipt_digest",
                "channel_session_digest",
            )
        )
    )


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _digest(value: str) -> str:
    return sha256(str(value).encode("utf-8")).hexdigest()


def _iso_timestamp_ms(value: Any) -> int:
    try:
        return int(datetime.fromisoformat(str(value)).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0
