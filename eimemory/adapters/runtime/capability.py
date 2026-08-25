"""Provider-independent, internal adapter capability advertisements.

This module is intentionally below every retained adapter surface and above
the dynamic capability service.  It does not define a fixed L5 taxonomy, add a
model-facing tool, or open SQLite.  Adapters supply an explicit provider
binding/revision statement; the Runtime capability service validates and
persists its immutable history through the Storage v2 boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
from typing import Any, Protocol

from eimemory.capabilities.contracts import (
    CapabilityContractError,
    contract_digest,
    ensure_allowed,
    normalize_json_payload,
    normalize_opaque_id,
    normalize_string_sequence,
    normalize_text,
    require_timestamp,
)
from eimemory.capabilities.models import (
    ADAPTER_CAPABILITY_ADVERTISEMENT_SCHEMA_VERSION,
    RUN_VERDICTS,
    AdapterCapabilityAdvertisement,
)


ADAPTER_CAPABILITY_OUTCOME_SCHEMA_VERSION = "adapter.capability_outcome.v1"
ADAPTER_CAPABILITY_RECEIPT_SCHEMA_VERSION = "adapter.capability_receipt.v1"
DEFAULT_ADVERTISEMENT_TTL_SECONDS = 3_600
MAX_ADVERTISEMENT_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_ADVERTISEMENT_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_ADVERTISEMENT_FUTURE_SKEW_SECONDS = 5 * 60
MAX_ADVERTISEMENTS_PER_CALL = 32
MAX_HOST_EVENT_TYPES = 64
MAX_DIAGNOSTIC_BYTES = 16_384
MAX_OUTCOME_DIAGNOSTIC_BYTES = 8_192
IMPLEMENTATION_FINGERPRINT_REVISIONS = frozenset(
    {
        "code.implementation:v2",
        "code.implementation:v3",
        "code.implementation:v4",
        "code.implementation:v5",
        "code.implementation:v6",
    }
)


class AdapterCapabilityError(ValueError):
    """An untrusted adapter advertisement or normalized outcome is unsafe."""


class AdvertisementSignatureVerifier(Protocol):
    """Optional policy hook for channels that require signed advertisements."""

    def __call__(self, advertisement: AdapterCapabilityAdvertisement) -> bool: ...


_ADVERT_CONTEXT_KEYS = frozenset(
    {
        "advertisement_id",
        "advertisement_revision",
        "binding_id",
        "capability_revision_id",
        "contract_digest",
        "provider_instance_id",
        "operations",
        "limits",
        "side_effect_class",
        "host_event_types",
        "environment_fingerprint",
        "diagnostic_metadata",
        "applicability",
        "evidence_refs",
        "advertised_at",
        "expires_at",
        "created_at",
        "status",
        "capability_scope",
        "provenance",
        "signature",
        "request_key",
        # These fields are accepted only to assert that an RPC caller did not
        # relabel a channel.  They are never trusted as provider selection.
        "adapter_id",
        "provider_kind",
    }
)
_OUTCOME_KEYS = frozenset(
    {
        "binding_id",
        "capability_revision_id",
        "event_id",
        "occurred_at",
        "verdict",
        "evidence_refs",
        "metrics",
        "summary",
        "diagnostic_metadata",
    }
)
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?(?:key|token)|access[_-]?token|auth[_-]?token|refresh[_-]?token|"
    r"token|secret|password|private[_-]?key|authorization|cookie|credential)s?$",
    re.IGNORECASE,
)
_HOST_KEY = re.compile(
    r"(?:host(?:name)?|machine|node|device|path|cwd|executable|binary)(?:[_-]?(?:id|name|path))?$",
    re.IGNORECASE,
)
_SECRET_TEXT = re.compile(
    r"(?i)(?:\bbearer\s+[a-z0-9._~+/=-]{8,}|\bsk-[a-z0-9_-]{8,}|"
    r"(?:api[_-]?key|token|secret|password|authorization|cookie)\s*[:=])"
)
_HASHED_DIAGNOSTIC = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_HOST_EVENT_TYPE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _plus_seconds(value: str, seconds: int) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _bounded_reason(value: str) -> str:
    allowed = {
        "invalid_host_event",
        "capability_outcome_not_declared",
        "outcome_schema_invalid",
        "binding_not_advertised",
        "capability_revision_mismatch",
        "advertisement_stale",
        "unsupported_host_event",
        "advertisement_lookup_failed",
    }
    return value if value in allowed else "invalid_host_event"


def _advertisement_rejection_reason(error: AdapterCapabilityError) -> str:
    """Map untrusted validation details to a stable, secret-safe receipt code."""

    reason = str(error)
    allowed = {
        "advertisement_schema_invalid",
        "advertisement_stale",
        "advertisement_signature_required",
        "advertisement_signature_invalid",
        "implementation_digest_mismatch",
    }
    return reason if reason in allowed else "advertisement_schema_invalid"


def _safe_event_type(value: object) -> str:
    event_type = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if not _SAFE_HOST_EVENT_TYPE.fullmatch(event_type) or _SECRET_TEXT.search(event_type):
        return "unknown"
    return event_type


def _safe_summary(value: object) -> str:
    summary = normalize_text(value, field="outcome.summary", max_chars=2_000)
    return "[REDACTED]" if _SECRET_TEXT.search(summary) else summary


def _normalize_context(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AdapterCapabilityError("adapter_context must be an object")
    if len(value) > len(_ADVERT_CONTEXT_KEYS):
        raise AdapterCapabilityError("adapter_context has too many fields")
    unknown = set(value).difference(_ADVERT_CONTEXT_KEYS)
    if unknown:
        raise AdapterCapabilityError(
            "adapter_context contains unsupported fields: " + ", ".join(sorted(str(item) for item in unknown))
        )
    return dict(value)


def _canonical_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def sanitize_diagnostic_metadata(
    value: Mapping[str, Any] | None,
    *,
    field: str = "diagnostic_metadata",
    max_bytes: int = MAX_DIAGNOSTIC_BYTES,
) -> dict[str, Any]:
    """Return bounded diagnostics without raw secrets, hosts, or commands.

    The result is suitable for durable advertisement metadata and outcome
    diagnostics.  Host-like values become stable hashes so operators can
    correlate an environment without turning host identity into a capability
    identity.  Executable-shaped payloads fail closed instead of being stored.
    """

    if value is None:
        return {}
    normalized = normalize_json_payload(
        value,
        field=field,
        reject_executable=True,
        max_bytes=max_bytes,
    )

    def scrub(item: Any, *, key: str = "") -> Any:
        canonical_key = _canonical_key(key)
        if _SENSITIVE_KEY.search(canonical_key):
            return "[REDACTED]"
        if isinstance(item, Mapping):
            return {str(child_key): scrub(child, key=str(child_key)) for child_key, child in item.items()}
        if isinstance(item, list):
            return [scrub(child, key=key) for child in item]
        if isinstance(item, str):
            if _SECRET_TEXT.search(item):
                return "[REDACTED]"
            if _HOST_KEY.search(canonical_key):
                if _HASHED_DIAGNOSTIC.fullmatch(item):
                    return item
                return "sha256:" + sha256(item.encode("utf-8", errors="replace")).hexdigest()
            return item
        return item

    return dict(scrub(normalized))


def _required_value(context: Mapping[str, Any], key: str) -> Any:
    value = context.get(key)
    if value is None or value == "":
        raise AdapterCapabilityError(f"adapter_context.{key} is required")
    return value


def _request_key(advertisement: AdapterCapabilityAdvertisement, supplied: object) -> str:
    if supplied is not None:
        try:
            request_key = normalize_opaque_id(
                supplied,
                field="adapter_context.request_key",
            )
        except CapabilityContractError as exc:
            raise AdapterCapabilityError("advertisement_schema_invalid") from exc
        if _SECRET_TEXT.search(request_key):
            raise AdapterCapabilityError("advertisement_schema_invalid")
        return request_key
    return (
        "adapter.advertisement:"
        f"{advertisement.adapter_id}:{advertisement.advertisement_id}:{advertisement.advertisement_digest}"
    )


def _safe_evidence_refs(value: Sequence[object] | object, *, field: str) -> tuple[str, ...]:
    refs = normalize_string_sequence(value, field=field, item_field="evidence_ref", max_items=64)
    if not refs:
        raise AdapterCapabilityError(f"{field} must not be empty")
    if any(_SECRET_TEXT.search(ref) for ref in refs):
        raise AdapterCapabilityError(f"{field} contains a credential-like value")
    return refs


@dataclass(frozen=True, slots=True)
class UnsupportedCapabilityOutcome:
    """Explicit non-attribution result for an unsupported host event."""

    adapter_id: str
    event_type: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ADAPTER_CAPABILITY_OUTCOME_SCHEMA_VERSION,
            "ok": False,
            "status": "unsupported",
            "adapter_id": self.adapter_id,
            "event_type": self.event_type,
            "reason": _bounded_reason(self.reason),
        }


@dataclass(frozen=True, slots=True)
class NormalizedCapabilityOutcome:
    """Bounded adapter outcome ready for WP7 observation persistence."""

    adapter_id: str
    binding_id: str
    capability_revision_id: str
    event_type: str
    event_id: str
    occurred_at: str
    verdict: str
    evidence_refs: tuple[str, ...]
    metrics: Mapping[str, Any]
    summary: str
    diagnostic_metadata: Mapping[str, Any]
    outcome_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ADAPTER_CAPABILITY_OUTCOME_SCHEMA_VERSION,
            "ok": True,
            "status": "normalized",
            "adapter_id": self.adapter_id,
            "binding_id": self.binding_id,
            "capability_revision_id": self.capability_revision_id,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "verdict": self.verdict,
            "evidence_refs": list(self.evidence_refs),
            "metrics": dict(self.metrics),
            "summary": self.summary,
            "diagnostic_metadata": dict(self.diagnostic_metadata),
            "outcome_digest": self.outcome_digest,
        }


class AdapterCapabilityService:
    """Internal protocol implementation for one adapter/provider kind.

    Callers use this class from hooks, provider cores, or RPC handlers.  The
    only mutation path is ``Runtime.capabilities``; no adapter receives a raw
    store or database handle.
    """

    def __init__(
        self,
        runtime: Any,
        *,
        adapter_id: str,
        provider_kind: str = "",
        require_signature: bool = False,
        signature_verifier: AdvertisementSignatureVerifier | None = None,
        max_advertisement_age_seconds: int = MAX_ADVERTISEMENT_AGE_SECONDS,
    ) -> None:
        self.runtime = runtime
        self.adapter_id = normalize_opaque_id(adapter_id, field="adapter_id")
        self.provider_kind = normalize_opaque_id(
            provider_kind or adapter_id,
            field="provider_kind",
        )
        self.require_signature = bool(require_signature)
        self.signature_verifier = signature_verifier
        self.max_advertisement_age_seconds = max(
            1,
            min(MAX_ADVERTISEMENT_AGE_SECONDS, int(max_advertisement_age_seconds)),
        )

    def build_advertisement(
        self,
        adapter_context: Mapping[str, Any],
        *,
        now: str = "",
    ) -> AdapterCapabilityAdvertisement:
        """Validate one explicit adapter statement before any persistence."""

        context = _normalize_context(adapter_context)
        asserted_adapter = context.get("adapter_id")
        if asserted_adapter not in (None, "", self.adapter_id):
            raise AdapterCapabilityError("adapter_context.adapter_id does not match adapter surface")
        asserted_provider = context.get("provider_kind")
        if asserted_provider not in (None, "", self.provider_kind):
            raise AdapterCapabilityError("adapter_context.provider_kind does not match adapter surface")
        reference_time = require_timestamp(now, field="now", required=False) if now else _now_iso()
        advertised_at = require_timestamp(
            context.get("advertised_at", reference_time),
            field="adapter_context.advertised_at",
        )
        expires_at = require_timestamp(
            context.get(
                "expires_at",
                _plus_seconds(advertised_at, DEFAULT_ADVERTISEMENT_TTL_SECONDS),
            ),
            field="adapter_context.expires_at",
        )
        created_at = require_timestamp(
            context.get("created_at", advertised_at),
            field="adapter_context.created_at",
        )
        # CapabilityStore seeds lifecycle state from an immutable descriptor's
        # created_at.  Do not let a host backdate that state independently of
        # the advertised fact it is submitting.
        if created_at != advertised_at:
            raise AdapterCapabilityError("advertisement_schema_invalid")
        diagnostics = sanitize_diagnostic_metadata(
            context.get("environment_fingerprint", context.get("diagnostic_metadata", {})),
            field="adapter_context.environment_fingerprint",
        )
        if not diagnostics:
            diagnostics = {"adapter_id": self.adapter_id, "provider_kind": self.provider_kind}
        applicability = sanitize_diagnostic_metadata(
            context.get("applicability", {"adapter_id": self.adapter_id}),
            field="adapter_context.applicability",
        )
        if not applicability:
            raise AdapterCapabilityError("adapter_context.applicability must not be empty")
        provenance = sanitize_diagnostic_metadata(
            context.get("provenance", {"source": f"adapter.{self.adapter_id}"}),
            field="adapter_context.provenance",
        )
        raw_signature = context.get("signature", {})
        if raw_signature is not None and not isinstance(raw_signature, Mapping):
            raise AdapterCapabilityError("advertisement_schema_invalid")
        try:
            advertisement = AdapterCapabilityAdvertisement(
                advertisement_id=_required_value(context, "advertisement_id"),
                advertisement_revision=_required_value(context, "advertisement_revision"),
                binding_id=_required_value(context, "binding_id"),
                capability_revision_id=_required_value(context, "capability_revision_id"),
                adapter_id=self.adapter_id,
                provider_kind=self.provider_kind,
                provider_instance_id=_required_value(context, "provider_instance_id"),
                contract_digest=_required_value(context, "contract_digest"),
                operations=_required_value(context, "operations"),
                limits=_required_value(context, "limits"),
                side_effect_class=_required_value(context, "side_effect_class"),
                host_event_types=_required_value(context, "host_event_types"),
                environment_fingerprint=diagnostics,
                applicability=applicability,
                evidence_refs=_safe_evidence_refs(
                    _required_value(context, "evidence_refs"),
                    field="adapter_context.evidence_refs",
                ),
                advertised_at=advertised_at,
                expires_at=expires_at,
                created_at=created_at,
                status=str(context.get("status") or "active"),
                scope=str(context.get("capability_scope") or "global"),
                provenance=provenance,
                signature=raw_signature or {},
                schema_version=ADAPTER_CAPABILITY_ADVERTISEMENT_SCHEMA_VERSION,
            )
        except (CapabilityContractError, TypeError, ValueError) as exc:
            raise AdapterCapabilityError("advertisement_schema_invalid") from exc
        if len(advertisement.host_event_types) > MAX_HOST_EVENT_TYPES:
            raise AdapterCapabilityError("advertisement_schema_invalid")
        self._assert_fresh(advertisement, now=reference_time)
        self._assert_signature(advertisement)
        return advertisement

    def advertise_capabilities(
        self,
        adapter_context: Mapping[str, Any],
        *,
        runtime_scope: Mapping[str, Any],
        now: str = "",
    ) -> dict[str, Any]:
        """Persist one validated advertisement through ``Runtime.capabilities``.

        A rejected advertisement returns a small safe receipt rather than a
        transport traceback.  This preserves fail-open host behavior while the
        capability control plane itself remains fail-closed.
        """

        try:
            advertisement = self.build_advertisement(adapter_context, now=now)
            fingerprint_error = self._implementation_fingerprint_error(
                advertisement,
                runtime_scope=runtime_scope,
                capability_scope=str(adapter_context.get("capability_scope") or "global")
                if isinstance(adapter_context, Mapping)
                else "global",
            )
            if fingerprint_error:
                raise AdapterCapabilityError(fingerprint_error)
            request_key = _request_key(advertisement, dict(adapter_context).get("request_key"))
            receipt = self.runtime.capabilities.advertise(
                advertisement,
                runtime_scope=runtime_scope,
                request_key=request_key,
            )
        except AdapterCapabilityError as exc:
            return {
                "schema_version": ADAPTER_CAPABILITY_RECEIPT_SCHEMA_VERSION,
                "ok": False,
                "status": "rejected",
                "adapter_id": self.adapter_id,
                "reason": _advertisement_rejection_reason(exc),
            }
        except (CapabilityContractError, TypeError, ValueError):
            return {
                "schema_version": ADAPTER_CAPABILITY_RECEIPT_SCHEMA_VERSION,
                "ok": False,
                "status": "rejected",
                "adapter_id": self.adapter_id,
                "reason": "advertisement_schema_invalid",
            }
        except RuntimeError:
            return {
                "schema_version": ADAPTER_CAPABILITY_RECEIPT_SCHEMA_VERSION,
                "ok": False,
                "status": "rejected",
                "adapter_id": self.adapter_id,
                "reason": "advertisement_rejected",
            }
        return {
            "schema_version": ADAPTER_CAPABILITY_RECEIPT_SCHEMA_VERSION,
            "ok": True,
            "status": "accepted",
            "adapter_id": self.adapter_id,
            "advertisement_id": advertisement.advertisement_id,
            "advertisement_revision": advertisement.advertisement_revision,
            "binding_id": advertisement.binding_id,
            "capability_revision_id": advertisement.capability_revision_id,
            "advertisement_digest": advertisement.advertisement_digest,
            "expires_at": advertisement.expires_at,
            "signature_verified": bool(self.require_signature and advertisement.signature),
            "mutation": receipt.to_dict(),
        }

    def _implementation_fingerprint_error(
        self,
        advertisement: AdapterCapabilityAdvertisement,
        *,
        runtime_scope: Mapping[str, Any],
        capability_scope: str,
    ) -> str:
        """Require the v2 implementation fingerprint to match its binding."""

        if advertisement.capability_revision_id not in IMPLEMENTATION_FINGERPRINT_REVISIONS:
            return ""
        binding_context = getattr(self.runtime.capabilities, "binding_context", None)
        if not callable(binding_context):
            return "binding_not_advertised"
        try:
            binding = binding_context(
                advertisement.binding_id,
                runtime_scope=runtime_scope,
                capability_scope=capability_scope,
                at_time=advertisement.advertised_at,
            )
        except (RuntimeError, TypeError, ValueError):
            return "binding_not_advertised"
        descriptor = binding.get("descriptor") if isinstance(binding, Mapping) else {}
        expected = str((descriptor or {}).get("implementation_digest") or "").strip().lower()
        actual = str(
            (advertisement.environment_fingerprint or {}).get("implementation_digest") or ""
        ).strip().lower()
        if not expected or actual != expected:
            return "implementation_digest_mismatch"
        return ""

    def capability_health(
        self,
        binding_id: str,
        *,
        runtime_scope: Mapping[str, Any],
        capability_scope: str = "global",
        at_time: str = "",
    ) -> dict[str, Any]:
        """Return honest freshness/lifecycle state for one explicit binding."""

        try:
            normalized_binding = normalize_opaque_id(binding_id, field="binding_id")
            checked_at = require_timestamp(at_time, field="at_time", required=False) if at_time else _now_iso()
            all_advertisements = self.runtime.capabilities.list_advertisements(
                runtime_scope=runtime_scope,
                capability_scope=capability_scope,
                binding_id=normalized_binding,
                adapter_id=self.adapter_id,
                provider_kind=self.provider_kind,
                status=None,
                at_time=checked_at,
                limit=MAX_ADVERTISEMENTS_PER_CALL,
            )
            fresh_advertisements = self.runtime.capabilities.list_advertisements(
                runtime_scope=runtime_scope,
                capability_scope=capability_scope,
                binding_id=normalized_binding,
                adapter_id=self.adapter_id,
                provider_kind=self.provider_kind,
                status="active",
                at_time=checked_at,
                fresh_at=checked_at,
                limit=MAX_ADVERTISEMENTS_PER_CALL,
            )
        except (CapabilityContractError, RuntimeError, TypeError, ValueError):
            return {
                "schema_version": ADAPTER_CAPABILITY_RECEIPT_SCHEMA_VERSION,
                "ok": False,
                "binding_id": "",
                "adapter_id": self.adapter_id,
                "readiness": "unknown",
                "reason": "advertisement_lookup_failed",
            }
        if fresh_advertisements:
            readiness, reason = "ready", "fresh_advertisement"
        elif all_advertisements:
            readiness, reason = "degraded", "advertisement_stale_or_inactive"
        else:
            readiness, reason = "not_ready", "binding_not_advertised"
        return {
            "schema_version": ADAPTER_CAPABILITY_RECEIPT_SCHEMA_VERSION,
            "ok": bool(fresh_advertisements),
            "binding_id": normalized_binding,
            "adapter_id": self.adapter_id,
            "provider_kind": self.provider_kind,
            "checked_at": checked_at,
            "readiness": readiness,
            "reason": reason,
            "advertisement_count": len(all_advertisements),
            "fresh_advertisement_count": len(fresh_advertisements),
            "fresh_advertisement_ids": [
                str(item.get("entity_id") or "") for item in fresh_advertisements[:MAX_ADVERTISEMENTS_PER_CALL]
            ],
        }

    def normalize_capability_outcome(
        self,
        host_event: Mapping[str, Any] | None,
        *,
        runtime_scope: Mapping[str, Any],
        event_type: str = "",
        capability_scope: str = "global",
    ) -> dict[str, Any]:
        """Normalize only a host-declared capability outcome.

        A regular host event can carry unrelated context, credentials, or an
        ambiguous task result.  It is *not* inferred into a capability.  The
        host must provide a narrow ``capability_outcome`` object whose binding,
        revision, evidence, and verdict can be matched to a fresh
        advertisement.  Unsupported events remain explicit results.
        """

        if not isinstance(host_event, Mapping):
            return self._unsupported(event_type, "invalid_host_event")
        resolved_event_type = _safe_event_type(event_type or host_event.get("event_type") or "")
        if resolved_event_type == "unknown":
            return self._unsupported(resolved_event_type, "invalid_host_event")
        raw_outcome = host_event.get("capability_outcome")
        if not isinstance(raw_outcome, Mapping):
            return self._unsupported(resolved_event_type, "capability_outcome_not_declared")
        if len(raw_outcome) > len(_OUTCOME_KEYS) or set(raw_outcome).difference(_OUTCOME_KEYS):
            return self._unsupported(resolved_event_type, "outcome_schema_invalid")
        try:
            binding_id = normalize_opaque_id(raw_outcome.get("binding_id"), field="outcome.binding_id")
            revision_id = normalize_opaque_id(
                raw_outcome.get("capability_revision_id"),
                field="outcome.capability_revision_id",
            )
            event_id = normalize_opaque_id(raw_outcome.get("event_id"), field="outcome.event_id")
            occurred_at = require_timestamp(raw_outcome.get("occurred_at"), field="outcome.occurred_at")
            verdict = ensure_allowed(raw_outcome.get("verdict"), field="outcome.verdict", allowed=RUN_VERDICTS)
            evidence_refs = _safe_evidence_refs(
                raw_outcome.get("evidence_refs"),
                field="outcome.evidence_refs",
            )
            metrics = sanitize_diagnostic_metadata(
                raw_outcome.get("metrics", {}),
                field="outcome.metrics",
                max_bytes=MAX_OUTCOME_DIAGNOSTIC_BYTES,
            )
            diagnostics = sanitize_diagnostic_metadata(
                raw_outcome.get("diagnostic_metadata", {}),
                field="outcome.diagnostic_metadata",
                max_bytes=MAX_OUTCOME_DIAGNOSTIC_BYTES,
            )
            summary = _safe_summary(raw_outcome.get("summary", "outcome reported"))
            advertisements = self.runtime.capabilities.list_advertisements(
                runtime_scope=runtime_scope,
                capability_scope=capability_scope,
                binding_id=binding_id,
                adapter_id=self.adapter_id,
                provider_kind=self.provider_kind,
                status="active",
                at_time=occurred_at,
                fresh_at=occurred_at,
                limit=MAX_ADVERTISEMENTS_PER_CALL,
            )
        except (AdapterCapabilityError, CapabilityContractError, RuntimeError, TypeError, ValueError):
            return self._unsupported(resolved_event_type, "outcome_schema_invalid")
        if not advertisements:
            return self._unsupported(resolved_event_type, "binding_not_advertised")
        binding_context = getattr(self.runtime.capabilities, "binding_context", None)
        binding_descriptor: Mapping[str, Any] = {}
        if revision_id in IMPLEMENTATION_FINGERPRINT_REVISIONS:
            try:
                binding = binding_context(
                    binding_id,
                    runtime_scope=runtime_scope,
                    capability_scope=capability_scope,
                    at_time=occurred_at,
                ) if callable(binding_context) else None
                binding_descriptor = (
                    binding.get("descriptor")
                    if isinstance(binding, Mapping) and isinstance(binding.get("descriptor"), Mapping)
                    else {}
                )
            except (RuntimeError, TypeError, ValueError):
                return self._unsupported(resolved_event_type, "binding_not_advertised")
        matched_revision = [
            item
            for item in advertisements
            if str((item.get("descriptor") or {}).get("capability_revision_id") or "") == revision_id
        ]
        if not matched_revision:
            return self._unsupported(resolved_event_type, "capability_revision_mismatch")
        if revision_id in IMPLEMENTATION_FINGERPRINT_REVISIONS:
            expected_digest = str(binding_descriptor.get("implementation_digest") or "").strip().lower()
            if not expected_digest or any(
                str(
                    ((item.get("descriptor") or {}).get("environment_fingerprint") or {}).get(
                        "implementation_digest"
                    )
                    or ""
                ).strip().lower()
                != expected_digest
                for item in matched_revision
            ):
                return self._unsupported(resolved_event_type, "implementation_digest_mismatch")
        matching_event = [
            item
            for item in matched_revision
            if resolved_event_type in tuple((item.get("descriptor") or {}).get("host_event_types") or ())
        ]
        if not matching_event:
            return self._unsupported(resolved_event_type, "unsupported_host_event")
        payload = {
            "adapter_id": self.adapter_id,
            "binding_id": binding_id,
            "capability_revision_id": revision_id,
            "event_type": resolved_event_type,
            "event_id": event_id,
            "occurred_at": occurred_at,
            "verdict": verdict,
            "evidence_refs": list(evidence_refs),
            "metrics": metrics,
            "summary": summary,
            "diagnostic_metadata": diagnostics,
        }
        outcome = NormalizedCapabilityOutcome(
            adapter_id=self.adapter_id,
            binding_id=binding_id,
            capability_revision_id=revision_id,
            event_type=resolved_event_type,
            event_id=event_id,
            occurred_at=occurred_at,
            verdict=verdict,
            evidence_refs=tuple(evidence_refs),
            metrics=metrics,
            summary=summary,
            diagnostic_metadata=diagnostics,
            outcome_digest=contract_digest(payload),
        )
        return outcome.to_dict()

    def persist_normalized_capability_outcome(
        self,
        normalized_outcome: Mapping[str, Any],
        *,
        runtime_scope: Mapping[str, Any],
        independent_verifier: Mapping[str, Any],
        environment_fingerprint: Mapping[str, Any],
        provenance: Mapping[str, Any],
        capability_scope: str = "global",
    ) -> dict[str, Any]:
        """Persist a previously normalized pass/fail adapter outcome as v3 evidence.

        This is intentionally a second, explicit operation.  Normalization is
        host-facing and can stay best-effort; persistence requires a distinct
        verifier and resolves the capability ID from the registered binding
        contract.  A host cannot relabel an outcome by injecting a capability
        name, and an adapter cannot self-grade merely by emitting a success
        event.
        """

        if not isinstance(normalized_outcome, Mapping) or normalized_outcome.get("ok") is not True:
            return self._persistence_result("unclassified", "normalized_outcome_required")
        if str(normalized_outcome.get("adapter_id") or "") != self.adapter_id:
            return self._persistence_result("unclassified", "adapter_identity_mismatch")
        verdict = str(normalized_outcome.get("verdict") or "").strip().lower()
        if verdict not in {"pass", "fail"}:
            return self._persistence_result("unclassified", "outcome_verdict_requires_evaluation_run")
        if not isinstance(independent_verifier, Mapping) or independent_verifier.get("independent") is not True:
            return self._persistence_result("unclassified", "independent_verifier_required")
        if not isinstance(environment_fingerprint, Mapping) or not environment_fingerprint:
            return self._persistence_result("unclassified", "environment_fingerprint_required")
        if not isinstance(provenance, Mapping) or not provenance:
            return self._persistence_result("unclassified", "provenance_required")
        try:
            binding_id = normalize_opaque_id(normalized_outcome.get("binding_id"), field="outcome.binding_id")
            revision_id = normalize_opaque_id(
                normalized_outcome.get("capability_revision_id"),
                field="outcome.capability_revision_id",
            )
            event_id = normalize_opaque_id(normalized_outcome.get("event_id"), field="outcome.event_id")
            occurred_at = require_timestamp(normalized_outcome.get("occurred_at"), field="outcome.occurred_at")
            evidence_refs = _safe_evidence_refs(
                normalized_outcome.get("evidence_refs"),
                field="outcome.evidence_refs",
            )
            binding = self.runtime.capabilities.binding_context(
                binding_id,
                runtime_scope=runtime_scope,
                capability_scope=capability_scope,
                at_time=occurred_at,
            )
        except (AdapterCapabilityError, CapabilityContractError, RuntimeError, TypeError, ValueError):
            return self._persistence_result("unclassified", "binding_context_unavailable")
        descriptor = binding.get("descriptor") if isinstance(binding, Mapping) else {}
        if (
            not isinstance(descriptor, Mapping)
            or str(binding.get("status") or "") != "active"
            or str(descriptor.get("capability_revision_id") or "") != revision_id
        ):
            return self._persistence_result("unclassified", "binding_context_mismatch")
        capability_id = str(descriptor.get("capability_id") or "").strip()
        if not capability_id:
            return self._persistence_result("unclassified", "binding_capability_identity_missing")
        verifier = dict(independent_verifier)
        verifier_passed = verifier.get("passed")
        if not isinstance(verifier_passed, bool) or verifier_passed != (verdict == "pass"):
            return self._persistence_result("unclassified", "verifier_verdict_mismatch")
        diagnostics = sanitize_diagnostic_metadata(
            environment_fingerprint,
            field="environment_fingerprint",
            max_bytes=MAX_OUTCOME_DIAGNOSTIC_BYTES,
        )
        safe_provenance = sanitize_diagnostic_metadata(
            provenance,
            field="provenance",
            max_bytes=MAX_OUTCOME_DIAGNOSTIC_BYTES,
        )
        if not diagnostics or not safe_provenance:
            return self._persistence_result("unclassified", "sanitized_context_empty")
        attribution = {
            "capability_id": capability_id,
            "capability_revision_id": revision_id,
            "provider_binding_id": binding_id,
            "idempotency_key": f"adapter:{self.adapter_id}:{event_id}:{normalized_outcome.get('outcome_digest') or ''}",
            "observed_at": occurred_at,
            "evidence_refs": list(evidence_refs),
            "environment_fingerprint": diagnostics,
            "provenance": {
                **safe_provenance,
                "adapter_id": self.adapter_id,
                "adapter_outcome_digest": str(normalized_outcome.get("outcome_digest") or ""),
                "binding_digest": str(binding.get("entity_digest") or ""),
            },
        }
        from eimemory.capabilities.observations import CapabilityObservations

        try:
            result = CapabilityObservations(self.runtime.store).normalize_outcome(
                {
                    "capability_attribution": attribution,
                    "verifier": verifier,
                    "verdict": verdict,
                    "adapter_outcome": dict(normalized_outcome),
                },
                runtime_scope=runtime_scope,
                capability_scope=capability_scope,
                request_key=f"adapter-capability-observation:{self.adapter_id}:{event_id}",
            )
        except (CapabilityContractError, RuntimeError, TypeError, ValueError):
            return self._persistence_result("unclassified", "observation_persistence_rejected")
        payload = result.to_dict()
        return {
            "schema_version": ADAPTER_CAPABILITY_OUTCOME_SCHEMA_VERSION,
            "ok": payload.get("status") == "recorded",
            "status": str(payload.get("status") or "unclassified"),
            "reason": str(payload.get("reason") or ""),
            "adapter_id": self.adapter_id,
            "binding_id": binding_id,
            "capability_revision_id": revision_id,
            "capability_id": capability_id,
            "observation": payload.get("observation"),
        }

    def record_verified_capability_outcome(
        self,
        host_event: Mapping[str, Any] | None,
        *,
        runtime_scope: Mapping[str, Any],
        independent_verifier: Mapping[str, Any],
        environment_fingerprint: Mapping[str, Any],
        provenance: Mapping[str, Any],
        event_type: str = "",
        capability_scope: str = "global",
    ) -> dict[str, Any]:
        """Normalize and persist one independently verified adapter outcome.

        This is the sole convenience path for adapter hosts that possess a
        separate verifier.  It deliberately does not infer an outcome from a
        generic lifecycle event: the event must first satisfy the declared
        binding advertisement and then the resulting pass/fail record must be
        corroborated by the supplied independent verifier.
        """

        normalized = self.normalize_capability_outcome(
            host_event,
            runtime_scope=runtime_scope,
            event_type=event_type,
            capability_scope=capability_scope,
        )
        if normalized.get("ok") is not True:
            return self._persistence_result(
                "unclassified",
                str(normalized.get("reason") or "outcome_normalization_rejected"),
            )
        return self.persist_normalized_capability_outcome(
            normalized,
            runtime_scope=runtime_scope,
            independent_verifier=independent_verifier,
            environment_fingerprint=environment_fingerprint,
            provenance=provenance,
            capability_scope=capability_scope,
        )

    def _assert_fresh(self, advertisement: AdapterCapabilityAdvertisement, *, now: str) -> None:
        reference = datetime.fromisoformat(now.replace("Z", "+00:00"))
        advertised = datetime.fromisoformat(advertisement.advertised_at.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(advertisement.expires_at.replace("Z", "+00:00"))
        if advertisement.status != "active":
            raise AdapterCapabilityError("advertisement_stale")
        if expires <= reference:
            raise AdapterCapabilityError("advertisement_stale")
        if advertised < reference - timedelta(seconds=self.max_advertisement_age_seconds):
            raise AdapterCapabilityError("advertisement_stale")
        if advertised > reference + timedelta(seconds=MAX_ADVERTISEMENT_FUTURE_SKEW_SECONDS):
            raise AdapterCapabilityError("advertisement_schema_invalid")
        if expires - advertised > timedelta(seconds=MAX_ADVERTISEMENT_TTL_SECONDS):
            raise AdapterCapabilityError("advertisement_schema_invalid")

    def _assert_signature(self, advertisement: AdapterCapabilityAdvertisement) -> None:
        if not self.require_signature:
            return
        if not advertisement.signature or self.signature_verifier is None:
            raise AdapterCapabilityError("advertisement_signature_required")
        try:
            verified = self.signature_verifier(advertisement)
        except Exception as exc:  # noqa: BLE001 - no verifier internals cross the adapter boundary
            raise AdapterCapabilityError("advertisement_signature_invalid") from exc
        if verified is not True:
            raise AdapterCapabilityError("advertisement_signature_invalid")

    def _persistence_result(self, status: str, reason: str) -> dict[str, Any]:
        return {
            "schema_version": ADAPTER_CAPABILITY_OUTCOME_SCHEMA_VERSION,
            "ok": False,
            "status": status,
            "adapter_id": self.adapter_id,
            "reason": reason,
        }

    def _unsupported(self, event_type: str, reason: str) -> dict[str, Any]:
        return UnsupportedCapabilityOutcome(
            adapter_id=self.adapter_id,
            event_type=_safe_event_type(event_type),
            reason=reason,
        ).to_dict()


def advertise_capabilities(
    runtime: Any,
    *,
    adapter_id: str,
    runtime_scope: Mapping[str, Any],
    adapter_context: Mapping[str, Any],
    provider_kind: str = "",
    require_signature: bool = False,
    signature_verifier: AdvertisementSignatureVerifier | None = None,
    now: str = "",
) -> dict[str, Any]:
    """Convenience protocol function used by thin adapter surfaces."""

    return AdapterCapabilityService(
        runtime,
        adapter_id=adapter_id,
        provider_kind=provider_kind,
        require_signature=require_signature,
        signature_verifier=signature_verifier,
    ).advertise_capabilities(adapter_context, runtime_scope=runtime_scope, now=now)


__all__ = [
    "ADAPTER_CAPABILITY_OUTCOME_SCHEMA_VERSION",
    "ADAPTER_CAPABILITY_RECEIPT_SCHEMA_VERSION",
    "AdapterCapabilityError",
    "AdapterCapabilityService",
    "AdvertisementSignatureVerifier",
    "DEFAULT_ADVERTISEMENT_TTL_SECONDS",
    "MAX_ADVERTISEMENT_TTL_SECONDS",
    "NormalizedCapabilityOutcome",
    "UnsupportedCapabilityOutcome",
    "advertise_capabilities",
    "sanitize_diagnostic_metadata",
]
