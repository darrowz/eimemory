from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from eimemory.core.clock import now_iso
from eimemory.identity import hongtu_identity_meta
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef


SourceExpansionEvaluator = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]

AUTONOMOUS_SOURCE = "eimemory.autonomous_source_expansion"
DEFAULT_MIN_SCORE = 0.7
DEFAULT_MAX_APPLY = 3

_CHATPAPER_BASE_URI = "https://www.chatpaper.ai/zh/dashboard/arxiv/cs/AI"
_CATEGORY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cs.RO", ("robot", "robotics", "embodied", "具身", "机器人", "navigation", "manipulation")),
    ("cs.CV", ("vision", "visual", "image", "video", "multimodal", "lvml", "lvlm", "视觉", "图像", "多模态")),
    ("cs.CL", ("language", "llm", "prompt", "dialogue", "conversation", "nlp", "语言", "提示词", "对话")),
    ("cs.LG", ("learning", "training", "reinforcement", "model", "policy", "学习", "训练", "强化学习")),
    ("cs.IR", ("retrieval", "search", "memory", "knowledge", "召回", "检索", "记忆", "知识")),
)
def run_autonomous_source_expansion(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    apply: bool = False,
    evaluator: SourceExpansionEvaluator | None = None,
    max_apply: int = DEFAULT_MAX_APPLY,
    min_score: float = DEFAULT_MIN_SCORE,
) -> dict[str, Any]:
    """Safely expand source coverage from memory gaps and source performance."""
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    scanned_at = now_iso()
    context = _build_context(runtime, scope=scope_ref)
    proposals = _build_proposals(runtime, context=context)
    effective_evaluator = evaluator or _env_llm_evaluator()
    approved: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    duplicate_count = 0
    applied: list[dict[str, Any]] = []
    audit_record_ids: list[str] = []
    remaining_apply_budget = max(0, int(max_apply))

    for proposal in proposals:
        if _proposal_is_duplicate(runtime, proposal):
            duplicate_count += 1
            continue

        evaluation = _evaluate_proposal(proposal, context=context, evaluator=effective_evaluator, min_score=min_score)
        applied_entry = None
        if evaluation["decision"] == "approve" and apply and remaining_apply_budget > 0:
            applied_entry = _apply_proposal(runtime, proposal, evaluation=evaluation, scanned_at=scanned_at)
            if applied_entry is not None:
                remaining_apply_budget -= 1
                applied.append(applied_entry)
        audited = _audit_proposal(
            runtime,
            proposal=proposal,
            evaluation=evaluation,
            scope=scope_ref,
            scanned_at=scanned_at,
            applied=applied_entry is not None,
        )
        if audited is not None:
            audit_record_ids.append(audited.record_id)

        if evaluation["decision"] != "approve":
            rejected.append({**proposal, "evaluation": evaluation})
            continue
        approved.append({**proposal, "evaluation": evaluation})

    return {
        "ok": True,
        "apply": bool(apply),
        "scanned_at": scanned_at,
        "proposal_count": len(proposals),
        "approved_count": len(approved),
        "rejected_count": len(rejected),
        "duplicate_count": duplicate_count,
        "applied_count": len(applied),
        "updated_source_ids": sorted({str(item["source_id"]) for item in applied if item.get("source_id")}),
        "audit_record_ids": audit_record_ids,
        "proposals": proposals,
        "approved": approved,
        "rejected": rejected,
        "applied": applied,
    }


def latest_autonomous_source_expansion(runtime: Any, *, scope: dict[str, Any] | ScopeRef | None = None, limit: int = 20) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    records = []
    offset = 0
    page_size = max(20, int(limit))
    while len(records) < int(limit):
        page = runtime.store.list_records(kinds=["source_candidate"], scope=scope_ref, limit=page_size, offset=offset)
        if not page:
            break
        records.extend(record for record in page if record.source == AUTONOMOUS_SOURCE)
        offset += len(page)
    return {
        "count": len(records),
        "latest": records[0].to_dict() if records else None,
        "recent": [record.to_dict() for record in records[:limit]],
    }


def _build_context(runtime: Any, *, scope: ScopeRef) -> dict[str, Any]:
    scope_payload = asdict(scope)
    policy = runtime.collection_policy(scope=scope_payload)
    sources = runtime.sources.list_sources(enabled=True)
    return {
        "scope": scope_payload,
        "gap_queries": list(policy.get("gap_queries") or []),
        "collection_policy": {
            "run_now": list(policy.get("run_now") or []),
            "pause": list(policy.get("pause") or []),
            "lower_frequency": list(policy.get("lower_frequency") or []),
        },
        "sources": [source.to_dict() for source in sources],
    }


