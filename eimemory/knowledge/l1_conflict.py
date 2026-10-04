from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from eimemory.knowledge.l1_prompts import CONFLICT_DETECTION_SYSTEM_PROMPT
from eimemory.knowledge.sediment import L1Atom, L1_ATOM_TYPES
from eimemory.metadata import business_metadata
from eimemory.models.records import ScopeRef


class L1ConflictJudgeUnavailable(RuntimeError):
    """A conflicting L1 candidate existed but the judge could not decide safely."""


def _normalize_conflict_action(value: Any, *, strict: bool) -> str:
    """Keep an explicit valid decision distinct from malformed judge output."""
    if strict:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise L1ConflictJudgeUnavailable("l1_conflict_action_missing")
        if not isinstance(value, str):
            raise L1ConflictJudgeUnavailable("l1_conflict_action_invalid")
        action = value.strip().lower()
        if action not in {"store", "skip", "update", "merge"}:
            raise L1ConflictJudgeUnavailable("l1_conflict_action_invalid")
    else:
        action = str(value or "store").strip().lower()
        if action not in {"store", "skip", "update", "merge"}:
            action = "store"
    return "update" if action == "merge" else action


@dataclass(frozen=True, slots=True)
class ConflictDecision:
    action: str
    atom: L1Atom
    target_ids: tuple[str, ...] = ()
    merged_content: str = ""
    merged_type: str = ""


def find_conflict_candidates(
    memory_api: Any,
    atom: L1Atom,
    *,
    scope: dict,
    source_id: str = "",
    exclude_record_ids: tuple[str, ...] | list[str] = (),
    limit: int = 5,
) -> list[Any]:
    scope_ref = ScopeRef.from_dict(scope)
    records = memory_api.store.search(
        query=atom.text,
        kinds=["memory"],
        scope=scope,
        limit=max(1, min(16, int(limit) * 2)),
        source_ids=[source_id] if source_id else None,
    )
    excluded = {str(value) for value in exclude_record_ids if str(value).strip()}
    out = []
    for record in records:
        if str(getattr(record, "record_id", "") or "") in excluded:
            continue
        if str(getattr(record, "status", "active") or "") != "active":
            continue
        record_scope = getattr(record, "scope", scope_ref)
        if record_scope != scope_ref:
            continue
        record_source_id = str(getattr(record, "source_id", source_id) or "")
        if source_id and record_source_id != source_id:
            continue
        meta = business_metadata(record.meta)
        layer = str(meta.get("memory_layer") or "").strip().lower()
        memory_type = str(meta.get("memory_type") or record.content.get("memory_type") or "")
        if layer == "l0":
            continue
        if memory_type not in L1_ATOM_TYPES | {"preference", "operator_preference", "instruction", "persona"}:
            continue
        out.append(record)
    return out[: max(1, min(5, int(limit)))]


