from __future__ import annotations

import base64
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

from eimemory.knowledge.l1_conflict import adjudicate_l1_atoms
from eimemory.knowledge.sediment import L1Atom, L1ExtractorUnavailable, extract_l1_atoms
from eimemory.metadata import business_metadata, normalize_metadata, split_metadata
from eimemory.models.memory_edges import MemoryEdge
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef
from eimemory.contracts.l1_source_order import (
    L1SourceOrderBlocked, l1_source_event_binding, l1_source_material_digest,
    require_l1_supersession_order, validate_l1_source_event, require_l1_source_parent_current,
)


L1_EXTRACT_VERSION = "l1_extract.v3"


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
    source_event_binding: dict | None = None,
) -> RecordEnvelope:
    """Repair a pre-v2 L1 atom that was stored without applying its update targets."""
    if not target_ids:
        return record
    scope_ref = record.scope
    source_id = record.source_id

    def mutation(sqlite):
        current = sqlite.get_by_exact_ref(
            record.record_id, scope=scope_ref, source_id=source_id, preserve_unknown_occurred_at=True
        )
        if current is None or current.status != "active":
            raise ValueError("l1_existing_atom_unavailable")
        if source_event_binding is not None:
            from eimemory.recall.indexing import is_inactive_or_superseded_record
            if is_inactive_or_superseded_record(current):
                raise L1SourceOrderBlocked("l1_existing_atom_conflict")
            if (current.source != record.source or current.summary != record.summary
                    or current.content != record.content):
                raise L1SourceOrderBlocked("l1_existing_atom_conflict")
            validate_l1_source_event(current, source_event_binding,
                                     get_exact=sqlite.get_by_exact_ref)
            current_binding = business_metadata(current.meta).get("source_event_binding")
            if current_binding != business_metadata(record.meta).get("source_event_binding"):
                raise L1SourceOrderBlocked("l1_existing_atom_conflict")
            if current_binding is None and current.time.occurred_at != source_event_binding["occurred_at"]:
                raise L1SourceOrderBlocked("l1_legacy_source_binding_unknown")
            if current_binding is not None and current_binding != source_event_binding:
                raise L1SourceOrderBlocked("l1_source_parent_changed")
            if current_binding is not None and current.time.occurred_at != source_event_binding["occurred_at"]:
                raise L1SourceOrderBlocked("l1_source_binding_invalid")
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
            if source_event_binding is not None:
                require_l1_supersession_order(current, old, source_event_binding,
                                             get_exact=sqlite.get_by_exact_ref)
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
    diagnostics_sink: dict | None = None,
    source_event_binding: dict | None = None,
) -> list[dict[str, Any]]:
    """Write L1 atoms after Tencent-style store/skip/update adjudication."""

    scope_dict = _scope_dict(scope)
    scope_ref = ScopeRef.from_dict(scope_dict)
    parent = (
        memory_api.store.get_by_exact_ref(
            episode_id, scope=scope_ref, source_id=channel_id, preserve_unknown_occurred_at=True
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
    if source_event_binding is not None and (
        parent is None or l1_source_event_binding(parent) != source_event_binding
    ):
        raise L1SourceOrderBlocked("l1_source_parent_changed")
    source_binding = source_event_binding or (l1_source_event_binding(parent) if parent is not None else None)
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
        source_event_binding=source_binding,
    )
    if diagnostics_sink is not None:
        diagnostics_sink.update(store_count=0, skip_count=0, update_count=0,
                                source_order_stale_count=0, skipped_target_records=[],
                                written_record_ids=[])
    written: list[dict[str, Any]] = []
    for decision in decisions:
        if decision.action == "skip":
            if diagnostics_sink is not None:
                diagnostics_sink["skip_count"] += 1
                for target_id in decision.target_ids:
                    target = memory_api.store.get_by_exact_ref(
                        target_id, scope=scope_ref, source_id=channel_id)
                    diagnostics_sink["skipped_target_records"].append(target)
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
        try:
            if existing is not None:
                fact = existing
                if source_binding is not None:
                    def validate_existing(sqlite):
                        current = sqlite.get_by_exact_ref(
                            existing.record_id, scope=scope_ref, source_id=channel_id,
                            preserve_unknown_occurred_at=True)
                        if current is None:
                            raise L1SourceOrderBlocked("l1_source_parent_unavailable")
                        if (current.summary != text or current.content.get("text") != text
                                or current.content.get("memory_type") != memory_type):
                            raise L1SourceOrderBlocked("l1_existing_atom_conflict")
                        validate_l1_source_event(current, source_binding,
                                                 get_exact=sqlite.get_by_exact_ref)
                        previous = business_metadata(current.meta).get("source_event_binding")
                        if previous is None and current.time.occurred_at != parent.time.occurred_at:
                            raise L1SourceOrderBlocked("l1_legacy_source_binding_unknown")
                        if previous is not None and previous != source_binding:
                            raise L1SourceOrderBlocked("l1_source_parent_changed")
                        if previous is not None and current.time.occurred_at != parent.time.occurred_at:
                            raise L1SourceOrderBlocked("l1_source_binding_invalid")
                        return current, [], []
                    fact = memory_api.store.mutate_records_atomically(validate_existing)
                    from eimemory.recall.indexing import is_inactive_or_superseded_record
                    if is_inactive_or_superseded_record(fact):
                        # Explicit legacy re-extraction cannot revive an atom
                        # that a later authorized observation already closed.
                        if diagnostics_sink is not None:
                            diagnostics_sink["skip_count"] += 1
                            diagnostics_sink["skipped_target_records"].append(fact)
                        continue
                if decision.action == "update" and decision.target_ids:
                    fact = _reconcile_existing_l1_update(
                        memory_api, record=fact, target_ids=decision.target_ids,
                        source_event_binding=source_binding,
                    )
            else:
                fact = memory_api.ingest(
                record_id=atom_id,
                text=text,
                memory_type=memory_type,
                title=text[:72],
                scope={**scope_dict, "preserve_scope": True} if source_binding is not None else scope_dict,
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
                **({"source_event_binding": source_binding} if source_binding is not None else {}),
            )
        except L1SourceOrderBlocked as exc:
            if exc.code != "l1_source_order_stale":
                raise
            # A completed old observation is not a retryable model outage. Its
            # L0 history remains; it cannot replace a later current state.
            if diagnostics_sink is not None:
                diagnostics_sink["skip_count"] += 1
                diagnostics_sink["source_order_stale_count"] += 1
                if exc.target is not None:
                    diagnostics_sink["skipped_target_records"].append(exc.target)
            continue
        # Bound source time was assigned and verified by the write owner before
        # this child's first commit, including all of its supersession targets.
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
        if diagnostics_sink is not None:
            diagnostics_sink["update_count" if decision.action == "update" else "store_count"] += 1
            diagnostics_sink["written_record_ids"].append(fact.record_id)
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
        record.record_id, scope=record.scope, source_id=record.source_id,
        preserve_unknown_occurred_at=True)
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
    source_binding = l1_source_event_binding(record)
    extraction: dict[str, Any] = {}
    adjudication: dict[str, Any] = {}
    # Queue/caller text can be stale. Only the source that was actually read
    # from this exact partition supplies user/assistant roles and material.
    text = str(record.content.get("text") or record.summary or "")
    try:
        client = _resolve_l1_client(
            use_llm=use_llm, llm=llm, fallback_heuristic=fallback,
        )
        atoms = extract_l1_atoms(
            turn_text=text,
            source_message_ids=[record.record_id],
            use_llm=use_llm, llm=client, fallback_heuristic=fallback,
            diagnostics_sink=extraction,
        )
        channel_id = record.source_id
        written = persist_l1_atoms(
            memory_api, atoms=atoms, episode_id=record.record_id,
            scope=record.scope, channel_id=channel_id,
            session_id=str(meta.get("session_id") or ""),
            turn_id=str(meta.get("turn_id") or ""), llm=client,
            strict_conflict=bool(use_llm and not fallback),
            diagnostics_sink=adjudication, source_event_binding=source_binding,
        )
    except Exception as exc:
        _persist_extraction_failure(memory_api, record=record, extraction=extraction,
                                    adjudication=adjudication, failure=exc)
        raise
    def complete_extraction(sqlite):
        current = sqlite.get_by_exact_ref(
            record.record_id, scope=record.scope, source_id=record.source_id,
            preserve_unknown_occurred_at=True)
        if (current is None or current.source != record.source
                or l1_source_material_digest(current) != source_binding["input_digest"]):
            raise ValueError("l1_parent_changed_during_extraction")
        require_l1_source_parent_current(current, source_binding)
        current_meta = business_metadata(current.meta)
        if _authoritative_completion(current_meta):
            return [], [], []
        if str(current_meta.get("l1_extracted_at") or "").strip() and not retry_legacy:
            return [], [], []
        completion_status = "stored" if written else ("no_change" if atoms else "no_memory")
        from eimemory.knowledge.l1_diagnostics import normalize_l1_extraction_diagnostics
        children = [sqlite.get_by_exact_ref(item["record_id"], scope=current.scope,
                                           source_id=current.source_id) for item in written]
        diagnostic = normalize_l1_extraction_diagnostics(
            parent=current, extraction=extraction, adjudication=adjudication,
            child_records=children, completion_status=completion_status,
        )
        current.meta = {
            **dict(current.meta or {}),
            "l1_extracted_at": datetime.now(timezone.utc).isoformat(),
            "l1_atom_count": len(written),
            "l1_candidate_count": len(atoms),
            "l1_extraction_status": completion_status,
            "l1_extraction_diagnostics": diagnostic,
            "l1_extractor": diagnostic["extractor"],
            "l1_extract_version": L1_EXTRACT_VERSION,
        }
        current.touch()
        sqlite.upsert(current, commit=False)
        return written, [current], []

    try:
        return memory_api.store.mutate_records_atomically(complete_extraction)
    except Exception as exc:
        _persist_extraction_failure(memory_api, record=record, extraction=extraction,
                                    adjudication=adjudication, failure=exc)
        raise