def _build_proposals(runtime: Any, *, context: dict[str, Any]) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    chatpaper_sources = [source for source in runtime.sources.list_sources(enabled=True) if _is_chatpaper_source(source)]
    categories = _candidate_categories(context.get("gap_queries") or [])
    if not categories:
        return []

    if not chatpaper_sources:
        proposals.append(
            _proposal(
                action="add_chatpaper_source",
                source_id="src_auto_chatpaper_arxiv",
                source_kind="url",
                title="ChatPaper arXiv autonomous",
                uri=_CHATPAPER_BASE_URI,
                category=categories[0],
                reason="bootstrap trusted ChatPaper arXiv source for active research intake",
                evidence_gaps=context.get("gap_queries") or [],
            )
        )
        return proposals

    for source in chatpaper_sources:
        for category in categories:
            proposals.append(
                _proposal(
                    action="add_chatpaper_category",
                    source_id=source.source_id,
                    source_kind=source.source_kind,
                    title=source.title,
                    uri=source.uri,
                    category=category,
                    reason=f"expand ChatPaper arXiv coverage to {category}",
                    evidence_gaps=context.get("gap_queries") or [],
                )
            )
    return proposals


def _candidate_categories(gap_queries: list[str]) -> list[str]:
    scores: dict[str, int] = {}
    for gap in gap_queries:
        lowered = str(gap or "").lower()
        for category, hints in _CATEGORY_HINTS:
            if any(hint in lowered for hint in hints):
                scores[category] = scores.get(category, 0) + 1
    if not scores:
        return []
    return sorted(scores, key=lambda category: (-scores[category], category))


def _proposal(
    *,
    action: str,
    source_id: str,
    source_kind: str,
    title: str,
    uri: str,
    category: str,
    reason: str,
    evidence_gaps: list[str],
) -> dict[str, Any]:
    payload = {
        "action": action,
        "source_id": source_id,
        "source_kind": source_kind,
        "title": title,
        "uri": uri,
        "category": category,
        "reason": reason,
        "evidence_gaps": list(evidence_gaps)[:10],
        "source_family": "chatpaper_arxiv",
    }
    payload["proposal_id"] = "srcprop_" + sha256(
        "|".join([action, source_id, uri, category]).encode("utf-8")
    ).hexdigest()[:16]
    return payload


def _evaluate_proposal(
    proposal: dict[str, Any],
    *,
    context: dict[str, Any],
    evaluator: SourceExpansionEvaluator | None,
    min_score: float,
) -> dict[str, Any]:
    fallback = _deterministic_evaluation(proposal, context=context, min_score=min_score)
    if evaluator is None:
        return fallback
    try:
        llm_payload = evaluator(dict(proposal), dict(context))
    except Exception as exc:
        return {
            **fallback,
            "evaluator": "deterministic_after_llm_error",
            "llm_error": type(exc).__name__,
        }
    if not isinstance(llm_payload, dict):
        return {**fallback, "evaluator": "deterministic_after_malformed_llm"}

    llm_decision = str(llm_payload.get("decision") or "").strip().lower()
    llm_score = _float_or_none(llm_payload.get("score"))
    score = max(0.0, min(1.0, llm_score if llm_score is not None else float(fallback["score"])))
    if llm_decision not in {"approve", "reject", "needs_review"}:
        llm_decision = fallback["decision"]
    if llm_decision == "approve" and score < float(min_score):
        llm_decision = "needs_review"
    return {
        "decision": llm_decision,
        "score": round(score, 3),
        "reason": str(llm_payload.get("reason") or fallback["reason"]),
        "labels": [str(item) for item in (llm_payload.get("labels") or [])],
        "evaluator": "llm",
        "llm_provider": str(llm_payload.get("llm_provider") or ""),
        "llm_model": str(llm_payload.get("llm_model") or ""),
        "fallback_score": fallback["score"],
    }


def _deterministic_evaluation(
    proposal: dict[str, Any],
    *,
    context: dict[str, Any],
    min_score: float,
) -> dict[str, Any]:
    category = str(proposal.get("category") or "")
    gap_text = " ".join(str(item) for item in (context.get("gap_queries") or [])).lower()
    score = 0.68
    for matched_category, hints in _CATEGORY_HINTS:
        if matched_category == category and any(hint in gap_text for hint in hints):
            score = 0.84
            break
    if proposal.get("source_family") == "chatpaper_arxiv":
        score += 0.06
    score = min(0.95, score)
    decision = "approve" if score >= float(min_score) else "needs_review"
    return {
        "decision": decision,
        "score": round(score, 3),
        "reason": "deterministic source expansion score",
        "labels": ["deterministic", str(proposal.get("source_family") or "")],
        "evaluator": "deterministic",
    }


