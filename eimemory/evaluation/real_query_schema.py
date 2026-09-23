"""Shared production real-query schema, digests, and pure feature helpers.

Extracted from real_query_gate to break import cycles (A2): dataset/label modules
can depend on this file without pulling the evaluation engine.
"""
from __future__ import annotations

from datetime import datetime, timezone
from ipaddress import IPv4Address
from hashlib import sha256
import json
import re
from typing import Any
from unicodedata import decimal as unicode_decimal, normalize as normalize_unicode

from eimemory.adapters.runtime.channel import SUPPORTED_RUNTIME_CHANNELS

PRODUCTION_REAL_QUERY_SCHEMA = "production_redacted_v1"

PRODUCTION_REAL_QUERY_REPORT_SCHEMA = "production_recall_gate.v1"

PRODUCTION_REAL_QUERY_POLICY = "production_recall_gate_policy.v4"

_PRIOR_PRODUCTION_REAL_QUERY_POLICY = "production_recall_gate_policy.v3"

PRODUCTION_REAL_QUERY_REQUIRED_CHANNELS = frozenset({"openclaw", "codex", "hermes"})

PRODUCTION_REAL_QUERY_DATASET_EVIDENCE_SCHEMA = "secure_dataset_fingerprint.v1"

PRODUCTION_RECALL_BOOTSTRAP_STATE_SCHEMA = "production_recall_bootstrap_state.v1"

PRIOR_HEALTH_SNAPSHOT_SCHEMA = "prior_health_snapshot.v1"

GROUND_TRUTH_RANKING_IDENTITY_SCHEMA = "production_ranking_identity.v2"

_SEMANTIC_RANKING_REF_RE = re.compile(r"^(?:gtr|memr)_[0-9a-f]{64}$")

_GROUND_TRUTH_EFFECTIVE_CONTENT_KEYS = frozenset(
    {
        "report_type",
        "priority",
        "must_use",
        "target_capability",
        "lesson_record_id",
        "replay_record_id",
    }
)

PRODUCTION_REAL_QUERY_TRUSTED_LABELERS = frozenset({"operator", "release_operator"})

PRODUCTION_REAL_QUERY_TRUSTED_COLLECTORS = frozenset({"production_capture", "proactive_audit_capture"})

PRODUCTION_REAL_QUERY_THRESHOLDS: dict[str, float] = {
    "recall_at_5": 0.90,
    "precision_at_5": 0.20,
    "mrr": 0.80,
    "ndcg_at_5": 0.80,
    "top1_stability": 0.90,
    "jaccard_at_5": 0.80,
    "latency_ms_p95": 3000.0,
    "peak_memory_bytes": 67_108_864.0,
}

PRODUCTION_REAL_QUERY_BASELINE_MARGINS: dict[str, float] = {
    "recall_at_5": 0.01,
    "precision_at_5": 0.01,
    "mrr": 0.01,
    "ndcg_at_5": 0.01,
    "top1_stability": 0.01,
    "jaccard_at_5": 0.0,
}

PRODUCTION_REAL_QUERY_DYNAMIC_KNOWLEDGE = {
    "jaccard_floor": 0.65,
    "minimum_top1_stability": 0.98,
    "recall_noninferiority_margin": 0.01,
}

_REAL_QUERY_MIN_ACTIVE_CHANNELS = 1

_REAL_QUERY_REQUIRED_PER_CHANNEL = 5

_REAL_QUERY_MIN_CASES = 15

_REAL_QUERY_MIN_LABELS = 15

_REAL_QUERY_RECALL_DEPTH = 64

_MAX_QUERY_TERMS = 16

_MAX_QUERY_TERM_CHARS = 64

_MAX_QUERY_FEATURE_CHARS = 512

_MAX_BASELINE_CHAIN_DEPTH = 8

_MAX_PRIOR_HEALTH_SNAPSHOT_BYTES = 64 * 1024

_RECALL_CORPUS_KINDS = ("memory", "claim_card", "knowledge_page", "reflection", "rule")

_LOW_SIGNAL_QUERY_TOKENS = frozenset(
    {
        "audit",
        "capture",
        "case",
        "collector",
        "codex",
        "default",
        "eimemory",
        "ground",
        "hermes",
        "memory",
        "openclaw",
        "production",
        "proactive",
        "query",
        "recall",
        "source",
        "truth",
        "when",
        "behavior",
    }
)

_DIGEST_KEYS = frozenset(
    {"dataset_digest", "engine_digest", "fusion_digest", "policy_digest", "result_digest"}
)