def _persist_extraction_failure(
    memory_api: Any, *, record: RecordEnvelope, extraction: dict,
    adjudication: dict, failure: Exception,
) -> None:
    """Keep a bounded failed observation without an extraction completion mark."""
    from eimemory.knowledge.l1_diagnostics import normalize_l1_extraction_diagnostics
    reason = getattr(failure, "code", "")
    if not reason and failure.args and isinstance(failure.args[0], str):
        reason = failure.args[0]
    report = normalize_l1_extraction_diagnostics(
        parent=record, extraction=extraction, adjudication=adjudication,
        child_records=[memory_api.store.get_by_exact_ref(
            child_id, scope=record.scope, source_id=record.source_id)
            for child_id in adjudication.get("written_record_ids", [])]
            if "written_record_ids" in adjudication else None,
        completion_status="blocked" if isinstance(failure, L1SourceOrderBlocked) else "unavailable",
        failure_reason=reason,
    )
    def observation(sqlite):
        current = sqlite.get_by_exact_ref(record.record_id, scope=record.scope,
                                          source_id=record.source_id,
                                          preserve_unknown_occurred_at=True)
        if current is None or current.source != record.source:
            return None, [], []
        if _authoritative_completion(business_metadata(current.meta)):
            return None, [], []
        # A changed source keeps this old observation's binding. The reader
        # reports source_binding_changed instead of certifying it for new text.
        current.meta = {**dict(current.meta or {}), "l1_extraction_diagnostics": report}
        current.touch()
        sqlite.upsert(current, commit=False)
        return None, [current], []
    try:
        memory_api.store.mutate_records_atomically(observation)
    except Exception:
        # An unavailable diagnostic sink cannot mask the original failure.
        pass


