from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime, timezone
import json
import re
from types import SimpleNamespace
from typing import Any

from eimemory.identity import (
    build_identity_report,
    hongtu_query_scopes,
    is_hongtu_scope,
    needs_hongtu_identity_repair,
    normalize_hongtu_record,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.adapters.runtime.channel import SUPPORTED_RUNTIME_CHANNELS, resolve_channel_scope
from eimemory.storage.jsonl import payload_digest


# Exact refs bind tenant/agent/workspace/user/source/id, never a bare record ID.
_REF_WHERE = (
    "tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ? "
    "AND source_id = ? AND record_id = ?"
)


class _RepairSkipped(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def identity_report(runtime, *, limit: int | None = None, scope=None) -> dict[str, Any]:
    return build_identity_report(_iter_records(runtime, limit=limit, scope=scope))


def repair_hongtu_identity(
    runtime,
    *,
    apply: bool = False,
    limit: int | None = None,
    scope=None,
    skip_created_at_or_after: str | None = None,
) -> dict[str, Any]:
    """Repair identity using exact refs and content-version CAS.

    Legacy agent/workspace aliases use the existing transactional rewrite
    contract; tenant, user and source remain unchanged. A payload digest is a
    content version, not a monotonic generation:
    byte-identical delete/recreate ABA is outside this guarantee.
    """
    if skip_created_at_or_after:
        _utc_instant(skip_created_at_or_after)
    if scope is None:
        if apply:
            raise ValueError("identity_repair_scope_required")
        report = build_identity_report(
            _iter_records(runtime, limit=limit, skip_created_at_or_after=skip_created_at_or_after)
        )
        return {**report, "ok": True, "apply": False, "scoped": False,
                "candidate_count": report["repair_candidate_count"], "repaired_count": 0,
                "repaired_record_ids": [], "diagnostic_only": True}

    if not isinstance(scope, ScopeRef):
        keys = ("tenant_id", "agent_id", "workspace_id", "user_id")
        if not isinstance(scope, dict) or any(key not in scope or not isinstance(scope[key], str) for key in keys):
            raise ValueError("identity_repair_complete_scope_required")
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    # Legacy agent/workspace aliases are admitted explicitly, within the same
    # tenant/user, including the existing store-authorized shared blank user.
    admitted = {(_scope_tuple(item)) for item in hongtu_query_scopes(scope_ref)}
    if is_hongtu_scope(scope_ref):
        admitted.update(_scope_tuple(ScopeRef.from_dict(resolve_channel_scope(channel, asdict(scope_ref))))
                        for channel in SUPPORTED_RUNTIME_CHANNELS)
    if scope_ref.user_id:
        admitted.update((tenant, agent, workspace, "") for tenant, agent, workspace, _ in list(admitted))

    def discover(sqlite):
        plans: list[dict[str, Any]] = []
        outcomes: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        discovered: set[tuple[str, ...]] = set()

        def capture():
            candidates = (candidate for exact in sorted(admitted)
                          for candidate in _iter_records(SimpleNamespace(store=sqlite), limit=limit, scope=ScopeRef(*exact)))
            for candidate in candidates:
                if _scope_tuple(candidate.scope) not in admitted:
                    continue
                identity = _exact_ref(candidate)
                if identity in discovered:
                    continue
                if limit is not None and limit > 0 and len(discovered) >= limit:
                    break
                discovered.add(identity)
                yield candidate
                if not needs_hongtu_identity_repair(candidate):
                    continue
                ref = _exact_ref(candidate)
                if ref in seen:
                    continue
                seen.add(ref)
                try:
                    current, version = _repair_snapshot(sqlite, ref)
                    _require_eligible(current, skip_created_at_or_after)
                    proposed = normalize_hongtu_record(current)
                    target = _exact_ref(proposed)
                    _require_identity_transition(current, proposed)
                    storage = sqlite.identity_repair_preflight(proposed)
                    if target != ref:
                        # Query the physical key WITHOUT source_id: its current
                        # uniqueness constraint does not include the source.
                        occupied = sqlite.execute(
                            "SELECT source_id FROM records WHERE storage_key = ? OR (tenant_id = ? AND agent_id = ? "
                            "AND workspace_id = ? AND user_id = ? AND record_id = ?)",
                            (storage["storage_key"], *target[:4], target[5]),
                        ).fetchone()
                        if occupied is not None:
                            raise _RepairSkipped("target_conflict")
                    if not storage.get("ok"):
                        raise _RepairSkipped(str(storage["reason"]))
                    if target == ref and storage["storage_key"] != version[2]:
                        raise _RepairSkipped("source_storage_key_mismatch")
                    plans.append({"source_ref": ref, "target_ref": target, "version": version})
                except _RepairSkipped as exc:
                    outcomes.append(_outcome(ref, exc.reason))

        report = build_identity_report(capture())
        return report, plans, outcomes

    report, plans, outcomes = runtime.store.read_consistent(discover)
    repaired_ids: list[str] = []
    if apply:
        for plan in plans:
            ref = plan["source_ref"]

            def mutate(sqlite):
                current, version = _repair_snapshot(sqlite, ref)
                if version != plan["version"]:
                    raise _RepairSkipped("source_version_changed")
                if _scope_tuple(current.scope) not in admitted:
                    raise _RepairSkipped("source_scope_not_admitted")
                _require_eligible(current, skip_created_at_or_after)
                proposed = normalize_hongtu_record(current)
                _require_identity_transition(current, proposed)
                if _exact_ref(proposed) != plan["target_ref"]:
                    raise _RepairSkipped("scope_move_unsupported")
                storage = sqlite.identity_repair_preflight(proposed, for_write=True)
                if not storage.get("ok"):
                    raise _RepairSkipped(str(storage["reason"]))
                if plan["target_ref"] == ref and storage["storage_key"] != version[2]:
                    raise _RepairSkipped("source_storage_key_mismatch")
                # The transaction owner has BEGIN IMMEDIATE. Guard the exact
                # snapshot version; do not fall back to updated_at-only CAS.
                changed = sqlite.execute(
                    "UPDATE records SET payload_digest = payload_digest WHERE storage_key = ? AND " + _REF_WHERE
                    + " AND payload_digest = ?",
                    (version[2], *ref, version[0]),
                ).rowcount
                if changed != 1:
                    raise _RepairSkipped("source_version_changed")
                if plan["target_ref"] != ref:
                    occupied = sqlite.execute("SELECT 1 FROM records WHERE storage_key = ?",
                                              (storage["storage_key"],)).fetchone()
                    if occupied is not None:
                        raise _RepairSkipped("target_conflict")
                sqlite.rewrite(proposed, previous_scope=current.scope, commit=False)
                return _outcome(ref, "applied"), [proposed], []

            try:
                outcome = runtime.store.mutate_records_atomically(mutate)
            except _RepairSkipped as exc:
                # Raising within the owner rolls back and avoids post-commit
                # projection/outbox flushing for a rejected/no-op candidate.
                outcome = _outcome(ref, exc.reason)
            except Exception as exc:
                outcome = _outcome(ref, "transaction_failed:" + type(exc).__name__)
            outcomes.append(outcome)
            if outcome["reason"] == "applied":
                repaired_ids.append(ref[5])

    benign = {"fresh", "ingest_stamped", "no_longer_eligible"}
    blocked = [item for item in outcomes if item["reason"] not in benign | {"applied"}]
    counts: dict[str, int] = {}
    for item in outcomes:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    status = "partial" if blocked and repaired_ids else "blocked" if blocked else "complete" if apply else "preview"
    return {
        **report,
        "report_phase": "discovery_snapshot",
        "ok": not blocked,
        "status": status,
        "apply": bool(apply),
        "scoped": True,
        "candidate_count": report["repair_candidate_count"],
        "eligible_in_place_count": len(plans),
        "repaired_count": len(repaired_ids),
        "repaired_record_ids": repaired_ids[:100],
        "skipped_fresh_count": counts.get("fresh", 0) + counts.get("ingest_stamped", 0),
        "blocked_count": len(blocked),
        "outcome_counts": counts,
        "repair_outcomes": outcomes[:100],
        "repair_outcomes_truncated": len(outcomes) > 100,
    }


def _scope_tuple(scope: ScopeRef) -> tuple[str, str, str, str]:
    return scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id


def _exact_ref(record: RecordEnvelope) -> tuple[str, ...]:
    return (*_scope_tuple(record.scope), record.source_id, record.record_id)


def _repair_snapshot(sqlite, ref: tuple[str, ...]) -> tuple[RecordEnvelope, tuple[str, ...]]:
    rows = sqlite.execute(
        "SELECT * FROM records WHERE tenant_id = ? AND agent_id = ? "
        "AND workspace_id = ? AND user_id = ? AND record_id = ?",
        (*ref[:4], ref[5]),
    ).fetchall()
    if len(rows) != 1:
        raise _RepairSkipped("source_missing" if not rows else "source_ambiguous")
    row = rows[0]
    if row["source_id"] != ref[4]:
        raise _RepairSkipped("source_identity_changed")
    digest = str(row["payload_digest"] or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise _RepairSkipped("source_version_unknown")
    # Archived compact projections need their own metadata-rewrite contract.
    # This bounded phase does not rewrite payload segments or archive pointers.
    if str(row["payload_pointer_json"] or ""):
        raise _RepairSkipped("archived_payload_unsupported")
    record = sqlite.get_by_exact_ref(
        ref[5], scope=ScopeRef(*ref[:4]), source_id=ref[4],
    )
    if (record is None or _exact_ref(record) != ref
            or record.kind != row["kind"] or record.status != row["status"]
            or record.source != row["source"]
            or record.time.created_at != row["created_at"]
            or record.time.updated_at != row["updated_at"]
            or any(getattr(record, key) != row[key] for key in ("title", "summary", "detail"))
            or payload_digest(record.to_dict()) != digest):
        raise _RepairSkipped("source_projection_or_digest_mismatch")
    try:
        if json.loads(str(row["meta_json"])) != record.meta:
            raise _RepairSkipped("source_projection_or_digest_mismatch")
    except (TypeError, ValueError) as exc:
        raise _RepairSkipped("source_projection_or_digest_mismatch") from exc
    physical_key = str(row["storage_key"] or "")
    if sqlite.identity_repair_preflight(record)["storage_key"] != physical_key:
        raise _RepairSkipped("source_storage_key_mismatch")
    # The second token binds every captured SQL projection column, including
    # metadata and all exact refs. Compare it inside BEGIN IMMEDIATE; the SQL
    # payload-CAS then cannot race another writer before the in-place upsert.
    return record, (digest, payload_digest(dict(row)), physical_key)


def _require_identity_transition(before: RecordEnvelope, after: RecordEnvelope) -> None:
    old = before.to_dict()
    new = after.to_dict()
    # Identity repair may alter agent/workspace, identity metadata and touch
    # updated_at, but cannot become a tenant/user/source/content mutation.
    for payload in (old, new):
        payload.pop("meta", None)
        payload["time"] = {key: value for key, value in payload["time"].items() if key != "updated_at"}
        payload["scope"] = {key: value for key, value in payload["scope"].items() if key not in {"agent_id", "workspace_id"}}
    if old != new:
        raise _RepairSkipped("identity_transition_forbidden")


def _require_eligible(record: RecordEnvelope, bound: str | None) -> None:
    if not needs_hongtu_identity_repair(record):
        raise _RepairSkipped("no_longer_eligible")
    if bool((record.meta or {}).get("identity_stamped_on_ingest")):
        raise _RepairSkipped("ingest_stamped")
    if bound:
        try:
            if _record_at_or_after(record, bound):
                raise _RepairSkipped("fresh")
        except ValueError as exc:
            raise _RepairSkipped("source_timestamp_unknown") from exc


def _outcome(ref: tuple[str, ...], reason: str) -> dict[str, Any]:
    return {"source_ref": {"tenant_id": ref[0], "agent_id": ref[1], "workspace_id": ref[2],
                           "user_id": ref[3], "source_id": ref[4], "record_id": ref[5]},
            "reason": reason}


def _utc_instant(value: str) -> datetime:
    try:
        instant = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("identity_repair_invalid_timestamp") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("identity_repair_timestamp_timezone_required")
    return instant.astimezone(timezone.utc)


def _record_at_or_after(record: RecordEnvelope, bound: str) -> bool:
    time_ref = getattr(record, "time", None)
    stamp = str(getattr(time_ref, "created_at", "") or getattr(time_ref, "updated_at", "") or "")
    return _utc_instant(stamp) >= _utc_instant(bound)


def _iter_records(
    runtime,
    *,
    limit: int | None = None,
    scope=None,
    skip_created_at_or_after: str | None = None,
) -> Iterator[RecordEnvelope]:
    page_size = 500
    offset = 0
    yielded_count = 0
    target_limit = None if limit is None or limit <= 0 else int(limit)
    while True:
        remaining = page_size if target_limit is None else max(0, min(page_size, target_limit - yielded_count))
        if remaining <= 0:
            break
        page = runtime.store.list_records(limit=remaining, offset=offset, scope=scope)
        if not page:
            break
        page_count = len(page)
        for record in page:
            if skip_created_at_or_after and _record_at_or_after(record, skip_created_at_or_after):
                continue
            if bool((getattr(record, "meta", {}) or {}).get("identity_stamped_on_ingest")):
                if not needs_hongtu_identity_repair(record):
                    continue
            yield record
            yielded_count += 1
        del record
        page.clear()
        offset += page_count
