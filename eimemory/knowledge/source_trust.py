from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import math
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


TRUST_AUTHORITY = "eimemory.source_trust.v1"
UNREGISTERED_TRUST_CAP = 0.50
CAPABILITY_TRUST_THRESHOLD = 0.80
DEFAULT_SOURCE_TRUST = {
    "official_docs": 1.0,
    "docs": 1.0,
    "api_docs": 0.95,
    "github_repo": 0.9,
    "paper": 0.85,
    "feishu_doc": 0.8,
    "webpage": 0.65,
    "manual": 0.6,
    "blog": 0.5,
    "summary": 0.5,
    "news": 0.5,
    "rss": 0.5,
}
REGISTERED_SOURCE_TRUST_CAP = {
    "blog": 0.65,
    "manual": 0.65,
    "news": 0.65,
    "rss": 0.65,
    "summary": 0.65,
    "webpage": 0.65,
    "feishu_doc": 0.80,
    "github_repo": 0.85,
    "paper": 0.85,
    "api_docs": 1.00,
    "docs": 1.00,
    "official_docs": 1.00,
}
_POLICY = {
    "authority": TRUST_AUTHORITY,
    "unregistered_trust_cap": UNREGISTERED_TRUST_CAP,
    "capability_trust_threshold": CAPABILITY_TRUST_THRESHOLD,
    "registered_source_trust_cap": REGISTERED_SOURCE_TRUST_CAP,
    "required_bindings": ["enabled_registry_source_id", "normalized_uri", "server_connector_id"],
    "caller_trust_fields": "diagnostic_only",
}
POLICY_DIGEST = sha256(
    json.dumps(_POLICY, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceTrustDecision:
    score: float
    tier: str
    authority: str
    source_id: str
    normalized_uri: str
    connector_id: str
    policy_digest: str
    diagnostic_claimed_trust: float | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


def source_trust_for_kind(source_kind: str) -> float:
    normalized = str(source_kind or "").strip().lower().replace("-", "_")
    return float(DEFAULT_SOURCE_TRUST.get(normalized, UNREGISTERED_TRUST_CAP))


def trust_tier_for_score(value: float) -> str:
    if value >= 0.9:
        return "high"
    if value >= 0.6:
        return "medium"
    return "low"


def normalize_source_uri(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        if not scheme or parts.username is not None or parts.password is not None:
            return ""
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        if not host and scheme in {"http", "https"}:
            return ""
        port = parts.port
    except (UnicodeError, ValueError):
        return ""
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"
    if port is not None and not default_port:
        netloc = f"{netloc}:{port}"
    path = parts.path or ("/" if scheme in {"http", "https"} else "")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def resolve_source_trust(
    payload: Mapping[str, Any],
    *,
    registry: Any,
    connector_id: str,
) -> SourceTrustDecision:
    source_kind = _source_kind(payload)
    source_id = _source_id(payload)
    normalized_uri = normalize_source_uri(_source_uri(payload))
    trusted_connector_id = str(connector_id or "").strip()
    claimed_trust = _claimed_trust(payload)
    fallback = min(source_trust_for_kind(source_kind), UNREGISTERED_TRUST_CAP)
    reasons: list[str] = []

    sources = list(registry.list_sources()) if registry is not None else []
    matching_id = next((item for item in sources if item.source_id == source_id), None) if source_id else None
    if not source_id:
        reasons.append("missing_source_id")
    elif matching_id is None:
        reasons.append("registry_source_not_found")
    elif not matching_id.enabled:
        reasons.append("registry_source_disabled")
    else:
        registry_uri = normalize_source_uri(matching_id.uri)
        metadata = dict(matching_id.metadata or {})
        connector_ids = metadata.get("connector_ids")
        extra_connectors = connector_ids if isinstance(connector_ids, (list, tuple, set)) else []
        expected_connectors = {
            str(value).strip()
            for value in [metadata.get("connector_id"), *extra_connectors]
            if str(value or "").strip()
        }
        expected_kind = str(
            metadata.get("knowledge_source_kind") or matching_id.source_kind or ""
        ).strip().lower().replace("-", "_")
        if not normalized_uri or normalized_uri != registry_uri:
            reasons.append("registry_uri_mismatch")
        if not trusted_connector_id or trusted_connector_id not in expected_connectors:
            reasons.append("connector_mismatch")
        if expected_kind == "url":
            reasons.append("source_kind_unbound")
        if expected_kind and expected_kind != source_kind:
            reasons.append("source_kind_mismatch")
        if not reasons:
            operator_score = _clamp(metadata.get("trust"), default=source_trust_for_kind(source_kind))
            score = min(
                operator_score,
                float(REGISTERED_SOURCE_TRUST_CAP.get(source_kind, UNREGISTERED_TRUST_CAP)),
            )
            return SourceTrustDecision(
                score=score,
                tier=trust_tier_for_score(score),
                authority=TRUST_AUTHORITY,
                source_id=source_id,
                normalized_uri=normalized_uri,
                connector_id=trusted_connector_id,
                policy_digest=POLICY_DIGEST,
                diagnostic_claimed_trust=claimed_trust,
                reasons=("registry_verified",),
            )

    return SourceTrustDecision(
        score=fallback,
        tier=trust_tier_for_score(fallback),
        authority=TRUST_AUTHORITY,
        source_id=source_id,
        normalized_uri=normalized_uri,
        connector_id=trusted_connector_id,
        policy_digest=POLICY_DIGEST,
        diagnostic_claimed_trust=claimed_trust,
        reasons=tuple(reasons or ["unverified_source"]),
    )


def revalidate_source_trust_decision(
    value: Any,
    *,
    registry: Any,
) -> SourceTrustDecision | None:
    existing = source_trust_decision_from_payload(value)
    if existing is None:
        return None
    refreshed = resolve_source_trust(
        _payload(value),
        registry=registry,
        connector_id=existing.connector_id,
    )
    return replace(
        refreshed,
        diagnostic_claimed_trust=existing.diagnostic_claimed_trust,
    )


def source_trust_decision_from_payload(value: Any) -> SourceTrustDecision | None:
    payload = _payload(value)
    candidates = [
        payload.get("source_trust_decision"),
        _nested(payload, "meta", "source_trust_decision"),
        _nested(payload, "content", "source_trust_decision"),
        _nested(payload, "provenance", "source_trust_decision"),
    ]
    raw = next((item for item in candidates if isinstance(item, Mapping)), None)
    if raw is None:
        return None
    try:
        score = _finite_float(raw.get("score"))
        if not 0.0 <= score <= 1.0:
            return None
        score = round(score, 3)
        tier = str(raw.get("tier") or "")
        authority = str(raw.get("authority") or "")
        source_id = str(raw.get("source_id") or "").strip()
        normalized_uri = normalize_source_uri(raw.get("normalized_uri"))
        connector_id = str(raw.get("connector_id") or "").strip()
        policy_digest = str(raw.get("policy_digest") or "")
        reasons = tuple(str(item) for item in (raw.get("reasons") or []) if str(item))
        claimed = raw.get("diagnostic_claimed_trust")
        try:
            diagnostic_claimed_trust = None if claimed is None else round(max(0.0, min(1.0, _finite_float(claimed))), 3)
        except (TypeError, ValueError):
            diagnostic_claimed_trust = None
    except (TypeError, ValueError):
        return None
    if (
        authority != TRUST_AUTHORITY
        or policy_digest != POLICY_DIGEST
        or score < 0.0
        or tier != trust_tier_for_score(score)
        or not reasons
    ):
        return None
    payload_source_id = _source_id(payload)
    payload_uri = normalize_source_uri(_source_uri(payload))
    if source_id and payload_source_id and source_id != payload_source_id:
        return None
    if normalized_uri and payload_uri and normalized_uri != payload_uri:
        return None
    return SourceTrustDecision(
        score=score,
        tier=tier,
        authority=authority,
        source_id=source_id,
        normalized_uri=normalized_uri,
        connector_id=connector_id,
        policy_digest=policy_digest,
        diagnostic_claimed_trust=diagnostic_claimed_trust,
        reasons=reasons,
    )


def _payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    payload: dict[str, Any] = {}
    for key in (
        "record_id",
        "kind",
        "status",
        "title",
        "summary",
        "detail",
        "source",
        "source_id",
        "source_kind",
        "source_uri",
        "content",
        "meta",
        "provenance",
    ):
        if hasattr(value, key):
            payload[key] = getattr(value, key)
    return payload


def _nested(payload: Mapping[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first_text(*values: Any) -> str:
    for value in values:
        text = " ".join(str(value or "").split())
        if text:
            return text
    return ""


def _source_id(payload: Mapping[str, Any]) -> str:
    return _first_text(
        payload.get("source_id"),
        _nested(payload, "meta", "source_id"),
        _nested(payload, "content", "source_id"),
        _nested(payload, "provenance", "source_id"),
    )


def _source_kind(payload: Mapping[str, Any]) -> str:
    return _first_text(
        payload.get("source_kind"),
        _nested(payload, "meta", "source_kind"),
        _nested(payload, "content", "source_kind"),
        _nested(payload, "provenance", "source_kind"),
    ).lower().replace("-", "_")


def _source_uri(payload: Mapping[str, Any]) -> str:
    return _first_text(
        payload.get("source_uri"),
        payload.get("uri"),
        payload.get("url"),
        _nested(payload, "meta", "source_uri"),
        _nested(payload, "content", "source_uri"),
        _nested(payload, "provenance", "source_uri"),
    )


def _claimed_trust(payload: Mapping[str, Any]) -> float | None:
    for value in (
        payload.get("source_trust"),
        payload.get("trust"),
        payload.get("confidence"),
        _nested(payload, "meta", "source_trust"),
        _nested(payload, "content", "source_trust"),
    ):
        if value is None:
            continue
        try:
            return round(max(0.0, min(1.0, _finite_float(value))), 3)
        except (TypeError, ValueError):
            continue
    return None


def _finite_float(value: Any) -> float:
    if isinstance(value, bool):
        raise TypeError("boolean is not a trust score")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("trust score must be finite")
    return number


def _clamp(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    return round(max(0.0, min(1.0, number)), 3)
