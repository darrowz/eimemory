from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


LIVING_MEMORY_META_KEY = "living_memory_v1"
LIVING_MEMORY_SCHEMA_VERSION = "living_memory.v1"


TEMPORAL_FIELDS: tuple[str, ...] = (
    "occurred_at",
    "valid_from",
    "valid_until",
    "life_phase",
    "temporal_distance",
    "recurrence",
    "supersedes",
    "anticipates",
    "unresolved_until",
)
MOTIVE_FIELDS: tuple[str, ...] = ("motive", "fear", "desire", "boundary", "trust_delta")
AFFECTIVE_FIELDS: tuple[str, ...] = (
    "valence",
    "arousal",
    "pressure",
    "frustration_repeat",
    "trust_building",
    "repair_needed",
)
PERSPECTIVE_FIELDS: tuple[str, ...] = ("self_view", "observer_view", "compassion_reframe")
ACTION_POSTURE_FIELDS: tuple[str, ...] = (
    "recommended",
    "friction",
    "naturalness",
    "urgency",
    "reversibility",
    "trust_risk",
    "ripeness",
)
ACTION_POSTURES: tuple[str, ...] = ("act", "nudge", "wait", "let_go")


def default_living_memory_meta() -> dict[str, Any]:
    """Return a fresh living_memory.v1 payload with conservative defaults."""
    return {
        "schema_version": LIVING_MEMORY_SCHEMA_VERSION,
        "temporal": {
            "occurred_at": "",
            "valid_from": "",
            "valid_until": "",
            "life_phase": "unspecified",
            "temporal_distance": "unspecified",
            "recurrence": "none",
            "supersedes": [],
            "anticipates": [],
            "unresolved_until": "",
        },
        "motive": {
            "motive": "unspecified",
            "fear": "",
            "desire": "",
            "boundary": [],
            "trust_delta": 0,
        },
        "affective": {
            "valence": "neutral",
            "arousal": "low",
            "pressure": "normal",
            "frustration_repeat": False,
            "trust_building": False,
            "repair_needed": False,
        },
        "perspective": {
            "self_view": "The memory records a user signal without enough context to infer a specific stance.",
            "observer_view": "A later observer should treat this as neutral context unless paired with stronger evidence.",
            "compassion_reframe": "Respond with care and avoid over-interpreting a sparse memory.",
        },
        "action_posture": {
            "recommended": "wait",
            "friction": "unknown",
            "naturalness": "neutral",
            "urgency": "normal",
            "reversibility": "medium",
            "trust_risk": "low",
            "ripeness": "low",
        },
    }