def backfill_l1_from_l0(
    memory_api: Any,
    *,
    scope: dict | ScopeRef,
    limit: int = 50,
    use_llm: bool = True,
    llm: object | None = None,
    retry_legacy: bool = False,
    cursor: str = "",
    scan_limit: int = 1000,
) -> dict[str, Any]:
    """Process a bounded page, returning an explicit continuation when needed.

    Completion describes the read page's end, not future concurrent captures.
    A fresh call without a cursor starts a new sweep; no cursor state is saved.
    """
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    bounded_limit = max(1, min(500, int(limit)))
    bounded_scan = max(1, min(1000, int(scan_limit)))
    cursor_identity = {
        "schema": "l1_backfill_cursor.v1",
        "scope": asdict(scope_ref),
        "retry_legacy": bool(retry_legacy),
        "extract_version": L1_EXTRACT_VERSION,
    }
    after = ""
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 8192:
                raise ValueError("invalid cursor")
            state = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (not isinstance(state, dict)
                    or any(state.get(key) != value for key, value in cursor_identity.items())
                    or not isinstance(state.get("after"), str) or not state["after"]):
                raise ValueError("cursor identity mismatch")
            after = state["after"]
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("l1_backfill_cursor_invalid") from exc
    page, has_more = memory_api.store.list_l1_backfill_page(
        scope=scope_ref, after=after, limit=bounded_scan,
    )
    scanned_records = 0
    scanned = 0
    extracted = 0
    skipped = 0
    completed = 0
    empty = 0
    legacy_retried = 0
    for storage_key, record in page:
        scanned_records += 1
        after = storage_key
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
        if completed >= bounded_limit:
            break
    remaining = has_more or scanned_records < len(page)
    next_cursor = (
        base64.urlsafe_b64encode(json.dumps(
            {**cursor_identity, "after": after}, sort_keys=True,
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).decode("ascii")
        if remaining else ""
    )
    return {
        "ok": True,
        "scanned": scanned,
        "completed": completed,
        "extracted": extracted,
        "empty": empty,
        "skipped": skipped,
        "legacy_retried": legacy_retried,
        "loaded_records": len(page),
        "scanned_records": scanned_records,
        "scan_limit": bounded_scan,
        "scan_exhausted": bool(remaining and scanned_records >= bounded_scan),
        "next_cursor": next_cursor,
        "complete": not remaining,
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
    if existing.scope != scope_ref:
        raise ValueError("l1_atom_partition_mismatch")
    from eimemory.recall.indexing import is_inactive_or_superseded_record
    if existing.status != "active" or is_inactive_or_superseded_record(existing):
        raise ValueError("l1_atom_inactive")
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
    business_meta, runtime_meta = split_metadata(existing.meta)
    for key in {"ingest_request_digest", "superseded_by", "mutation_state", "removed_by",
                "conflict_action", "supersedes", "edits"}:
        business_meta.pop(key, None)
    business_meta.update(memory_layer="l1", capture_origin="l1_edit", edits=record_id)
    return memory_api.ingest(
        text=str(text).strip(),
        memory_type=memory_type if memory_type not in {"conversation", "context"} else "instruction",
        title=new_title,
        scope=scope_dict,
        source=existing.source,
        source_id=existing.source_id,
        force_capture=True,
        evidence=list(existing.evidence),
        links=[link for link in existing.links if link.relation not in {"supersedes", "superseded_by", "removed_by"}],
        supersede_record_ids=[record_id],
        meta=normalize_metadata({"business_meta": business_meta, "runtime_meta": runtime_meta}),
    )
