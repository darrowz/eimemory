from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EvidenceQuery:
    source: str
    query: str
    fact_fields: tuple[str, ...] = ()
    required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", str(self.source or "").strip())
        object.__setattr__(self, "query", str(self.query or "").strip())
        object.__setattr__(self, "fact_fields", _fact_fields_tuple(self.fact_fields))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "query": self.query,
            "fact_fields": list(self.fact_fields),
            "required": bool(self.required),
        }


def require_query_first(
    subject: str,
    queries: Sequence[EvidenceQuery | Mapping[str, Any] | str],
    *,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    query_items = [_coerce_query(item) for item in queries]
    evidence_by_source = evidence if isinstance(evidence, Mapping) else {}

    for query in query_items:
        if query.required and query.source not in evidence_by_source:
            return {
                "ok": False,
                "subject": str(subject or ""),
                "queries": [item.to_dict() for item in query_items],
                "evidence_sources": sorted(str(source) for source in evidence_by_source.keys()),
                "blocked_reason": f"missing_required_evidence:{query.source}",
                "facts": {},
            }

    facts: dict[str, Any] = {}
    for query in query_items:
        if query.source not in evidence_by_source:
            continue
        source_facts = _source_facts(evidence_by_source[query.source])
        fact_fields = query.fact_fields or tuple(source_facts.keys())
        for field in fact_fields:
            if field not in source_facts:
                continue
            value = source_facts[field]
            if _has_fact_value(value):
                facts[field] = value

    return {
        "ok": True,
        "subject": str(subject or ""),
        "queries": [item.to_dict() for item in query_items],
        "evidence_sources": sorted(str(source) for source in evidence_by_source.keys()),
        "blocked_reason": "",
        "facts": facts,
    }


def _coerce_query(value: EvidenceQuery | Mapping[str, Any] | str) -> EvidenceQuery:
    if isinstance(value, EvidenceQuery):
        return value
    if isinstance(value, Mapping):
        return EvidenceQuery(
            source=str(value.get("source") or ""),
            query=str(value.get("query") or ""),
            fact_fields=_fact_fields_tuple(value.get("fact_fields") or value.get("facts")),
            required=bool(value.get("required", True)),
        )
    text = str(value or "").strip()
    return EvidenceQuery(source=text, query=text)


def _source_facts(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    facts: dict[str, Any] = {}
    nested = value.get("facts")
    if isinstance(nested, Mapping):
        facts.update({str(key): item for key, item in nested.items()})
    for key, item in value.items():
        if key == "facts":
            continue
        facts.setdefault(str(key), item)
    return facts


def _fact_fields_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        field = value.strip()
        return (field,) if field else ()
    if isinstance(value, Sequence):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),) if str(value).strip() else ()


def _has_fact_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return bool(value)
    return True
