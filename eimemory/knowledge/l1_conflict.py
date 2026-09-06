from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from eimemory.knowledge.l1_prompts import CONFLICT_DETECTION_SYSTEM_PROMPT
from eimemory.knowledge.sediment import L1Atom, L1_ATOM_TYPES
from eimemory.metadata import business_metadata


@dataclass(frozen=True, slots=True)
class ConflictDecision:
    action: str
    atom: L1Atom
    target_ids: tuple[str, ...] = ()
    merged_content: str = ""
    merged_type: str = ""


def find_conflict_candidates(memory_api: Any, atom: L1Atom, *, scope: dict, limit: int = 5) -> list[Any]:
    records = memory_api.store.search(query=atom.text, kinds=["memory"], scope=scope, limit=max(1, min(8, int(limit))))
    out = []
    for record in records:
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
    llm: object | None = None,
) -> list[ConflictDecision]:
    """Tencent two-phase: lexical/vector candidates, then LLM store/skip/update.

    No candidates or no LLM → store. Never Jaccard-scan the whole file.
    """

    if not atoms:
        return []
    matches: list[tuple[L1Atom, list[Any]]] = []
    for index, atom in enumerate(atoms):
        matches.append((atom, find_conflict_candidates(memory_api, atom, scope=scope)))
    if not any(candidates for _, candidates in matches) or llm is None:
        return [ConflictDecision(action="store", atom=atom) for atom in atoms]
    complete = getattr(llm, "complete", None)
    if not callable(complete):
        return [ConflictDecision(action="store", atom=atom) for atom in atoms]
    pool = []
    seen: set[str] = set()
    new_items = []
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
        new_items.append({"record_id": record_id, "type": atom.memory_type, "content": atom.text, "related_ids": related})
    user_prompt = json.dumps({"candidate_pool": pool, "new_memories": new_items}, ensure_ascii=False)
    try:
        result = complete(system_prompt=CONFLICT_DETECTION_SYSTEM_PROMPT, user_prompt=user_prompt, json_mode=True)
        payload = json.loads(str(getattr(result, "text", "") or "[]"))
    except Exception:
        return [ConflictDecision(action="store", atom=atom) for atom in atoms]
    if isinstance(payload, dict):
        payload = payload.get("items") or payload.get("decisions") or []
    if not isinstance(payload, list):
        return [ConflictDecision(action="store", atom=atom) for atom in atoms]
    by_id = {f"new-{index}": atom for index, atom in enumerate(atoms)}
    decisions: list[ConflictDecision] = []
    used: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        record_id = str(item.get("record_id") or "")
        atom = by_id.get(record_id)
        if atom is None or record_id in used:
            continue
        used.add(record_id)
        action = str(item.get("action") or "store").strip().lower()
        if action not in {"store", "skip", "update", "merge"}:
            action = "store"
        if action == "merge":
            action = "update"
        targets = tuple(str(value) for value in (item.get("target_ids") or []) if str(value).strip())
        merged_type = str(item.get("merged_type") or atom.memory_type).strip().lower()
        if merged_type not in L1_ATOM_TYPES:
            merged_type = atom.memory_type
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
            decisions.append(ConflictDecision(action="store", atom=atom))
    return decisions
