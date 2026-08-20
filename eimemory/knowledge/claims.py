from __future__ import annotations

import hashlib
from typing import Any

from eimemory.knowledge.capabilities import normalize_capability_context
from eimemory.models.claim_cards import ClaimCard


def build_claim_cards(
    *,
    paper_source_id: str,
    paper_extract_id: str,
    sentences: list[str],
    provenance: dict[str, Any] | None = None,
    capability_context: dict[str, Any] | None = None,
) -> tuple[ClaimCard, ...]:
    # Capability attribution is explicit metadata only.  Do not infer a
    # capability from the claim wording or auto-register a knowledge link.
    normalized_capability_context = normalize_capability_context(capability_context)
    base_provenance = dict(provenance or {})
    if normalized_capability_context:
        base_provenance["capability_context"] = dict(normalized_capability_context)
    metadata = {"capability_context": dict(normalized_capability_context)} if normalized_capability_context else {}
    claims: list[ClaimCard] = []
    for sentence in sentences:
        claim_type = classify_claim(sentence)
        if claim_type == "background" and len(claims) >= 1:
            continue
        if len(sentence.split()) < 3:
            continue
        claims.append(
            ClaimCard(
                claim_card_id=_stable_id("claim", paper_source_id, sentence),
                paper_source_id=paper_source_id,
                paper_extract_id=paper_extract_id,
                claim_text=sentence,
                claim_type=claim_type,
                evidence_text=sentence,
                confidence=0.72 if claim_type in {"finding", "method", "limitation"} else 0.58,
                metadata=metadata,
                provenance=base_provenance,
            )
        )
    if not claims and sentences:
        sentence = max(sentences, key=len)
        claims.append(
            ClaimCard(
                claim_card_id=_stable_id("claim", paper_source_id, sentence),
                paper_source_id=paper_source_id,
                paper_extract_id=paper_extract_id,
                claim_text=sentence,
                claim_type="summary",
                evidence_text=sentence,
                confidence=0.5,
                metadata=metadata,
                provenance=base_provenance,
            )
        )
    return tuple(claims)


def classify_claim(sentence: str) -> str:
    lowered = sentence.lower()
    if "limitation" in lowered or "only " in lowered or "requires " in lowered:
        return "limitation"
    if "method" in lowered or "we propose" in lowered or "approach" in lowered:
        return "method"
    if any(marker in lowered for marker in ["shows", "improves", "outperforms", "reduces", "increases"]):
        return "finding"
    return "background"


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