_RAW_FIELD_MARKERS = frozenset(
    {
        "query",
        "raw_query",
        "query_text",
        "conversation",
        "messages",
        "result_text",
        "returned_text",
        "body",
        "content",
        "secret",
        "password",
        "token",
        "api_key",
    }
)

_EMAIL_FEATURE_RE = re.compile(r"(?i)(?<![\w.+-])[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+(?![\w.-])")

_PHONE_FEATURE_RE = re.compile(r"(?<![A-Za-z0-9_])\+?[0-9](?:[\s().-]*[0-9]){7,14}(?![A-Za-z0-9_])")

_PHONE_CONTEXT_RE = re.compile(r"(?i)\b(?:phone|mobile|tel(?:ephone)?|contact)\s*[:=]?\s*$")

_NON_PHONE_NUMERIC_CONTEXT_RE = re.compile(
    r"(?i)\b(?:build|release|order|record|ticket|issue|commit|revision|version|id|identifier|"
    r"job|task|case|invoice|serial|reference|ref)\s*[:#=_-]?\s*$"
)

_CN_MOBILE_RE = re.compile(r"(?:(?:00)?86)?1[3-9][0-9]{9}")

_NANP_PHONE_RE = re.compile(r"1?[2-9][0-9]{2}[2-9][0-9]{6}")

_DATE_TIME_CANDIDATE_FORMATS = (
    (re.compile(r"(?:19|20)[0-9]{8}"), "%Y%m%d%H"),
    (re.compile(r"(?:19|20)[0-9]{10}"), "%Y%m%d%H%M"),
    (re.compile(r"(?:19|20)[0-9]{12}"), "%Y%m%d%H%M%S"),
    (re.compile(r"(?:19|20)[0-9]{2}-[0-9]{2}-[0-9]{2} [0-9]{2}"), "%Y-%m-%d %H"),
    (re.compile(r"(?:19|20)[0-9]{2}\.[0-9]{2}\.[0-9]{2} [0-9]{2}"), "%Y.%m.%d %H"),
)

_ACCESS_CREDENTIAL_RE = re.compile(
    r"(?i)(?:\b(?:authorization|password|passphrase|client[_-]?secret|access[_-]?token|refresh[_-]?token|"
    r"session[_-]?cookie|api[_-]?key|credential)s?\s*[:=]|\bbearer\s+|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\b(?:ghp|github_pat|sk)-[A-Za-z0-9_-]{8,}\b)"
)

_PERSON_PLACEHOLDER_RE = re.compile(r"(?i)^(?:person_ref:[a-z0-9][a-z0-9._-]{2,63}|\[person(?::[a-z0-9._-]{1,48})?\]|<person>)$")

_HONORIFIC_PERSON_IN_TEXT_RE = re.compile(
    r"(?<![A-Za-z])(?:Mr|Mrs|Ms|Miss|Dr|Prof)\.?\s+[A-Z][a-z]{1,30}"
    r"(?:\s+[A-Z][a-z]{1,30}){0,2}(?![A-Za-z])"
)

_CJK_TITLED_PERSON_IN_TEXT_RE = re.compile(r"[\u3400-\u9fff]{2,4}\s*(?:先生|女士|博士|老师)")

_KNOWN_NON_PERSON_ENTITY_RE = re.compile(r"(?i)(?<![A-Za-z])Dr\.?\s+Pepper(?![A-Za-z])")

def production_real_query_active_channel_contract(channel_counts: dict[str, int]) -> dict[str, Any]:
    counts = {channel: int(channel_counts.get(channel) or 0) for channel in sorted(SUPPORTED_RUNTIME_CHANNELS)}
    active_channels = [channel for channel, count in counts.items() if count > 0]
    # This contract certifies the observed dataset channels, not every product
    # the library supports. Empty datasets still fail the minimum evidence gate.
    required_channels = active_channels
    total_count = sum(counts.get(channel, 0) for channel in required_channels)
    blocked: list[str] = []
    if len(active_channels) < _REAL_QUERY_MIN_ACTIVE_CHANNELS:
        blocked.append("active_channel_coverage_missing")
    if any(counts.get(channel, 0) < _REAL_QUERY_REQUIRED_PER_CHANNEL for channel in required_channels):
        blocked.append("required_channel_coverage_missing")
    if total_count < _REAL_QUERY_MIN_CASES:
        blocked.append("minimum_case_count_missing")
    return {
        "ok": not blocked,
        "active_channels": active_channels,
        "per_channel_case_count": counts,
        "required_channels": required_channels,
        "required_per_channel": _REAL_QUERY_REQUIRED_PER_CHANNEL,
        "required_case_count": _REAL_QUERY_MIN_CASES,
        "required_label_count": _REAL_QUERY_MIN_LABELS,
        "required_per_active_channel": _REAL_QUERY_REQUIRED_PER_CHANNEL,
        "blocked_reasons": list(dict.fromkeys(blocked)),
    }