def _proposal_is_duplicate(runtime: Any, proposal: dict[str, Any]) -> bool:
    category = str(proposal.get("category") or "").strip()
    for source in runtime.sources.list_sources():
        if source.source_id != proposal.get("source_id"):
            continue
        categories = set(_source_categories(source))
        if proposal.get("action") == "add_chatpaper_category" and category in categories:
            return True
        if proposal.get("action") == "add_chatpaper_source":
            return True
    return False


def _apply_proposal(
    runtime: Any,
    proposal: dict[str, Any],
    *,
    evaluation: dict[str, Any],
    scanned_at: str,
) -> dict[str, Any] | None:
    action = str(proposal.get("action") or "")
    if action == "add_chatpaper_source":
        entry = runtime.sources.add_source(
            {
                "source_id": proposal["source_id"],
                "source_kind": "url",
                "title": proposal["title"],
                "uri": proposal["uri"],
                "enabled": True,
                "tags": ["arxiv", "autonomous", "chatpaper", "paper"],
                "metadata": {
                    "categories": [proposal["category"]],
                    "frequency": "daily",
                    "max_items": 10,
                    "autonomous_expansion": _expansion_metadata(scanned_at, [proposal["category"]], evaluation),
                },
            }
        )
        return {"source_id": entry.source_id, "category": proposal["category"], "action": action}

    if action != "add_chatpaper_category":
        return None
    for source in runtime.sources.list_sources():
        if source.source_id != proposal.get("source_id"):
            continue
        metadata = dict(source.metadata or {})
        categories = _source_categories(source)
        category = str(proposal.get("category") or "").strip()
        if category in categories:
            return None
        categories.append(category)
        metadata["categories"] = categories
        previous_expansion = metadata.get("autonomous_expansion") if isinstance(metadata.get("autonomous_expansion"), dict) else {}
        metadata["autonomous_expansion"] = {
            **previous_expansion,
            **_expansion_metadata(scanned_at, [category], evaluation),
            "applied_count": int(previous_expansion.get("applied_count") or 0) + 1,
        }
        entry = runtime.sources.add_source(
            {
                **source.to_dict(),
                "metadata": metadata,
            }
        )
        return {"source_id": entry.source_id, "category": category, "action": action}
    return None


def _expansion_metadata(scanned_at: str, categories: list[str], evaluation: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_run_at": scanned_at,
        "last_categories": list(categories),
        "last_score": evaluation.get("score"),
        "last_decision": evaluation.get("decision"),
        "last_reason": evaluation.get("reason"),
        "evaluator": evaluation.get("evaluator"),
    }


def _audit_proposal(
    runtime: Any,
    *,
    proposal: dict[str, Any],
    evaluation: dict[str, Any],
    scope: ScopeRef,
    scanned_at: str,
    applied: bool,
) -> RecordEnvelope | None:
    status = "active" if applied else ("rejected" if evaluation["decision"] == "reject" else "candidate")
    record = RecordEnvelope(
        record_id="srcauto_" + sha256(
            "|".join([proposal["proposal_id"], scanned_at, evaluation["decision"]]).encode("utf-8")
        ).hexdigest()[:16],
        kind="source_candidate",
        status=status,
        title=f"Autonomous source proposal: {proposal['category']}",
        summary=str(proposal.get("reason") or ""),
        detail=f"{proposal.get('action')} {proposal.get('source_id')} {proposal.get('category')}",
        content={
            "proposal": dict(proposal),
            "evaluation": dict(evaluation),
            "applied": bool(applied),
        },
        tags=["autonomous-source", "source-expansion", str(proposal.get("category") or "")],
        links=[],
        evidence=list(proposal.get("evidence_gaps") or [])[:10],
        source=AUTONOMOUS_SOURCE,
        scope=scope,
        time=TimeRef.now(),
        provenance={
            "proposal_id": proposal["proposal_id"],
            "scan_kind": "autonomous_source_expansion",
            "scanned_at": scanned_at,
        },
        meta={
            "proposal_id": proposal["proposal_id"],
            "source_id": proposal.get("source_id"),
            "source_kind": proposal.get("source_kind"),
            "category": proposal.get("category"),
            "evaluation": dict(evaluation),
            "applied": bool(applied),
            **hongtu_identity_meta(source=AUTONOMOUS_SOURCE, channel="eimemory", organ="memory", modality="text"),
        },
    )
    runtime.store.append(record)
    return record


