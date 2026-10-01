from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

from eimemory.knowledge.l1_conflict import adjudicate_l1_atoms
from eimemory.knowledge.sediment import L1Atom, L1ExtractorUnavailable, extract_l1_atoms
from eimemory.metadata import business_metadata
from eimemory.models.memory_edges import MemoryEdge
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef


L1_EXTRACT_VERSION = "l1_extract.v2"


def _is_episode_evidence_record(record):
    from eimemory.recall.indexing import is_episode_evidence_record as _impl
    return _impl(record)


def _scope_dict(scope: dict | ScopeRef) -> dict[str, str]:
    if isinstance(scope, dict):
        return dict(scope)
    return {
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "workspace_id": scope.workspace_id,
        "user_id": scope.user_id,
    }


def _atom_record_id(
    *,
    atom: L1Atom,
    scope_ref: ScopeRef,
    channel_id: str,
    episode_id: str,
    session_id: str,
    turn_id: str,
) -> str:
    if not episode_id and not (session_id and turn_id):
        return ""
    identity = [
        "l1_atom.v1",
        asdict(scope_ref),
        channel_id,
        episode_id,
        session_id,
        turn_id,
        asdict(atom),
    ]
    return "mem_" + sha256(json.dumps(
        identity,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:32]


def _authoritative_completion(meta: dict[str, Any]) -> bool:
    return (
        str(meta.get("l1_extract_version") or "") == L1_EXTRACT_VERSION
        and str(meta.get("l1_extraction_status") or "")
        in {"stored", "no_change", "no_memory"}
    )


def _resolve_l1_client(*, use_llm: bool, llm: object | None, fallback_heuristic: bool) -> object | None:
    if not use_llm or llm is not None:
        return llm
    from eimemory.llm.hermes_adapter import resolve_l1_llm_client
    client = resolve_l1_llm_client()
    if client is None and not fallback_heuristic:
        raise L1ExtractorUnavailable("l1_llm_unavailable")
    return client


def _reconcile_existing_l1_update(
    memory_api: Any,
    *,
    record: RecordEnvelope,
    target_ids: tuple[str, ...],
) -> RecordEnvelope:
    """Repair a pre-v2 L1 atom that was stored without applying its update targets."""
    if not target_ids:
        return record
    scope_ref = record.scope
    source_id = record.source_id

    def mutation(sqlite):
        current = sqlite.get_by_exact_ref(
            record.record_id, scope=scope_ref, source_id=source_id
        )
        if current is None or current.status != "active":
            raise ValueError("l1_existing_atom_unavailable")
        changed: list[RecordEnvelope] = [current]
        edges: list[MemoryEdge] = []
        supersedes = {
            link.target_id
            for link in current.links
            if link.relation == "supersedes" and link.target_kind == "record"
        }
        for target_id in dict.fromkeys(target_ids):
            if not target_id or target_id == current.record_id:
                continue
            old = sqlite.get_by_exact_ref(
                target_id, scope=scope_ref, source_id=source_id
            )
            if old is None:
                raise ValueError("l1_conflict_target_not_found")
            if old.kind != "memory":
                raise ValueError("l1_conflict_target_kind_mismatch")
            if old.status == "superseded" and str(
                business_metadata(old.meta).get("superseded_by")
                or old.meta.get("superseded_by")
                or ""
            ) == current.record_id:
                supersedes.add(old.record_id)
                continue
            if old.status != "active":
                raise ValueError("l1_conflict_target_inactive")
            old.status = "superseded"
            old.links = [
                link for link in old.links if link.relation != "superseded_by"
            ]
            old.links.append(LinkRef(
                relation="superseded_by",
                target_kind="record",
                target_id=current.record_id,
            ))
            old.meta = {
                **dict(old.meta or {}),
                "superseded_by": current.record_id,
                "mutation_state": "superseded",
            }
            old.touch()
            sqlite.upsert(old, commit=False)
            changed.append(old)
            supersedes.add(old.record_id)
            edges.append(MemoryEdge.create(
                from_id=current.record_id,
                to_id=old.record_id,
                edge_type="temporal",
                confidence=1.0,
                evidence_id=current.record_id,
                scope=current.scope,
                reason="supersedes",
            ))
        current.links = [
            link for link in current.links
            if not (link.relation == "supersedes" and link.target_kind == "record")
        ] + [
            LinkRef(relation="supersedes", target_kind="record", target_id=target_id)
            for target_id in sorted(supersedes)
        ]
        current.meta = {
            **dict(current.meta or {}),
            "conflict_action": "update",
            "supersedes": sorted(supersedes),
        }
        current.touch()
        sqlite.upsert(current, commit=False)
        return current, changed, edges

    return memory_api.store.mutate_records_atomically(mutation)


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
    strict_conflict: bool = False,
) -> list[dict[str, Any]]:
    """Write L1 atoms after Tencent-style store/skip/update adjudication."""

    scope_dict = _scope_dict(scope)
    scope_ref = ScopeRef.from_dict(scope_dict)
    parent = (
        memory_api.store.get_by_exact_ref(
            episode_id, scope=scope_ref, source_id=channel_id
        )
        if episode_id else None
    )
    if episode_id and parent is None:
        visible = memory_api.store.get_by_id(episode_id, scope=scope_ref)
        if visible is not None and (
            visible.scope != scope_ref or visible.source_id != channel_id
        ):
            raise ValueError("l1_parent_partition_mismatch")
    if parent is not None and (parent.scope != scope_ref or parent.source_id != channel_id):
        raise ValueError("l1_parent_partition_mismatch")
    typed_atoms = [atom for atom in atoms if isinstance(atom, L1Atom)]
    atom_ids = {
        id(atom): _atom_record_id(
            atom=atom,
            scope_ref=scope_ref,
            channel_id=channel_id,
            episode_id=episode_id,
            session_id=session_id,
            turn_id=turn_id,
        )
        for atom in typed_atoms
    }
    existing_ids = []
    for atom in typed_atoms:
        atom_id = atom_ids[id(atom)]
        if not atom_id:
            continue
        existing = memory_api.store.get_by_exact_ref(
            atom_id, scope=scope_ref, source_id=channel_id
        )
        if existing is not None:
            existing_ids.append(existing.record_id)
    decisions = adjudicate_l1_atoms(
        memory_api,
        typed_atoms,
        scope=scope_dict,
        source_id=channel_id,
        llm=llm,
        strict=strict_conflict,
        exclude_record_ids=existing_ids,
    )
    written: list[dict[str, Any]] = []
    for decision in decisions:
        if decision.action == "skip":
            continue
        atom = decision.atom
        text = decision.merged_content or atom.text
        memory_type = decision.merged_type or atom.memory_type
        atom_id = atom_ids[id(atom)]
        existing = (
            memory_api.store.get_by_exact_ref(
                atom_id, scope=scope_ref, source_id=channel_id
            )
            if atom_id else None
        )
        if existing is not None:
            fact = existing
            if decision.action == "update" and decision.target_ids:
                fact = _reconcile_existing_l1_update(
                    memory_api,
                    record=existing,
                    target_ids=decision.target_ids,
                )
        else:
            fact = memory_api.ingest(
                record_id=atom_id,
                text=text,
                memory_type=memory_type,
                title=text[:72],
                scope=scope_dict,
                source=f"{channel_id}.l1",
                source_id=channel_id,
                force_capture=True,
                links=[LinkRef(
                    relation="derived_from",
                    target_kind="memory",
                    target_id=episode_id,
                )] if episode_id else None,
                evidence=list(atom.source_message_ids) or (
                    [episode_id] if episode_id else None
                ),
                meta={
                    "runtime_channel": channel_id,
                    "authoritative": True,
                    "capture_origin": "l1_extract",
                    "memory_layer": "l1",
                    "semantic_key": atom.semantic_key,
                    "l1_type": memory_type,
                    "source_message_ids": list(atom.source_message_ids),
                    "source_event_id": (
                        f"{session_id}:{turn_id}:l1"
                        if session_id and turn_id else f"{episode_id}:l1"
                    ),
                    "conflict_action": decision.action,
                    "supersedes": list(decision.target_ids),
                },
                supersede_record_ids=list(decision.target_ids)
                if decision.action == "update" else None,
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
    user_text: str = "",
    assistant_text: str = "",
    turn_text: str = "",
    fallback_heuristic: bool | None = None,
    retry_legacy: bool = False,
) -> list[dict[str, Any]]:
    if not _is_episode_evidence_record(record):
        return []
    stored = memory_api.store.get_by_exact_ref(
        record.record_id, scope=record.scope, source_id=record.source_id)
    if stored is None or stored.source != record.source:
        raise ValueError("l1_parent_partition_mismatch")
    # Callers can hold a stale pre-extraction envelope; always read completion
    # and source material from the authoritative row.
    record = stored
    meta = business_metadata(record.meta)
    completed = str(meta.get("l1_extracted_at") or "").strip()
    if _authoritative_completion(meta):
        return []
    if completed and not retry_legacy:
        return []
    fallback = (not use_llm) if fallback_heuristic is None else bool(fallback_heuristic)
    client = _resolve_l1_client(
        use_llm=use_llm,
        llm=llm,
        fallback_heuristic=fallback,
    )
    text = str(turn_text or record.content.get("text") or record.summary or "")
    atoms = extract_l1_atoms(
        user_text=user_text,
        assistant_text=assistant_text,
        turn_text=text,
        source_message_ids=[record.record_id],
        use_llm=use_llm,
        llm=client,
        fallback_heuristic=fallback,
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
        llm=client,
        strict_conflict=bool(use_llm and not fallback),
    )
    def complete_extraction(sqlite):
        current = sqlite.get_by_exact_ref(
            record.record_id, scope=record.scope, source_id=record.source_id)
        if (current is None or current.source != record.source
                or current.content != record.content or current.summary != record.summary
                or current.time.occurred_at != record.time.occurred_at):
            raise ValueError("l1_parent_changed_during_extraction")
        current_meta = business_metadata(current.meta)
        if _authoritative_completion(current_meta):
            return [], [], []
        if str(current_meta.get("l1_extracted_at") or "").strip() and not retry_legacy:
            return [], [], []
        current.meta = {
            **dict(current.meta or {}),
            "l1_extracted_at": datetime.now(timezone.utc).isoformat(),
            "l1_atom_count": len(written),
            "l1_candidate_count": len(atoms),
            "l1_extraction_status": (
                "stored" if written else ("no_change" if atoms else "no_memory")
            ),
            "l1_extractor": (
                "llm" if use_llm and client is not None else "heuristic"
            ),
            "l1_extract_version": L1_EXTRACT_VERSION,
        }
        current.touch()
        sqlite.upsert(current, commit=False)
        return written, [current], []

    return memory_api.store.mutate_records_atomically(complete_extraction)


def backfill_l1_from_l0(
    memory_api: Any,
    *,
    scope: dict | ScopeRef,
    limit: int = 50,
    use_llm: bool = True,
    llm: object | None = None,
    retry_legacy: bool = False,
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
    completed = 0
    empty = 0
    legacy_retried = 0
    for record in records:
        if not _is_episode_evidence_record(record):
            continue
        scanned += 1
        meta = business_metadata(record.meta)
        marker = str(meta.get("l1_extracted_at") or "").strip()
        if _authoritative_completion(meta):
            skipped += 1
            continue
        if marker and not retry_legacy:
            skipped += 1
            continue
        if marker:
            legacy_retried += 1
        written = extract_l1_from_l0_record(
            memory_api,
            record,
            use_llm=use_llm,
            llm=llm,
            retry_legacy=retry_legacy,
            fallback_heuristic=not use_llm,
        )
        extracted += len(written)
        completed += 1
        if not written:
            empty += 1
    return {
        "ok": True,
        "scanned": scanned,
        "completed": completed,
        "extracted": extracted,
        "empty": empty,
        "skipped": skipped,
        "legacy_retried": legacy_retried,
    }


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