def production_real_query_policy_payload(
    *, policy_schema: str = PRODUCTION_REAL_QUERY_POLICY,
) -> dict[str, Any]:
    payload = {
        "schema": policy_schema,
        "thresholds": dict(PRODUCTION_REAL_QUERY_THRESHOLDS),
        "k": 5,
        "required_channels": sorted(PRODUCTION_REAL_QUERY_REQUIRED_CHANNELS),
        "required_per_channel": _REAL_QUERY_REQUIRED_PER_CHANNEL,
        "required_case_count": _REAL_QUERY_MIN_CASES,
        "required_label_count": _REAL_QUERY_MIN_LABELS,
    }
    if policy_schema in {PRODUCTION_REAL_QUERY_POLICY, _PRIOR_PRODUCTION_REAL_QUERY_POLICY}:
        payload.update(
            baseline_noninferiority_margins=dict(
                PRODUCTION_REAL_QUERY_BASELINE_MARGINS
            ),
            dynamic_knowledge_stability=dict(
                PRODUCTION_REAL_QUERY_DYNAMIC_KNOWLEDGE
            ),
        )
    if policy_schema == PRODUCTION_REAL_QUERY_POLICY:
        payload["required_channels"] = "observed_dataset_channels"
        payload["coverage_scope"] = "dataset_only_not_all_installed_channels"
    return payload

def production_real_query_policy_digest(
    *, policy_schema: str = PRODUCTION_REAL_QUERY_POLICY,
) -> str:
    return _stable_digest(
        production_real_query_policy_payload(policy_schema=policy_schema)
    )

def _sample_channel_counts(samples: list[Any]) -> dict[str, int]:
    counts = {channel: 0 for channel in SUPPORTED_RUNTIME_CHANNELS}
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        channel = str(sample.get("channel") or "")
        if channel in counts:
            counts[channel] += 1
    return counts

