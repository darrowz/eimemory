from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping

from eimemory.governance import evidence_contract
from eimemory.governance.evidence_contract import (
    ReleaseIdentity,
    release_identity_from_record,
    same_release_authority,
)
from eimemory.governance.learning_state import (
    append_learning_record_once,
    stable_semantic_key,
)
from eimemory.models.records import ScopeRef
from eimemory.storage.atomic_file import read_json_strict


SCHEMA_VERSION = "external_channel_acceptance.v1"
SOURCE = "eimemory.external_channel.acceptance"
REPORT_TYPE = "external_channel_acceptance"
EVIDENCE_CLASS = "external_channel_receipt"
OPENCLAW_DELIVERY_STATE_SCHEMA = "openclaw_reply_delivery.v2"
EXTERNAL_DELIVERY_STATE_SCHEMA = "external_channel_delivery.v1"
DEFAULT_OPENCLAW_STATE_PATH = Path(
    "/var/lib/eimemory/openclaw_reply_delivery_state.json"
)
DEFAULT_EXTERNAL_STATE_PATH = Path(
    "/var/lib/eimemory/external_channel_delivery_state.json"
)

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_NAME_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_CONVERSATION_KINDS = frozenset({"direct", "group", "channel", "thread", "forum"})
_NON_EXTERNAL_PLATFORMS = frozenset(
    {"local", "deployment-replay", "api", "api_server", "webhook"}
)
_TRUSTED_TRANSPORT_OWNERS = frozenset({"hermes", "openclaw"})
_TRUSTED_EXTERNAL_TRANSPORT_OWNERS = frozenset({"hermes"})


@dataclass(frozen=True, slots=True)
class ExternalDeliveryCandidate:
    transport_owner: str
    platform: str
    conversation_kind: str
    inbound_message_id: str
    delivery_receipt_id: str
    runtime_commit: str
    received_at_ms: int
    platform_accepted_at_ms: int