def enrich_living_memory(record_or_text: Any, *, meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build deterministic living_memory.v1 metadata from text or a RecordEnvelope-like object."""
    text = _record_text(record_or_text)
    source_meta = _record_meta(record_or_text, meta=meta)
    lowered = text.lower()

    living = default_living_memory_meta()
    _apply_temporal(living, record_or_text, source_meta, lowered)
    _apply_motive(living, lowered)
    _apply_affective(living, lowered)
    _apply_action_posture(living, lowered)
    _apply_perspective(living)
    return living


def enrich_living_memory_meta(record_or_text: Any, *, meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a metadata fragment containing living_memory_v1."""
    return {LIVING_MEMORY_META_KEY: enrich_living_memory(record_or_text, meta=meta)}


def get_living_memory_meta(record_or_meta: Any) -> dict[str, Any]:
    """Read living metadata from a record or metadata mapping, returning safe defaults when absent."""
    meta = _meta_mapping(record_or_meta)
    existing = meta.get(LIVING_MEMORY_META_KEY) if isinstance(meta, Mapping) else None
    if not isinstance(existing, Mapping):
        return default_living_memory_meta()
    return _merge_living_defaults(existing)


def has_living_memory_meta(record_or_meta: Any) -> bool:
    """Return True only when a record or metadata mapping explicitly carries living metadata."""
    meta = _meta_mapping(record_or_meta)
    return isinstance(meta.get(LIVING_MEMORY_META_KEY), Mapping) if isinstance(meta, Mapping) else False


def with_living_memory_meta(
    record_or_meta: Any,
    living_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copied metadata dict with living_memory_v1 attached."""
    meta = dict(_meta_mapping(record_or_meta))
    if living_meta is None:
        living = enrich_living_memory(record_or_meta, meta=meta)
    else:
        living = _merge_living_defaults(living_meta)
    meta[LIVING_MEMORY_META_KEY] = living
    return meta


def write_living_memory_meta(
    record_or_meta: Any,
    living_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Alias for callers that prefer explicit read/write helper naming."""
    return with_living_memory_meta(record_or_meta, living_meta=living_meta)


def _merge_living_defaults(existing: Mapping[str, Any]) -> dict[str, Any]:
    merged = default_living_memory_meta()
    for key, value in existing.items():
        if key in {"temporal", "motive", "affective", "perspective", "action_posture"} and isinstance(value, Mapping):
            merged[key].update(dict(value))
        else:
            merged[str(key)] = deepcopy(value)
    merged["schema_version"] = LIVING_MEMORY_SCHEMA_VERSION
    return merged


def _apply_temporal(
    living: dict[str, Any],
    record_or_text: Any,
    meta: Mapping[str, Any],
    lowered: str,
) -> None:
    occurred_at = _first_text(
        _nested_get(record_or_text, "time", "occurred_at"),
        meta.get("occurred_at"),
        meta.get("valid_from"),
    )
    if occurred_at:
        living["temporal"]["occurred_at"] = occurred_at
        living["temporal"]["valid_from"] = occurred_at
    if _has_any(lowered, ("tomorrow", "next ", "later", "soon", "future", "will ")):
        living["temporal"]["temporal_distance"] = "future"
        living["temporal"]["future_intent"] = {
            "status": "open",
            "intent": _compact_source_text(record_or_text),
        }
    elif _has_any(lowered, ("yesterday", "last ", "previous", "before", "earlier")):
        living["temporal"]["temporal_distance"] = "past"
    elif _has_any(lowered, ("now", "today", "currently")):
        living["temporal"]["temporal_distance"] = "present"
    if _has_any(lowered, ("let go", "no longer", "obsolete", "drop this", "ignore old")):
        living["temporal"]["temporal_distance"] = "stale"
        living["temporal"]["state"] = "stale"
    if _has_any(lowered, ("again", "repeat", "repeated", "always", "every time", "keeps ")):
        living["temporal"]["recurrence"] = "recurring"
    if _has_any(lowered, ("until ", "before proceeding", "unresolved", "repair this")):
        living["temporal"]["unresolved_until"] = "resolved_by_followup"


def _apply_motive(living: dict[str, Any], lowered: str) -> None:
    boundaries: list[str] = []
    if _has_any(lowered, ("no fluff", "without fluff", "avoid fluff", "straight to the point", "filler")):
        boundaries.append("no_fluff")
    if _has_any(lowered, ("boundary", "do not", "don't", "dont", "stop ")):
        boundaries.append("respect_boundary")
    if boundaries:
        living["motive"]["boundary"] = sorted(set(boundaries))

    if _has_any(lowered, ("concise", "brief", "short", "no fluff", "straight to the point", "efficient")):
        living["motive"]["motive"] = "efficiency"
        living["motive"]["desire"] = "efficient_assistance"
    if _has_any(lowered, ("trust", "repair", "ignored", "broke", "broken")):
        living["motive"]["fear"] = "trust_or_boundary_failure"
    if _has_any(lowered, ("good", "helped", "works", "worked", "thanks", "thank you", "keep doing", "builds trust")):
        living["motive"]["trust_delta"] = 1
        if not living["motive"]["desire"]:
            living["motive"]["desire"] = "reinforce_helpful_behavior"
    if _has_any(lowered, ("broke my trust", "broken trust", "lost trust", "trust failure", "ignored the boundary", "repair this")):
        living["motive"]["trust_delta"] = -1
        living["motive"]["motive"] = "trust_repair"
        living["motive"]["desire"] = "repair_trust"


def _apply_affective(living: dict[str, Any], lowered: str) -> None:
    negative = _has_any(
        lowered,
        ("frustrated", "frustrating", "angry", "upset", "broke my trust", "ignored", "failure", "wrong"),
    )
    positive = _has_any(lowered, ("good", "helped", "works", "worked", "thanks", "thank you", "builds trust"))
    if negative:
        living["affective"]["valence"] = "negative"
        living["affective"]["arousal"] = "medium"
    if positive and not negative:
        living["affective"]["valence"] = "positive"
    if _has_any(lowered, ("urgent", "now", "pressure", "must", "before proceeding")):
        living["affective"]["pressure"] = "elevated"
    if _has_any(lowered, ("again", "repeat", "repeated", "keeps ", "always")):
        living["affective"]["frustration_repeat"] = True
    if positive:
        living["affective"]["trust_building"] = True
    if _has_any(lowered, ("repair", "apologize", "broke my trust", "broken trust", "trust failure", "ignored the boundary")):
        living["affective"]["repair_needed"] = True


def _apply_action_posture(living: dict[str, Any], lowered: str) -> None:
    motive = living["motive"]["motive"]
    repair_needed = bool(living["affective"]["repair_needed"])
    trust_delta = int(living["motive"]["trust_delta"] or 0)
    boundaries = set(living["motive"]["boundary"])

    if _has_any(lowered, ("let go", "no longer", "obsolete", "drop this", "ignore old")):
        living["action_posture"]["recommended"] = "let_go"
        living["action_posture"]["friction"] = "low"
        living["action_posture"]["naturalness"] = "release"
        living["action_posture"]["urgency"] = "normal"
        living["action_posture"]["ripeness"] = "high"
        return
    if motive == "efficiency":
        living["action_posture"]["recommended"] = "act"
        living["action_posture"]["naturalness"] = "concise"
        living["action_posture"]["friction"] = "low"
        living["action_posture"]["ripeness"] = "high"
    if "respect_boundary" in boundaries:
        living["action_posture"]["reversibility"] = "low"
    if repair_needed or trust_delta < 0:
        living["action_posture"]["recommended"] = "act"
        living["action_posture"]["urgency"] = "high"
        living["action_posture"]["trust_risk"] = "high"
        living["action_posture"]["friction"] = "high"
        living["action_posture"]["ripeness"] = "high"
    elif trust_delta > 0:
        living["action_posture"]["recommended"] = "nudge"
        living["action_posture"]["trust_risk"] = "low"
        living["action_posture"]["friction"] = "low"
        living["action_posture"]["ripeness"] = "medium"


def _apply_perspective(living: dict[str, Any]) -> None:
    motive = str(living["motive"]["motive"])
    valence = str(living["affective"]["valence"])
    recommended = str(living["action_posture"]["recommended"])
    living["perspective"] = {
        "self_view": f"The user is signaling {motive} with {valence} emotional tone.",
        "observer_view": f"Treat this as a living-memory cue for {recommended}, not as a complete personality model.",
        "compassion_reframe": "Honor the signal while leaving room for context, repair, and future change.",
    }


def _record_text(record_or_text: Any) -> str:
    if isinstance(record_or_text, str):
        return record_or_text
    if isinstance(record_or_text, Mapping):
        return _join_text(
            (
                record_or_text.get("text"),
                record_or_text.get("title"),
                record_or_text.get("summary"),
                record_or_text.get("detail"),
            )
        )
    content = getattr(record_or_text, "content", None)
    content_text = ""
    if isinstance(content, Mapping):
        content_text = _first_text(content.get("text"), content.get("body"), content.get("raw_text"))
    return _join_text(
        (
            _first_text(getattr(record_or_text, "title", "")),
            _first_text(getattr(record_or_text, "summary", "")),
            _first_text(getattr(record_or_text, "detail", "")),
            content_text,
        )
    )


def _compact_source_text(record_or_text: Any) -> str:
    text = ""
    if isinstance(record_or_text, Mapping):
        content = record_or_text.get("content")
        if isinstance(content, Mapping):
            text = _first_text(content.get("text"), content.get("body"), content.get("raw_text"))
        text = text or _first_text(record_or_text.get("text"), record_or_text.get("summary"), record_or_text.get("detail"))
    else:
        content = getattr(record_or_text, "content", None)
        if isinstance(content, Mapping):
            text = _first_text(content.get("text"), content.get("body"), content.get("raw_text"))
        text = text or _first_text(getattr(record_or_text, "summary", ""), getattr(record_or_text, "detail", ""))
    text = text or _record_text(record_or_text).strip()
    return text[:240]


def _record_meta(record_or_text: Any, *, meta: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    if meta is not None:
        return meta
    return _meta_mapping(record_or_text)


def _meta_mapping(record_or_meta: Any) -> Mapping[str, Any]:
    if isinstance(record_or_meta, Mapping):
        return record_or_meta
    meta = getattr(record_or_meta, "meta", None)
    if isinstance(meta, Mapping):
        return meta
    return {}


def _nested_get(value: Any, *attrs: str) -> Any:
    current = value
    for attr in attrs:
        if isinstance(current, Mapping):
            current = current.get(attr)
        else:
            current = getattr(current, attr, None)
        if current is None:
            return None
    return current


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _join_text(values: tuple[Any, ...]) -> str:
    return " ".join(text for text in (_first_text(value) for value in values) if text)


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)