def adjudicate_l1_atoms(
    memory_api: Any,
    atoms: list[L1Atom],
    *,
    scope: dict,
    source_id: str = "",
    llm: object | None = None,
    strict: bool = False,
    exclude_record_ids: tuple[str, ...] | list[str] = (),
) -> list[ConflictDecision]:
    """Tencent two-phase: lexical/vector candidates, then LLM store/skip/update.

    No candidates or no LLM → store. Never Jaccard-scan the whole file.
    """

    if not atoms:
        return []
    matches: list[tuple[L1Atom, list[Any]]] = []
    for index, atom in enumerate(atoms):
        matches.append((
            atom,
            find_conflict_candidates(
                memory_api,
                atom,
                scope=scope,
                source_id=source_id,
                exclude_record_ids=exclude_record_ids,
            ),
        ))
    if not any(candidates for _, candidates in matches):
        return [ConflictDecision(action="store", atom=atom) for atom in atoms]
    if llm is None:
        if strict:
            raise L1ConflictJudgeUnavailable("l1_conflict_llm_unavailable")
        return [ConflictDecision(action="store", atom=atom) for atom in atoms]
    complete = getattr(llm, "complete", None)
    if not callable(complete):
        if strict:
            raise L1ConflictJudgeUnavailable("l1_conflict_llm_invalid")
        return [ConflictDecision(action="store", atom=atom) for atom in atoms]
    pool = []
    seen: set[str] = set()
    new_items = []
    allowed_targets: dict[str, set[str]] = {}
    for index, (atom, candidates) in enumerate(matches):
        record_id = f"new-{index}"
        related = []
        for candidate in candidates:
            if candidate.record_id not in seen:
                seen.add(candidate.record_id)
                pool.append(
                    {
                        "id": candidate.record_id,
                        "type": business_metadata(candidate.meta).get("memory_type")
                        or candidate.content.get("memory_type"),
                        "content": candidate.summary or candidate.content.get("text") or candidate.title,
                    }
                )
            related.append(candidate.record_id)
        allowed_targets[record_id] = set(related)
        new_items.append({"record_id": record_id, "type": atom.memory_type, "content": atom.text, "related_ids": related})
    user_prompt = json.dumps({"candidate_pool": pool, "new_memories": new_items}, ensure_ascii=False)
    try:
        result = complete(system_prompt=CONFLICT_DETECTION_SYSTEM_PROMPT, user_prompt=user_prompt, json_mode=True)
        payload = json.loads(str(getattr(result, "text", "") or "[]"))
    except Exception as exc:
        if strict:
            raise L1ConflictJudgeUnavailable(
                f"l1_conflict_llm_failed:{type(exc).__name__}"
            ) from exc
        return [ConflictDecision(action="store", atom=atom) for atom in atoms]
    if isinstance(payload, dict):
        payload = payload.get("items") or payload.get("decisions") or []
    if not isinstance(payload, list):
        if strict:
            raise L1ConflictJudgeUnavailable("l1_conflict_invalid_output")
        return [ConflictDecision(action="store", atom=atom) for atom in atoms]
    by_id = {f"new-{index}": atom for index, atom in enumerate(atoms)}
    if strict:
        # Reject malformed actions before any candidate is marked decided.
        # Keep unrelated/unknown response IDs under the existing handling below.
        for item in payload:
            if isinstance(item, dict) and str(item.get("record_id") or "") in by_id:
                _normalize_conflict_action(item.get("action"), strict=True)
    decisions: list[ConflictDecision] = []
    used: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        record_id = str(item.get("record_id") or "")
        atom = by_id.get(record_id)
        if atom is None or record_id in used:
            continue
        action = _normalize_conflict_action(item.get("action"), strict=strict)
        requested_targets = tuple(
            str(value) for value in (item.get("target_ids") or []) if str(value).strip()
        )
        targets = tuple(
            value for value in requested_targets
            if value in allowed_targets.get(record_id, set())
        )
        if strict and len(targets) != len(requested_targets):
            raise L1ConflictJudgeUnavailable("l1_conflict_unauthorized_target")
        if action == "update" and not targets:
            if strict:
                raise L1ConflictJudgeUnavailable("l1_conflict_update_without_target")
            action = "store"
        merged_type = str(item.get("merged_type") or atom.memory_type).strip().lower()
        if merged_type not in L1_ATOM_TYPES:
            merged_type = atom.memory_type
        used.add(record_id)
        decisions.append(
            ConflictDecision(
                action=action,
                atom=atom,
                target_ids=targets,
                merged_content=str(item.get("merged_content") or atom.text).strip(),
                merged_type=merged_type,
            )
        )
    for index, atom in enumerate(atoms):
        if f"new-{index}" not in used:
            if strict and matches[index][1]:
                raise L1ConflictJudgeUnavailable("l1_conflict_missing_decision")
            decisions.append(ConflictDecision(action="store", atom=atom))
    return decisions