def record_external_channel_acceptance(
    runtime: Any,
    *,
    scope: ScopeRef | dict | None,
    current_release: ReleaseIdentity,
    openclaw_state_path: str | Path = DEFAULT_OPENCLAW_STATE_PATH,
    external_state_path: str | Path = DEFAULT_EXTERNAL_STATE_PATH,
    _receipt_identity_resolver: Callable[[Any], ReleaseIdentity | None] | None = None,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    receipt = runtime.store.get_by_id(current_release.receipt_id, scope=scope_ref)
    resolver = (
        _receipt_identity_resolver
        or evidence_contract.verified_deployment_receipt_identity
    )
    if not same_release_authority(resolver(receipt), current_release):
        return {"ok": False, "error": "current_deployment_receipt_invalid"}
    receipt_recorded_at_ms = _iso_timestamp_ms(
        getattr(getattr(receipt, "time", None), "created_at", "")
    )
    if receipt_recorded_at_ms <= 0:
        return {"ok": False, "error": "current_deployment_receipt_timestamp_invalid"}

    candidates: list[ExternalDeliveryCandidate] = []
    states_seen = 0
    invalid_states = 0
    for path, reader in (
        (Path(openclaw_state_path), _openclaw_candidates),
        (Path(external_state_path), _external_candidates),
    ):
        if not path.exists():
            continue
        states_seen += 1
        if path.is_symlink() or not path.is_file():
            invalid_states += 1
            continue
        try:
            document = read_json_strict(path, dict)
            candidates.extend(reader(document))
        except (OSError, ValueError):
            invalid_states += 1

    if states_seen == 0:
        return {"ok": False, "error": "channel_delivery_state_missing"}

    eligible = [
        candidate
        for candidate in candidates
        if candidate.runtime_commit == current_release.commit
        and candidate.received_at_ms >= receipt_recorded_at_ms
    ]
    if not eligible:
        if invalid_states == states_seen:
            return {"ok": False, "error": "channel_delivery_state_invalid"}
        return {"ok": False, "error": "current_release_channel_receipt_not_found"}

    candidate = max(
        eligible,
        key=lambda item: (
            item.platform_accepted_at_ms,
            item.transport_owner,
            item.platform,
            item.delivery_receipt_id,
        ),
    )
    evidence_payload = {
        "report_type": REPORT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "passed": True,
        "deployment_commit": current_release.commit,
        "deployment_version": current_release.version,
        "promotion_request_id": current_release.receipt_id,
        # ``release_session_id`` is a store-owned canonical field.  Keep an
        # explicit alias so the acceptance remains release-bound even in
        # isolated runtimes that cannot independently discover their release.
        "deployment_session_id": current_release.session_id,
        "transport_owner": candidate.transport_owner,
        "platform": candidate.platform,
        "conversation_kind": candidate.conversation_kind,
        "platform_accepted_at_ms": candidate.platform_accepted_at_ms,
        "inbound_message_digest": _digest(candidate.inbound_message_id),
        "delivery_receipt_digest": _digest(candidate.delivery_receipt_id),
        "channel_session_digest": _digest(
            f"{candidate.transport_owner}:{candidate.platform}:"
            f"{candidate.conversation_kind}"
        ),
    }
    record = append_learning_record_once(
        runtime,
        kind="learning_eval",
        title=f"External channel acceptance {current_release.commit[:12]}",
        summary=(
            "A real external user turn received a platform-accepted response."
        ),
        scope=scope_ref,
        loop_id=f"external_channel_acceptance_{current_release.commit[:12]}",
        step_name="channel.delivery",
        semantic_key=stable_semantic_key(
            "external_channel_acceptance",
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
            "transport_owner": candidate.transport_owner,
            "platform": candidate.platform,
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
        "transport_owner": candidate.transport_owner,
        "platform": candidate.platform,
        "conversation_kind": candidate.conversation_kind,
        "platform_accepted_at_ms": candidate.platform_accepted_at_ms,
    }


def validate_external_channel_acceptance(
    evidence: Any,
    *,
    current_release: ReleaseIdentity,
) -> bool:
    content = (
        evidence.content
        if isinstance(getattr(evidence, "content", None), Mapping)
        else {}
    )
    recorded_release = release_identity_from_record(evidence)
    return bool(
        getattr(evidence, "kind", "") == "learning_eval"
        and str(getattr(evidence, "source", "") or "") == SOURCE
        and str(content.get("report_type") or "") == REPORT_TYPE
        and str(content.get("schema_version") or "") == SCHEMA_VERSION
        and str(content.get("evidence_class") or "") == EVIDENCE_CLASS
        and content.get("passed") is True
        and same_release_authority(recorded_release, current_release)
        and str(content.get("transport_owner") or "").strip().lower()
        in _TRUSTED_TRANSPORT_OWNERS
        and _valid_platform(content.get("platform"))
        and str(content.get("conversation_kind") or "") in _CONVERSATION_KINDS
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


def _openclaw_candidates(document: Mapping[str, Any]) -> Iterable[ExternalDeliveryCandidate]:
    if (
        str(document.get("schema_version") or "") != OPENCLAW_DELIVERY_STATE_SCHEMA
        or not isinstance(document.get("entries"), dict)
    ):
        raise ValueError("invalid OpenClaw delivery state contract")
    candidates: list[ExternalDeliveryCandidate] = []
    for inbound_id, raw_entry in document["entries"].items():
        entry = raw_entry if isinstance(raw_entry, Mapping) else {}
        session_key = str(entry.get("session_key") or "").strip()
        if ":feishu:direct:" not in session_key:
            continue
        candidate = _candidate(
            status=entry.get("status"),
            transport_owner="openclaw",
            platform="feishu",
            conversation_kind="direct",
            inbound_message_id=inbound_id,
            delivery_receipt_id=entry.get("delivery_message_id"),
            runtime_commit=entry.get("runtime_commit"),
            received_at_ms=entry.get("received_at_ms"),
            platform_accepted_at_ms=entry.get("platform_accepted_at_ms"),
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _external_candidates(document: Mapping[str, Any]) -> Iterable[ExternalDeliveryCandidate]:
    if (
        str(document.get("schema_version") or "") != EXTERNAL_DELIVERY_STATE_SCHEMA
        or not isinstance(document.get("entries"), dict)
    ):
        raise ValueError("invalid external delivery state contract")
    candidates: list[ExternalDeliveryCandidate] = []
    for _entry_id, raw_entry in document["entries"].items():
        entry = raw_entry if isinstance(raw_entry, Mapping) else {}
        if (
            str(entry.get("transport_owner") or "").strip().lower()
            not in _TRUSTED_EXTERNAL_TRANSPORT_OWNERS
        ):
            continue
        candidate = _candidate(
            status=entry.get("status"),
            transport_owner=entry.get("transport_owner"),
            platform=entry.get("platform"),
            conversation_kind=entry.get("conversation_kind"),
            inbound_message_id=entry.get("inbound_message_id"),
            delivery_receipt_id=entry.get("delivery_receipt_id"),
            runtime_commit=entry.get("runtime_commit"),
            received_at_ms=entry.get("received_at_ms"),
            platform_accepted_at_ms=entry.get("platform_accepted_at_ms"),
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _candidate(**raw: Any) -> ExternalDeliveryCandidate | None:
    status = str(raw.get("status") or "").strip()
    transport_owner = str(raw.get("transport_owner") or "").strip().lower()
    platform = str(raw.get("platform") or "").strip().lower()
    conversation_kind = str(raw.get("conversation_kind") or "").strip().lower()
    inbound_message_id = str(raw.get("inbound_message_id") or "").strip()
    delivery_receipt_id = str(raw.get("delivery_receipt_id") or "").strip()
    runtime_commit = str(raw.get("runtime_commit") or "").strip().lower()
    received_at_ms = _positive_int(raw.get("received_at_ms"))
    accepted_at_ms = _positive_int(raw.get("platform_accepted_at_ms"))
    if not (
        status == "platform_accepted"
        and _valid_name(transport_owner)
        and _valid_platform(platform)
        and conversation_kind in _CONVERSATION_KINDS
        and inbound_message_id
        and delivery_receipt_id
        and _COMMIT_RE.fullmatch(runtime_commit) is not None
        and received_at_ms > 0
        and accepted_at_ms >= received_at_ms
    ):
        return None
    return ExternalDeliveryCandidate(
        transport_owner=transport_owner,
        platform=platform,
        conversation_kind=conversation_kind,
        inbound_message_id=inbound_message_id,
        delivery_receipt_id=delivery_receipt_id,
        runtime_commit=runtime_commit,
        received_at_ms=received_at_ms,
        platform_accepted_at_ms=accepted_at_ms,
    )


def _valid_name(value: Any) -> bool:
    return _NAME_RE.fullmatch(str(value or "").strip().lower()) is not None


def _valid_platform(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    return bool(_valid_name(normalized) and normalized not in _NON_EXTERNAL_PLATFORMS)


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
