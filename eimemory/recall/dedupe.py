"""Memory identity for recall dedupe.

``memory_content_key`` is exact full-payload identity. Callers must also
partition by scope/source ID.

``preference_paraphrase_key`` is a separate near-paraphrase identity for
preference-like memories. It collapses wording variants that share the same
modality roles or ordered steps, and deliberately does **not**:
- use dense cosine (dense-admission contract)
- merge opposite default/conditional roles
- merge opposite step orders
- merge versioned or differently sourced/statused payloads
"""
from __future__ import annotations

from hashlib import sha256
import json
import re

from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope


_PREFERENCE_TYPES = frozenset({
    "preference",
    "instruction",
    "persona",
    "user_preference",
    "operator_preference",
    "living_posture",
})

# These words bound supported grammatical wrappers only. They are never
# aliases: checking, reviewing, URLs, addresses and object modifiers can carry
# different constraints, so all semantic text remains literal in the key.
_DEFAULT_ACTION_STARTS = (
    "检查", "复核", "查看", "核对", "评估", "评价", "摘要", "总结", "概括",
    "读取", "断开", "取出", "关闭", "卸下", "更换", "抽",
)

_CLAUSE_SPLIT = re.compile(r"[；;。\n，,]")
_STEP_RE = re.compile(r"(先|再|然后|最后)(.*?)(?=先|再|然后|最后|$)")
_CONDITIONAL_MARKERS = ("只有", "明确要求", "除非")
_CONDITIONAL_RE = re.compile(r"(?:只有)?明确要求(.+?)(?:时)?才(?:提供|给)?(.+)")
_RECEIVED_DEFAULT_RE = re.compile(r"(?:收到|拿到)(.+)后(?:先给|先|提供|给)(.+)")


