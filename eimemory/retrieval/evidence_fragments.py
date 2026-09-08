"""Deterministic, extractive search projections; never new authoritative facts.

Offsets address the versioned parent keyword projection. No query, labels, model
output, aliases or inferred synonyms participate in fragmentation. The same
tokenizer is used for PostgreSQL document and query text (including Chinese).
"""
from __future__ import annotations

from hashlib import sha256
import re
import unicodedata

POLICY = "extractive-evidence-fragments.v1"
TOKENIZER = "unicode-cjk-bigrams.v1"
MAX_FRAGMENTS = 96
MAX_SPAN = 512
_TERMS = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+(?:[._-][a-z0-9]+)*")


def search_terms(text: str) -> tuple[str, ...]:
    terms = []
    for word in _TERMS.findall(unicodedata.normalize("NFKC", str(text)).lower()[:64000]):
        if "\u3400" <= word[0] <= "\u9fff":
            terms.extend(word[i:i + 2] for i in range(len(word) - 1))
            if len(word) == 1:
                terms.append(word)
        else:
            terms.append(word)
    return tuple(dict.fromkeys(terms))


def fts_document(text: str) -> str:
    return " ".join(search_terms(text))


def fts_query(text: str) -> str:
    # Terms contain no tsquery operators; use the identical document tokenizer.
    return " | ".join("'" + term + "'" for term in search_terms(text)[:128])


def evidence_fragments(text: str) -> list[dict]:
    text = str(text)[:64000]
    spans = []
    start = 0
    # Semicolons commonly bind a claim to a qualification/negation; keep them.
    for match in re.finditer(r"[。！？!?\n]+|(?<=\.)\s+", text):
        spans.append((start, match.end()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    result, seen = [], set()
    for start, end in spans:
        # Long sentences overlap; no head/tail splice can remove the middle.
        while start < end:
            stop = min(end, start + MAX_SPAN)
            body = text[start:stop]
            key = " ".join(body.split())
            if key and key not in seen:
                seen.add(key)
                result.append({"id": sha256(f"{POLICY}:{start}:{stop}:{body}".encode()).hexdigest(),
                               "start": start, "end": stop, "text": body})
            if stop == end:
                break
            start = stop - 64
    if len(result) > MAX_FRAGMENTS:
        # Highly fragmented input (lists/logs) falls back to complete overlapping
        # windows, not a silently truncated prefix of the first N sentences.
        result = []
        for start in range(0, len(text), MAX_SPAN - 64):
            stop = min(len(text), start + MAX_SPAN)
            body = text[start:stop]
            result.append({'id': sha256(f'{POLICY}:{start}:{stop}:{body}'.encode()).hexdigest(),
                           'start': start, 'end': stop, 'text': body})
            if stop == len(text):
                break
        if len(result) > MAX_FRAGMENTS:
            raise ValueError("evidence_fragment_bound")
    return result


def distinct_text(text: str) -> str:
    return "\n".join(fragment["text"].strip() for fragment in evidence_fragments(text))


def lexical_coverage(query: str, text: str) -> float:
    query_terms = set(search_terms(query))
    return len(query_terms & set(search_terms(text))) / len(query_terms) if query_terms else 0.0
