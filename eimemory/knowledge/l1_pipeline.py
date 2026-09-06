from __future__ import annotations

from typing import Any

from eimemory.knowledge.l1_conflict import adjudicate_l1_atoms
from eimemory.knowledge.sediment import L1Atom, extract_l1_atoms
from eimemory.metadata import business_metadata
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef
from eimemory.recall.indexing import is_episode_evidence_record


def persist_l1_atoms(
    memory_api: Any,
    *,
    atoms: list[Any],
    episode_id: str,
    scope: dict | ScopeRef,
    channel_id: str,
    session_id: str = "",
    turn_id: str = "",
    llm: object | None = None,
) -> list[dict[str, Any]]:
    """Write L1 atoms after Tencent-style store/skip/update adjudication."""

    scope_dict = scope if isinstance(scope, dict) else {
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "workspace_id": scope.workspace_id,
        "user_id": scope.user_id,
    }
    typed_atoms = [atom for atom in atoms if isinstance(atom, L1Atom)]
    decisions = adjudicate_l1_atoms(memory_api, typed_atoms, scope=scope_dict, llm=llm)
    written: list[dict[str, Any]] = []
    for decision in decisions:
        if decision.action == "skip":
            continue
        atom = decision.atom
        text = decision.merged_content or atom.text
        memory_type = decision.merged_type or atom.memory_type
        fact = memory_api.ingest(
            text=text,
            memory_type=memory_type,
            title=text[:72],
            scope=scope_dict,
            source=f"{channel_id}.l1",
            source_id=channel_id,
            force_capture=True,
            links=[LinkRef(relation="derived_from", target_kind="memory", target_id=episode_id)] if episode_id else None,
            evidence=list(atom.source_message_ids) or ([episode_id] if episode_id else None),
            meta={
                "runtime_channel": channel_id,
                "authoritative": True,
                "capture_origin": "l1_extract",
                "memory_layer": "l1",
                "semantic_key": atom.semantic_key,
                "l1_type": memory_type,
                "source_message_ids": list(atom.source_message_ids),
                "source_event_id": f"{session_id}:{turn_id}:l1" if session_id and turn_id else f"{episode_id}:l1",
                "conflict_action": decision.action,
                "supersedes": list(decision.target_ids),
            },
        )
        written.append(
            {
                "record_id": fact.record_id,
                "status": fact.status,
                "memory_type": memory_type,
                "title": fact.title,
                "memory_layer": "l1",
                "conflict_action": decision.action,
            }
        )
    return written


def extract_l1_from_l0_record(
    memory_api: Any,
    record: RecordEnvelope,
    *,
    use_llm: bool = False,
    llm: object | None = None,
) -> list[dict[str, Any]]:
    if not is_episode_evidence_record(record):
        return []
    meta = business_metadata(record.meta)
    if str(meta.get("l1_extracted_at") or "").strip():
        return []
    text = str(record.content.get("text") or record.summary or "")
    atoms = extract_l1_atoms(
        turn_text=text,
        source_message_ids=[record.record_id],
        use_llm=use_llm,
        llm=llm,
    )
    channel_id = str(meta.get("runtime_channel") or record.source_id or "hermes")
    written = persist_l1_atoms(
        memory_api,
        atoms=atoms,
        episode_id=record.record_id,
        scope=record.scope,
        channel_id=channel_id,
        session_id=str(meta.get("session_id") or ""),
        turn_id=str(meta.get("turn_id") or ""),
    )
    record.meta = {**dict(record.meta or {}), "l1_extracted_at": "1", "l1_atom_count": len(written)}
    record.touch()
    memory_api.store.append(record)
    return written


def backfill_l1_from_l0(
    memory_api: Any,
    *,
    scope: dict | ScopeRef,
    limit: int = 50,
    use_llm: bool = False,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    records = memory_api.store.list_records(
        kinds=["memory"],
        scope=scope_ref,
        status="active",
        limit=max(1, min(500, int(limit))),
    )
    scanned = 0
    extracted = 0
    skipped = 0
    for record in records:
        if not is_episode_evidence_record(record):
            continue
        scanned += 1
        meta = business_metadata(record.meta)
        if str(meta.get("l1_extracted_at") or "").strip():
            skipped += 1
            continue
        written = extract_l1_from_l0_record(memory_api, record, use_llm=use_llm)
        extracted += len(written)
    return {"ok": True, "scanned": scanned, "extracted": extracted, "skipped": skipped}


def edit_l1_atom(
    memory_api: Any,
    *,
    record_id: str,
    scope: dict | ScopeRef,
    text: str,
    title: str = "",
) -> RecordEnvelope:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    existing = memory_api.store.get_by_id(record_id, scope=scope_ref)
    if existing is None:
        raise ValueError("l1_atom_not_found")
    meta = business_metadata(existing.meta)
    layer = str(meta.get("memory_layer") or "").strip().lower()
    memory_type = str(meta.get("memory_type") or existing.content.get("memory_type") or "instruction")
    if layer == "l0" or str(existing.meta.get("capture_origin") or "") == "turn_sync":
        raise ValueError("cannot_edit_l0_use_l1")
    new_title = str(title or existing.title or text[:72]).strip()
    scope_dict = {
        "tenant_id": scope_ref.tenant_id,
        "agent_id": scope_ref.agent_id,
        "workspace_id": scope_ref.workspace_id,
        "user_id": scope_ref.user_id,
    }
    return memory_api.ingest(
        text=str(text).strip(),
        memory_type=memory_type if memory_type not in {"conversation", "context"} else "instruction",
        title=new_title,
        scope=scope_dict,
        source=existing.source,
        source_id=existing.source_id,
        force_capture=True,
        meta={
            **{k: v for k, v in dict(existing.meta or {}).items() if k not in {"ingest_request_digest"}},
            "memory_layer": "l1",
            "capture_origin": "l1_edit",
            "edits": record_id,
        },
    )