def memory_content_key(item: RecordEnvelope) -> str:
    # Projection truncation and token sets cannot establish fact equivalence.
    content = dict(item.content)
    if isinstance(content.get("text"), str):
        content["text"] = " ".join(content["text"].split())
    metadata = {
        key: value
        for key, value in business_metadata(item.meta).items()
        if key not in {"quality", "scoring"}
    }
    memory_type = str(metadata.get("memory_type") or content.get("memory_type") or "").lower()
    text = json.dumps(
        [
            item.kind,
            item.source,
            item.status,
            " ".join(item.title.split()),
            " ".join(item.summary.split()),
            item.detail,
            content,
            metadata,
            item.provenance,
            item.tags,
            [(link.relation, link.target_kind, link.target_id) for link in item.links],
            item.evidence,
            item.aliases,
            item.aliases_version,
            item.time.occurred_at if memory_type in {"event", "commitment"} else "",
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return sha256(text.encode("utf-8")).hexdigest()[:24]


def preference_paraphrase_key(item: RecordEnvelope) -> str | None:
    """Near-paraphrase identity for preference-like memories, or None.

    Exact duplicates still collapse via ``memory_content_key``. This key only
    applies when a preference body yields a structured fingerprint.
    """
    if item.kind != "memory":
        return None
    content = item.content if isinstance(item.content, dict) else {}
    metadata = business_metadata(item.meta)
    memory_type = str(
        metadata.get("memory_type") or content.get("memory_type") or ""
    ).strip().lower()
    if memory_type not in _PREFERENCE_TYPES:
        return None
    body = _preference_body(item)
    fingerprint = _preference_fingerprint(body)
    if fingerprint is None:
        return None
    version = _preference_version_token(item, content=content, metadata=metadata)
    payload = {
        "kind": "preference_paraphrase.v3",
        "status": str(item.status or ""),
        "source": str(item.source or ""),
        "detail": " ".join(str(item.detail or "").split()),
        "version": version,
        "fingerprint": fingerprint,
        # Text equivalence cannot waive distinct structured constraints.
        "memory_type": memory_type,
        "content_context": {key: value for key, value in content.items()
                            if key not in {"text", "memory_type"}},
        "metadata_context": {key: value for key, value in metadata.items()
                             if key not in {"quality", "scoring", "memory_type"}},
        "provenance": item.provenance,
        "tags": item.tags,
        "links": [(link.relation, link.target_kind, link.target_id) for link in item.links],
        "evidence": item.evidence,
        "aliases": item.aliases,
        "aliases_version": item.aliases_version,
    }
    return "pp:" + sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]


def memory_dedupe_identities(item: RecordEnvelope) -> tuple[str, ...]:
    """Identities that may collapse result slots within a scope/source partition.

    Exact content identity always participates. Preference near-paraphrase
    identity is additive and never replaces exact identity.
    """
    identities: list[str] = []
    if item.kind == "memory":
        identities.append("exact:" + memory_content_key(item))
        paraphrase = preference_paraphrase_key(item)
        if paraphrase:
            identities.append(paraphrase)
    return tuple(identities)


def _preference_body(item: RecordEnvelope) -> str:
    content = item.content if isinstance(item.content, dict) else {}
    parts: list[str] = []
    for value in (item.title, item.summary, item.detail, content.get("text")):
        if isinstance(value, str) and value.strip():
            normalized = " ".join(value.split())
            if normalized and normalized not in parts:
                parts.append(normalized)
    return "；".join(parts)


def _preference_version_token(
    item: RecordEnvelope,
    *,
    content: dict,
    metadata: dict,
) -> str:
    for source in (content, metadata, item.meta if isinstance(item.meta, dict) else {}):
        if not isinstance(source, dict):
            continue
        for key in ("version", "preference_version", "schema_version", "revision"):
            value = source.get(key)
            if value is None or value == "":
                continue
            return str(value).strip()
    return ""


def _preference_fingerprint(text: str) -> list | None:
    """Infer identity only from fully covered, ordered instruction clauses.

    Unparsed titles, conditions and trailing constraints deliberately disable
    near dedupe. Exact-content identity remains available to every caller.
    """
    body = str(text or "").strip()
    clauses = [clause.strip() for clause in _CLAUSE_SPLIT.split(body) if clause.strip()]
    if not clauses:
        return None
    has_roles = any("默认" in clause or any(marker in clause for marker in _CONDITIONAL_MARKERS)
                    for clause in clauses)
    if has_roles:
        # Preserve the existing mixed-role/multi-step fail-closed contract.
        if len(list(_STEP_RE.finditer(body))) > 1:
            return None
        roles: list[list] = []
        for clause in clauses:
            conditional = _CONDITIONAL_RE.fullmatch(clause)
            if conditional:
                requested = _preference_atoms(conditional.group(1))
                response = _preference_atoms(conditional.group(2))
                if not requested or not response:
                    return None
                roles.append(["conditional", list(requested), list(response)])
                continue
            if clause.count("默认") != 1 or any(marker in clause for marker in _CONDITIONAL_MARKERS):
                return None
            context, action = clause.split("默认", 1)
            received = _RECEIVED_DEFAULT_RE.fullmatch(action)
            if received:
                if context.strip():
                    return None
                context = received.group(1)
                action = received.group(2)
                if not _starts_supported_action(action):
                    return None
            else:
                # Remove only a grammatical prefix, never a marker inside a
                # noun or an action (for example 后台 or 不提供).
                for prefix in ("先给", "先", "提供", "给"):
                    if action.startswith(prefix) and _starts_supported_action(action[len(prefix):]):
                        action = action[len(prefix):]
                        break
            # Retain the context/action binding, not merely their token union.
            context_atoms = _preference_atoms(context)
            action_atoms = _preference_atoms(action)
            if not action_atoms:
                return None
            roles.append(["default", list(context_atoms), list(action_atoms)])
        return ["roles", roles]

    steps: list[list] = []
    for clause in clauses:
        matches = list(_STEP_RE.finditer(clause))
        # Without an explicit clause boundary, a connector may be part of a
        # noun (such as 最后期限). Do not infer its grammatical role.
        if len(matches) != 1:
            return None
        cursor = 0
        for match in matches:
            if match.start() != cursor:
                return None
            connector, action = match.groups()
            if (not steps and connector != "先") or (steps and connector == "先"):
                return None
            atoms = _preference_atoms(action)
            if not atoms:
                return None
            steps.append(["first" if connector == "先" else "last" if connector == "最后" else "next",
                          list(atoms)])
            cursor = match.end()
        if cursor != len(clause):
            return None
    # A word bag cannot prove arbitrary instructions or English clauses equal.
    return ["steps", steps] if steps else None


def _starts_supported_action(text: str) -> bool:
    return str(text or "").strip().startswith(_DEFAULT_ACTION_STARTS)


def _preference_atoms(text: str) -> tuple[str, ...]:
    """Preserve every semantic word, modifier, case, sign and repetition.

    Only whitespace is normalized, as in the exact-content contract. General
    synonym tables and omitted object modifiers cannot prove equivalence.
    """
    value = " ".join(str(text or "").split())
    return (value,) if value else ()