def _bounded_window(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    started = str(value.get("started_at") or "").strip()
    ended = str(value.get("ended_at") or "").strip()
    try:
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except ValueError:
        return None
    if start_dt.tzinfo is None or end_dt.tzinfo is None or start_dt >= end_dt:
        return None
    return {"started_at": started[:80], "ended_at": ended[:80]}

def _parse_bounded_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text or len(text) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None

def _secure_dataset_evidence(value: object) -> tuple[dict[str, Any], str]:
    if not isinstance(value, dict):
        return {}, "secure_dataset_fingerprint_missing"
    digest = str(value.get("digest") or value.get("sha256") or "").strip().lower()
    schema = str(value.get("schema") or value.get("schema_version") or "").strip()
    size = value.get("size")
    device = value.get("device")
    inode = value.get("inode")
    canonical_digest = str(value.get("canonical_digest") or "").strip().lower()
    source_canonical_digest = str(value.get("source_canonical_digest") or "").strip().lower()
    if (
        schema != PRODUCTION_REAL_QUERY_DATASET_EVIDENCE_SCHEMA
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
        or isinstance(device, bool)
        or not isinstance(device, int)
        or device < 0
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or inode < 0
        or (canonical_digest and (len(canonical_digest) != 64 or any(char not in "0123456789abcdef" for char in canonical_digest)))
        or (source_canonical_digest and (len(source_canonical_digest) != 64 or any(char not in "0123456789abcdef" for char in source_canonical_digest)))
    ):
        return {}, "secure_dataset_fingerprint_invalid"
    return {
        "schema": schema,
        "digest": digest,
        "size": size,
        "device": device,
        "inode": inode,
        **({"canonical_digest": canonical_digest} if canonical_digest else {}),
        **({"source_canonical_digest": source_canonical_digest} if source_canonical_digest else {}),
    }, ""

def _bounded_query_features(value: object) -> tuple[dict[str, Any], str]:
    if not isinstance(value, dict):
        return {"terms": [], "intent": "", "entities": [], "language": ""}, "query_features_invalid"
    if any(str(key).strip().lower() in _RAW_FIELD_MARKERS for key in value):
        return {"terms": [], "intent": "", "entities": [], "language": ""}, "query_features_not_redacted"
    allowed = {"terms", "intent", "entities", "language"}
    if any(str(key) not in allowed for key in value):
        return {"terms": [], "intent": "", "entities": [], "language": ""}, "query_features_not_redacted"
    terms = [str(item).strip() for item in list(value.get("terms") or []) if str(item).strip()]
    entities = [str(item).strip() for item in list(value.get("entities") or []) if str(item).strip()]
    intent = str(value.get("intent") or "").strip()
    language = str(value.get("language") or "").strip()
    all_values = [*terms, *entities, intent, language]
    unsafe = (
        not terms
        or len(terms) > _MAX_QUERY_TERMS
        or len(entities) > _MAX_QUERY_TERMS
        or any(len(item) > _MAX_QUERY_TERM_CHARS for item in all_values)
        or sum(len(item) for item in all_values) > _MAX_QUERY_FEATURE_CHARS
        or any(_looks_like_secret(item) for item in all_values)
        or any(_contains_person_entity(item) for item in [*entities, intent] if item)
        or _contains_person_entity(" ".join(terms))
    )
    frozen: dict[str, Any] = {"terms": terms[:_MAX_QUERY_TERMS]}
    if intent:
        frozen["intent"] = intent[:_MAX_QUERY_TERM_CHARS]
    if entities:
        frozen["entities"] = entities[:_MAX_QUERY_TERMS]
    if language:
        frozen["language"] = language[:16]
    return frozen, "query_features_not_redacted" if unsafe else ""

def production_real_query_feature_quality_reasons(value: object) -> list[str]:
    features, reason = _bounded_query_features(value)
    if reason:
        return [reason]
    terms: list[str] = []
    for key in ("terms", "entities"):
        for item in list(features.get(key) or []):
            terms.extend(_query_feature_tokens(str(item)))
    terms.extend(_query_feature_tokens(str(features.get("intent") or "")))
    informative = [
        term
        for term in terms
        if term not in _LOW_SIGNAL_QUERY_TOKENS
        and not term.isdigit()
        and not term.startswith(("prqp", "prqa", "prle", "pd"))
    ]
    if len(set(informative)) < 2:
        return ["query_features_low_signal"]
    return []

def _query_feature_tokens(value: str) -> list[str]:
    normalized = normalize_unicode("NFKC", str(value or "")).strip().casefold()
    if not normalized:
        return []
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", normalized)

def _looks_like_secret(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(
        _EMAIL_FEATURE_RE.search(text)
        or _looks_like_high_confidence_phone(text)
        or _ACCESS_CREDENTIAL_RE.search(text)
    )

def _looks_like_high_confidence_phone(value: str) -> bool:
    text = _normalize_decimal_digits(value)
    for match in _PHONE_FEATURE_RE.finditer(text):
        candidate = match.group(0)
        try:
            IPv4Address(candidate)
        except ValueError:
            pass
        else:
            continue
        digit_count = sum(character.isdigit() for character in candidate)
        if not 8 <= digit_count <= 15:
            continue
        if candidate.startswith("+"):
            return True
        if _looks_like_date_time_candidate(candidate):
            continue
        if digit_count >= 10 and re.search(r"\(\d{2,4}\)", candidate):
            return True
        if digit_count >= 10 and len(re.findall(r"[\s.-]+", candidate)) >= 2:
            return True
        normalized_digits = re.sub(r"[^0-9]", "", candidate)
        if _CN_MOBILE_RE.fullmatch(normalized_digits) or _NANP_PHONE_RE.fullmatch(normalized_digits):
            prefix = text[max(0, match.start() - 32) : match.start()]
            if _PHONE_CONTEXT_RE.search(prefix):
                return True
            if _NON_PHONE_NUMERIC_CONTEXT_RE.search(prefix):
                continue
            return True
        if _PHONE_CONTEXT_RE.search(text[max(0, match.start() - 24) : match.start()]):
            return True
    return False

def _normalize_decimal_digits(value: str) -> str:
    normalized = normalize_unicode("NFKC", str(value or "")).strip()
    characters: list[str] = []
    for character in normalized:
        try:
            characters.append(str(unicode_decimal(character)))
        except (TypeError, ValueError):
            characters.append(character)
    return "".join(characters)

def _looks_like_date_time_candidate(value: str) -> bool:
    candidate = " ".join(str(value or "").strip().split())
    for shape, date_format in _DATE_TIME_CANDIDATE_FORMATS:
        if not shape.fullmatch(candidate):
            continue
        try:
            datetime.strptime(candidate, date_format)
        except ValueError:
            continue
        return True
    return False

def _contains_person_entity(value: str) -> bool:
    text = " ".join(str(value or "").strip().split())
    if not text or _PERSON_PLACEHOLDER_RE.fullmatch(text):
        return False
    text = _KNOWN_NON_PERSON_ENTITY_RE.sub("", text)
    return bool(
        _HONORIFIC_PERSON_IN_TEXT_RE.search(text)
        or _CJK_TITLED_PERSON_IN_TEXT_RE.search(text)
    )

def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()

def _engine_identity_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()
