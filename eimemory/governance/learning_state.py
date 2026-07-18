from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.core.ids import generate_record_id
from eimemory.models.records import RecordEnvelope, ScopeRef


AUTONOMOUS_LEARNING_SCHEMA_VERSION = "autonomous_learning.v1"
ACTIVE_LOOP_STATUSES = {"running", "collecting", "researching", "experimenting", "evaluating", "promoting"}
TERMINAL_LOOP_STATUSES = {"completed", "blocked", "failed"}
DEFAULT_STALE_LOOP_SECONDS = 6 * 60 * 60


def scope_payload(scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    return asdict(scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope))


def stable_semantic_key(*parts: Any) -> str:
    text = "::".join(str(part or "").strip().lower() for part in parts)
    return sha256(text.encode("utf-8")).hexdigest()[:24]


def learning_idempotency_key(loop_id: str, step_name: str, semantic_key: str) -> str:
    return stable_semantic_key("learning", loop_id, step_name, semantic_key)


def start_learning_loop(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    trigger: str = "manual",
    dry_run: bool = False,
    force: bool = False,
    loop_id: str = "",
) -> RecordEnvelope:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    recover_stale_learning_loops(runtime, scope=scope_ref)
    active = active_learning_loops(runtime, scope=scope_ref)
    if active and not force:
        raise RuntimeError(f"active learning loop already exists: {active[0].record_id}")

    resolved_loop_id = loop_id or f"learn_{now_iso().replace('-', '').replace(':', '').replace('+', '_')}_{generate_record_id('learning_loop').split('_')[-1]}"
    idempotency_key = learning_idempotency_key(resolved_loop_id, "start", trigger or "manual")
    existing = find_record_by_idempotency(runtime, kinds=["learning_loop"], scope=scope_ref, idempotency_key=idempotency_key)
    if existing is not None:
        return existing

    record = RecordEnvelope.create(
        kind="learning_loop",
        title=f"Autonomous learning loop: {trigger or 'manual'}",
        summary=f"Learning loop started by {trigger or 'manual'}",
        scope=scope_ref,
        source="eimemory.autonomous_learning",
        status="running",
        content={
            "schema_version": AUTONOMOUS_LEARNING_SCHEMA_VERSION,
            "loop_id": resolved_loop_id,
            "trigger": trigger or "manual",
            "dry_run": bool(dry_run),
            "steps": [],
        },
        meta={
            "schema_version": AUTONOMOUS_LEARNING_SCHEMA_VERSION,
            "loop_id": resolved_loop_id,
            "trigger": trigger or "manual",
            "dry_run": bool(dry_run),
            "authority_tier": "L0",
            "semantic_key": stable_semantic_key("learning_loop", resolved_loop_id),
            "idempotency_key": idempotency_key,
            "status": "running",
        },
    )
    return runtime.store.append(record)


def active_learning_loops(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 50,
) -> list[RecordEnvelope]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    max_results = max(1, int(limit or 50))
    page_size = max(50, min(500, max_results))
    active: list[RecordEnvelope] = []
    offset = 0
    while True:
        page = runtime.store.list_records(kinds=["learning_loop"], scope=scope_ref, limit=page_size, offset=offset)
        for loop in page:
            if str(loop.status or "").strip().lower() in ACTIVE_LOOP_STATUSES:
                active.append(loop)
                if len(active) >= max_results:
                    return active
        if len(page) < page_size:
            return active
        offset += len(page)


def recover_stale_learning_loops(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    max_age_seconds: int = DEFAULT_STALE_LOOP_SECONDS,
    reason: str = "stale_learning_loop",
    limit: int = 50,
) -> list[RecordEnvelope]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    recovered: list[RecordEnvelope] = []
    now = datetime.now(timezone.utc)
    for loop in active_learning_loops(runtime, scope=scope_ref, limit=limit):
        age_seconds = _record_age_seconds(loop, now=now)
        if age_seconds < max(1, int(max_age_seconds)):
            continue
        previous_status = str(loop.status or "")
        loop.status = "failed"
        loop.summary = loop.summary or f"Recovered stale learning loop: {loop.record_id}"
        loop.meta["status"] = "failed"
        loop.meta["previous_status"] = previous_status
        loop.meta["stale_recovered_at"] = now_iso()
        loop.meta["stale_recovered_reason"] = reason
        loop.meta["stale_age_seconds"] = int(age_seconds)
        content = dict(loop.content or {})
        steps = list(content.get("steps") or [])
        steps.append(
            {
                "step_name": "stale_recovery",
                "status": "failed",
                "error": reason,
                "record_ids": [],
                "metrics": {"stale_age_seconds": int(age_seconds)},
                "updated_at": loop.meta["stale_recovered_at"],
            }
        )
        content["steps"] = steps
        content["stale_recovered_at"] = loop.meta["stale_recovered_at"]
        content["stale_recovered_reason"] = reason
        content["previous_status"] = previous_status
        loop.content = content
        loop.touch()
        recovered.append(runtime.store.rewrite(loop))
    return recovered


def latest_learning_loop(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
) -> RecordEnvelope | None:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    loops = runtime.store.list_records(kinds=["learning_loop"], scope=scope_ref, limit=1)
    return loops[0] if loops else None


