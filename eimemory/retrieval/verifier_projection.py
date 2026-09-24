"""Bounded extractive verifier windows over an already-authorized parent.

This changes visibility, NOT admission. No synthetic text, inferred aliases,
query-specific gold facts or extra records are added. Each quote must belong to
one contiguous presented window; offsets always address the original parent.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from .evidence_fragments import evidence_fragments, search_terms

POLICY = "verifier-parent-windows.v1"
MAX_PARENT_CHARS = 16_000
MAX_WINDOW_CHARS = 768
MAX_WINDOWS = 2


@dataclass(frozen=True)
class EvidenceWindow:
    start: int
    text: str

    @property
    def end(self) -> int:
        return self.start + len(self.text)


def project_evidence_windows(query: str, text: str, *, limit: int = MAX_WINDOW_CHARS) -> tuple[EvidenceWindow, ...]:
    """Present a query-ranked window plus independent head/tail context.

    Ranking affects only what the model can inspect. The model still must
    reject questions, hypotheticals, negations and merely related content.
    Windows never splice nonadjacent text into a new assertion.
    """
    if not isinstance(text, str) or type(limit) is not int or not 1 <= limit <= MAX_WINDOW_CHARS:
        raise ValueError("invalid_verifier_projection")
    body = text[:MAX_PARENT_CHARS]
    if len(body) <= limit:
        return (EvidenceWindow(0, body),)
    terms = set(search_terms(str(query or "")[:2048]))
    starts = {0, max(0, len(body) - limit)}
    for fragment in evidence_fragments(body):
        # Retain context before the matching sentence, not just its bare noun.
        starts.add(max(0, min(fragment["start"] - limit // 4, len(body) - limit)))
        starts.add(max(0, min(fragment["end"] - limit, len(body) - limit)))
    def rank(start: int) -> tuple[int, int]:
        coverage = len(terms & set(search_terms(body[start:start + limit])))
        return (-coverage, start)
    primary_start = min(starts, key=rank)
    primary = EvidenceWindow(primary_start, body[primary_start:primary_start + limit])
    # Preserve headings/subject context when the best evidence is later. When
    # the head wins, also offer the tail so a later qualification is not hidden.
    context_start = 0 if primary_start else len(body) - limit
    context = EvidenceWindow(context_start, body[context_start:context_start + limit])
    if context.start == primary.start:
        return (primary,)
    return (primary, context)


def locate_visible_quote(quote: str, windows: tuple[EvidenceWindow, ...]) -> tuple[int, int] | None:
    """Do not use parent.index(): an earlier invisible duplicate is not proof."""
    if not isinstance(quote, str) or not quote:
        return None
    for window in windows:
        offset = window.text.find(quote)
        if offset >= 0:
            start = window.start + offset
            return start, start + len(quote)
    return None


def projection_summary(parents, projections) -> dict[str, int]:
    return {
        "visible_candidate_count": sum(any(window.text for window in windows) for windows in projections),
        "visible_window_count": sum(len(windows) for windows in projections),
        "candidate_text_chars": sum(len(text) for _record, text in parents),
        "visible_text_chars": sum(len(window.text) for windows in projections for window in windows),
        "windowed_candidate_count": sum(
            len(windows) > 1 or windows[0].start != 0 or len(windows[0].text) < len(text)
            for (_record, text), windows in zip(parents, projections)),
    }


def projection_trace(parents, projections) -> list[dict]:
    """Private debug provenance, never raw text, IDs, scopes or model output."""
    return [{"candidate_index": index,
             "parent_digest": sha256(text.encode("utf-8")).hexdigest(),
             "parent_chars": len(text),
             "windows": [{"start": window.start, "end": window.end,
                          "digest": sha256(window.text.encode("utf-8")).hexdigest()}
                         for window in windows]}
            for index, ((_record, text), windows) in enumerate(zip(parents, projections))]


def verified_parent_excerpt(record, proof) -> dict:
    """Reconstruct only a digest-checked selected span, not a ranked fragment."""
    from eimemory.contracts.recall_evidence import valid_proof
    from .postgres_vector import candidate_record_keyword_text
    if not valid_proof(proof) or proof['record_id'] != record.record_id:
        return {}
    start, end = proof['span_start'], proof['span_end']
    if end - start > MAX_WINDOW_CHARS:
        return {}
    parent = candidate_record_keyword_text(record, max_text_chars=MAX_PARENT_CHARS)
    quote = parent[start:end]
    if end > len(parent) or sha256(quote.encode('utf-8')).hexdigest() != proof['quote_digest']:
        return {}
    return {'evidence_excerpt':quote, 'evidence_span':[start, end],
            'evidence_projection':'verified-parent-span.v1'}
