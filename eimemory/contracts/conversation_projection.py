"""Bounded verbatim conversation presentation, never relevance or fact proof."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from collections.abc import Mapping
import re
from typing import Any

from eimemory.contracts.recall_boundary import SCOPE_FIELDS
from eimemory.core.record_ids import is_valid_record_id


SCHEMA = "conversation-turn-span.v1"
MAX_PARENT_CHARS = 16_000
MAX_EXCERPT_CHARS = 768
MAX_TURN_CHARS = 1_024
_ROLE = re.compile(r"(?m)^(User|Assistant):[^\S\r\n]*")
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _assistant_span(text: Any) -> tuple[int, int] | None:
    if (not isinstance(text, str) or not text or len(text) > MAX_TURN_CHARS
            or "\x00" in text or "\ufffd" in text):
        return None
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeError:
        return None
    roles = list(_ROLE.finditer(text))
    # Only the runtime's single completed turn format is understood. Quoted,
    # inline or repeated role labels must not manufacture another boundary.
    if (len(roles) != 2 or roles[0].start() != 0
            or [role.group(1) for role in roles] != ["User", "Assistant"]):
        return None
    start, end = roles[1].end(), len(text)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if not 0 < end - start <= MAX_EXCERPT_CHARS:
        return None
    return start, end


def _physical_identity(ref: Any) -> tuple[str, ...] | None:
    if not isinstance(ref, Mapping) or set(ref) != {"record_id", "kind", "scope", "source_id", "version_digest"}:
        return None
    scope = ref.get("scope")
    if (ref.get("kind") != "memory" or not isinstance(ref.get("record_id"), str)
            or not is_valid_record_id(ref["record_id"])
            or not isinstance(scope, Mapping) or set(scope) != set(SCOPE_FIELDS)
            or not all(isinstance(scope[key], str) for key in SCOPE_FIELDS)
            or not isinstance(ref.get("source_id"), str) or not ref["source_id"]
            or not isinstance(ref.get("version_digest"), str)
            or _HEX64.fullmatch(ref["version_digest"]) is None):
        return None
    return (ref["record_id"], ref["kind"], *(scope[key] for key in SCOPE_FIELDS),
            ref["source_id"], ref["version_digest"])


@dataclass(frozen=True, slots=True)
class ConversationExcerpt:
    """A presentation span bound to one exact physical envelope and field."""
    identity: tuple[str, ...]
    parent_digest: str
    quote_digest: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        record_id, kind, tenant, agent, workspace, user, source, version = self.identity
        return {"schema": SCHEMA, "presentation_only": True, "parent_field": "content.text",
                "record_ref": {"record_id": record_id, "kind": kind,
                               "scope": dict(zip(SCOPE_FIELDS, (tenant, agent, workspace, user))),
                               "source_id": source, "version_digest": version},
                "parent_digest": self.parent_digest, "quote_digest": self.quote_digest,
                "span": [self.start, self.end]}

    def reconstruct(self, text: Any, record_ref: Any) -> str | None:
        span = _assistant_span(text)
        if (span is None or self.identity != _physical_identity(record_ref)
                or (self.start, self.end) != (0, len(text))
                or sha256(text.encode("utf-8")).hexdigest() != self.parent_digest):
            return None
        quote = text[self.start:self.end]
        return quote if sha256(quote.encode("utf-8")).hexdigest() == self.quote_digest else None

    @classmethod
    def from_dict(cls, payload: Any) -> ConversationExcerpt | None:
        if (not isinstance(payload, Mapping)
                or set(payload) != {"schema", "presentation_only", "parent_field", "record_ref", "parent_digest", "quote_digest", "span"}
                or payload.get("schema") != SCHEMA or payload.get("presentation_only") is not True
                or payload.get("parent_field") != "content.text"):
            return None
        identity = _physical_identity(payload.get("record_ref"))
        span = payload.get("span")
        if (identity is None or not isinstance(span, (list, tuple)) or len(span) != 2
                or any(type(value) is not int for value in span)
                or not 0 <= span[0] < span[1] <= MAX_PARENT_CHARS
                or span[1] - span[0] > MAX_TURN_CHARS
                or any(not isinstance(payload.get(key), str) or _HEX64.fullmatch(payload[key]) is None
                       for key in ("parent_digest", "quote_digest"))):
            return None
        return cls(identity, payload["parent_digest"], payload["quote_digest"], *span)


def project_conversation_answer(text: Any, record_ref: Any) -> ConversationExcerpt | None:
    """Keep a complete bounded turn, including User facts and Assistant reply.

    Consumers prefer evidence_excerpt over summary. Present the contiguous
    original turn so User facts and qualifications do not disappear from those
    consumers. Role text is presentation, not authenticated factual support.
    No query, model, synthesized separator or storage lookup participates.
    """
    span, identity = _assistant_span(text), _physical_identity(record_ref)
    if span is None or identity is None:
        return None
    start, end = 0, len(text)
    return ConversationExcerpt(identity, sha256(text.encode("utf-8")).hexdigest(),
                               sha256(text[start:end].encode("utf-8")).hexdigest(), start, end)