def _is_chatpaper_source(source: Any) -> bool:
    lowered_uri = str(getattr(source, "uri", "") or "").lower()
    lowered_title = str(getattr(source, "title", "") or "").lower()
    lowered_tags = {str(item).lower() for item in (getattr(source, "tags", []) or [])}
    return "chatpaper.ai" in lowered_uri or "chatpaper" in lowered_title or "chatpaper" in lowered_tags


def _source_categories(source: Any) -> list[str]:
    categories: list[str] = []
    seen: set[str] = set()
    uri_category = _chatpaper_category_from_uri(str(getattr(source, "uri", "") or ""))
    if uri_category:
        categories.append(uri_category)
        seen.add(uri_category)
    metadata = getattr(source, "metadata", {}) or {}
    for item in metadata.get("categories") or []:
        category = str(item or "").strip()
        if not category or category in seen:
            continue
        seen.add(category)
        categories.append(category)
    return categories


def _chatpaper_category_from_uri(uri: str) -> str:
    parsed = urlparse(str(uri or ""))
    query_category = parse_qs(parsed.query).get("category", [""])[0]
    if query_category:
        return str(query_category).strip()
    parts = [part for part in parsed.path.split("/") if part]
    if "arxiv" not in parts:
        return ""
    index = parts.index("arxiv")
    if index + 2 < len(parts):
        return f"{parts[index + 1]}.{parts[index + 2]}"
    if index + 1 < len(parts) and "." in parts[index + 1]:
        return parts[index + 1]
    return ""


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _env_llm_evaluator() -> SourceExpansionEvaluator | None:
    from eimemory.llm import llm_client_from_env

    try:
        command_client = llm_client_from_env("SOURCE_EXPANSION")
    except (OSError, ValueError) as configuration_error:
        def fail_configured_evaluator(
            _proposal: dict[str, Any],
            _context: dict[str, Any],
            error: BaseException = configuration_error,
        ) -> dict[str, Any]:
            raise error

        return fail_configured_evaluator
    if command_client is not None:
        def evaluate_with_command(proposal: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
            prompt = _source_expansion_prompt(proposal, context)
            result = command_client.complete(
                system_prompt="You are a conservative evaluator for a memory system source registry.",
                user_prompt=json.dumps(prompt, ensure_ascii=False),
                json_mode=True,
            )
            payload = json.loads(_extract_json_object(result.text))
            if not isinstance(payload, dict):
                raise ValueError("LLM source expansion result must be an object")
            return {**payload, "llm_provider": result.provider_id, "llm_model": result.model_id}

        return evaluate_with_command

    base_url = (
        os.environ.get("EIMEMORY_SOURCE_EXPANSION_LLM_BASE_URL")
        or os.environ.get("EIMEMORY_LLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or ""
    ).rstrip("/")
    api_key = (
        os.environ.get("EIMEMORY_SOURCE_EXPANSION_LLM_API_KEY")
        or os.environ.get("EIMEMORY_LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()
    model = (
        os.environ.get("EIMEMORY_SOURCE_EXPANSION_LLM_MODEL")
        or os.environ.get("EIMEMORY_LLM_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "gpt-4.1-mini"
    ).strip()
    if not base_url or not api_key:
        return None

    endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"

    def evaluate(proposal: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        prompt = _source_expansion_prompt(proposal, context)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a conservative evaluator for a memory system source registry."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            "temperature": 0,
        }
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=20) as response:
            raw_bytes = response.read(200_001)
        if len(raw_bytes) > 200_000:
            raise ValueError("LLM source expansion response exceeds size limit")
        raw = raw_bytes.decode("utf-8", errors="replace")
        data = json.loads(raw)
        content = str(data["choices"][0]["message"]["content"])
        result = json.loads(_extract_json_object(content))
        if not isinstance(result, dict):
            raise ValueError("LLM source expansion result must be an object")
        return {
            **result,
            "llm_provider": "openai-compatible",
            "llm_model": str(data.get("model") or model),
        }

    return evaluate


def _source_expansion_prompt(proposal: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": "Evaluate whether EIMemory should autonomously expand a memory source.",
        "rules": [
            "Return strict JSON only.",
            "Approve only trusted public research/source expansions.",
            "Reject sources that look unsafe, spammy, off-topic, credential-bearing, or duplicate.",
            "Use decision approve, reject, or needs_review.",
            "Use score between 0 and 1.",
        ],
        "proposal": proposal,
        "context": {
            "gap_queries": list(context.get("gap_queries") or [])[:12],
            "collection_policy": context.get("collection_policy") or {},
        },
        "output_schema": {"decision": "approve|reject|needs_review", "score": 0.0, "reason": "short reason", "labels": []},
    }


def _extract_json_object(text: str) -> str:
    stripped = str(text or "").strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM response did not contain a JSON object")
    return stripped[start : end + 1]
