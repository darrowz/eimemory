from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
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
    scope_ref = ScopeRef.from_dict(scope_dict)
    parent = memory_api.store.get_by_id(episode_id, scope=scope_ref) if episode_id else None
    if parent is not None and (parent.scope != scope_ref or parent.source_id != channel_id):
        raise ValueError("l1_parent_partition_mismatch")
    typed_atoms = [atom for atom in atoms if isinstance(atom, L1Atom)]
    decisions = adjudicate_l1_atoms(memory_api, typed_atoms, scope=scope_dict, llm=llm)
    written: list[dict[str, Any]] = []
    for decision in decisions:
        if decision.action == "skip":
            continue
        atom = decision.atom
        text = decision.merged_content or atom.text
        memory_type = decision.merged_type or atom.memory_type
        # Identity is per observation, not a global semantic-key deduplication.
        identity = ["l1_atom.v1", asdict(scope_ref), channel_id, episode_id,
                    session_id, turn_id, asdict(atom)]
        atom_id = "mem_" + sha256(json.dumps(
            identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()[:32] if episode_id or (session_id and turn_id) else ""
        fact = memory_api.ingest(
            record_id=atom_id,
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
        # Extraction time is not the event time of the source observation.
        if parent is not None and parent.time.occurred_at:
            def preserve_event_time(sqlite):
                current = sqlite.get_by_exact_ref(
                    fact.record_id, scope=scope_ref, source_id=channel_id)
                if current is None or current.source != fact.source:
                    raise ValueError("l1_atom_partition_mismatch")
                if current.time.occurred_at == parent.time.occurred_at:
                    return current, [], []
                current.time.occurred_at = parent.time.occurred_at
                sqlite.upsert(current, commit=False)
                return current, [current], []
            fact = memory_api.store.mutate_records_atomically(preserve_event_time)
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
    stored = memory_api.store.get_by_exact_ref(
        record.record_id, scope=record.scope, source_id=record.source_id)
    if stored is None or stored.source != record.source:
        raise ValueError("l1_parent_partition_mismatch")
    # Callers can hold a stale pre-extraction envelope; always read completion
    # and source material from the authoritative row.
    record = stored
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
    channel_id = record.source_id
    written = persist_l1_atoms(
        memory_api,
        atoms=atoms,
        episode_id=record.record_id,
        scope=record.scope,
        channel_id=channel_id,
        session_id=str(meta.get("session_id") or ""),
        turn_id=str(meta.get("turn_id") or ""),
    )
    def complete_extraction(sqlite):
        current = sqlite.get_by_exact_ref(
            record.record_id, scope=record.scope, source_id=record.source_id)
        if (current is None or current.source != record.source
                or current.content != record.content or current.summary != record.summary
                or current.time.occurred_at != record.time.occurred_at):
            raise ValueError("l1_parent_changed_during_extraction")
        if str(business_metadata(current.meta).get("l1_extracted_at") or "").strip():
            return [], [], []
        current.meta = {**dict(current.meta or {}),
                        "l1_extracted_at": "1", "l1_atom_count": len(written)}
        current.touch()
        sqlite.upsert(current, commit=False)
        return written, [current], []

    return memory_api.store.mutate_records_atomically(complete_extraction)


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