def mark_step(
    runtime: Any,
    loop: RecordEnvelope | str,
    *,
    step_name: str,
    status: str,
    record_ids: list[str] | None = None,
    error: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> RecordEnvelope:
    record = _resolve_loop(runtime, loop)
    content = dict(record.content or {})
    steps = list(content.get("steps") or [])
    existing_index = next((idx for idx, item in enumerate(steps) if str(item.get("step_name") or "") == step_name), None)
    step_payload = {
        "step_name": step_name,
        "status": status,
        "record_ids": list(record_ids or []),
        "error": error or "",
        "metrics": dict(metrics or {}),
        "updated_at": now_iso(),
    }
    if existing_index is None:
        steps.append(step_payload)
    else:
        previous = dict(steps[existing_index])
        previous.update(step_payload)
        previous.setdefault("created_at", previous.get("updated_at") or step_payload["updated_at"])
        steps[existing_index] = previous
    content["steps"] = steps
    record.content = content
    record.meta["last_step"] = step_name
    record.meta["last_step_status"] = status
    if status in TERMINAL_LOOP_STATUSES:
        record.status = status
        record.meta["status"] = status
    record.touch()
    return runtime.store.rewrite(record)


def complete_learning_loop(
    runtime: Any,
    loop: RecordEnvelope | str,
    *,
    status: str = "completed",
    summary: str = "",
) -> RecordEnvelope:
    if status not in TERMINAL_LOOP_STATUSES:
        raise ValueError(f"invalid terminal loop status: {status}")
    record = _resolve_loop(runtime, loop)
    record.status = status
    record.summary = summary or record.summary
    record.meta["status"] = status
    record.meta["finished_at"] = now_iso()
    content = dict(record.content or {})
    content["finished_at"] = record.meta["finished_at"]
    record.content = content
    record.touch()
    return runtime.store.rewrite(record)


def append_learning_record_once(
    runtime: Any,
    *,
    kind: str,
    title: str,
    summary: str,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    step_name: str,
    semantic_key: str,
    authority_tier: str = "L0",
    status: str = "candidate",
    content: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    evidence: list[str] | None = None,
    source: str = "eimemory.autonomous_learning",
    release_bound_idempotency: bool = True,
) -> RecordEnvelope:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    if not release_bound_idempotency and not (
        kind == "promotion_request" and source == "eimemory.deployment_receipt"
    ):
        raise ValueError("release-unbound idempotency is reserved for deployment receipts")
    from eimemory.governance.evidence_contract import current_release_identity, release_identity_payload

    release = current_release_identity(runtime, scope_ref)
    release_payload = release_identity_payload(release) if release is not None else {}
    idempotency_semantic_key = semantic_key
    if release is not None and release_bound_idempotency:
        idempotency_semantic_key = stable_semantic_key(
            semantic_key,
            release.commit,
            release.version,
            release.receipt_id,
            release.session_id,
        )
    idem = learning_idempotency_key(loop_id, step_name, idempotency_semantic_key)
    existing = find_record_by_idempotency(runtime, kinds=[kind], scope=scope_ref, idempotency_key=idem)
    if existing is not None:
        return existing
    supplied_meta = dict(meta or {})
    supplied_content = dict(content or {})
    for key in ("release_commit", "release_version", "deployment_receipt_id", "release_session_id"):
        supplied_meta.pop(key, None)
        supplied_content.pop(key, None)
    meta_payload = {
        "schema_version": AUTONOMOUS_LEARNING_SCHEMA_VERSION,
        "loop_id": loop_id,
        "authority_tier": authority_tier,
        "semantic_key": semantic_key,
        "idempotency_key": idem,
        **supplied_meta,
        **release_payload,
    }
    content_payload = {
        "schema_version": AUTONOMOUS_LEARNING_SCHEMA_VERSION,
        "loop_id": loop_id,
        **supplied_content,
        **release_payload,
    }
    record = RecordEnvelope.create(
        kind=kind,
        title=title,
        summary=summary,
        scope=scope_ref,
        source=source,
        status=status,
        content=content_payload,
        meta=meta_payload,
        evidence=list(evidence or []),
    )
    return runtime.store.append(record)


def find_record_by_idempotency(
    runtime: Any,
    *,
    kinds: list[str],
    scope: dict[str, Any] | ScopeRef | None,
    idempotency_key: str,
    page_size: int = 500,
) -> RecordEnvelope | None:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    indexed_lookup = getattr(runtime.store, "get_by_idempotency_key", None)
    if callable(indexed_lookup):
        return indexed_lookup(kinds=kinds, scope=scope_ref, idempotency_key=idempotency_key)
    offset = 0
    while True:
        page = runtime.store.list_records(kinds=kinds, scope=scope_ref, limit=page_size, offset=offset)
        for record in page:
            if str(record.meta.get("idempotency_key") or "") == idempotency_key:
                return record
        if len(page) < page_size:
            return None
        offset += len(page)


def _resolve_loop(runtime: Any, loop: RecordEnvelope | str) -> RecordEnvelope:
    if isinstance(loop, RecordEnvelope):
        return loop
    record = runtime.store.get_by_id(str(loop))
    if record is None or record.kind != "learning_loop":
        raise ValueError(f"learning loop not found: {loop}")
    return record


def _record_age_seconds(record: RecordEnvelope, *, now: datetime) -> float:
    timestamp = str(record.time.updated_at or record.time.created_at or "").strip()
    if not timestamp:
        return 0.0
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds())
