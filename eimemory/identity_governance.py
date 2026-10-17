"""Retain original identity versions with a separately authorized current view."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import sqlite3

from eimemory.identity_authority import (
    IdentityAuthorityError, canonical, digest, snapshot,
)
from eimemory.models.records import RecordEnvelope, ScopeRef

SOURCE = "eimemory.identity_governance"
PARTITION = "identity-governance"
SCHEMA = "identity_dualview_decision.v1"


@contextmanager
def optional_authority(directory=None, *, blocking=True):
    """Unavailable authority never authorizes a persisted current-view decision."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        try:
            authority, _ = stack.enter_context(snapshot(directory, blocking=blocking))
        except (OSError, ValueError, ImportError):
            authority = None
        yield authority


def _content(grant):
    return {"schema": SCHEMA, "grant_id": grant["event_id"],
            "manifest_digest": grant["manifest_digest"], "authority_event_hash": grant["event_hash"],
            "policy_digest": grant["policy_digest"], "manifest": grant["manifest"]}


def _record_id(grant):
    return "idgd_" + digest(_content(grant))[:32]


def validate_pair(sqlite, manifest, item):
    from eimemory.identity_ops import _repair_snapshot, _exact_ref, normalize_hongtu_record, _RepairSkipped

    owner = manifest["owner_scope"]
    refs = []
    for side in ("source", "target"):
        ref = item.get(side + "_ref")
        version = item.get(side + "_version")
        if (not isinstance(ref, list) or len(ref) != 6 or not all(isinstance(v, str) for v in ref)
                or not isinstance(version, list) or len(version) != 3
                or not all(isinstance(v, str) and v for v in version)):
            raise IdentityAuthorityError("identity_decision_invalid_ref_or_version")
        refs.append(tuple(ref))
    old_ref, target_ref = refs
    owner_ref = tuple(owner[key] for key in ("tenant_id", "agent_id", "workspace_id", "user_id"))
    if (target_ref[:4] != owner_ref or old_ref == target_ref or old_ref[0] != target_ref[0]
            or old_ref[3:] != target_ref[3:] or old_ref[4] != manifest["source_id"]
            or item.get("decision") != "retain_both" or item.get("current") != "target"):
        raise IdentityAuthorityError("identity_decision_owner_source_mismatch")
    try:
        old, old_version = _repair_snapshot(sqlite, old_ref)
        current, current_version = _repair_snapshot(sqlite, target_ref)
    except _RepairSkipped as exc:
        raise IdentityAuthorityError("identity_decision_snapshot_" + exc.reason) from None
    if (old.kind != current.kind or old.source != current.source
            or _exact_ref(normalize_hongtu_record(old)) != target_ref
            or old.status != "active" or current.status != "active"):
        raise IdentityAuthorityError("identity_decision_pair_mismatch")
    if (list(old_version) != item["source_version"]
            or list(current_version) != item["target_version"]):
        raise IdentityAuthorityError("identity_decision_version_changed")
    return old, current


def apply_decision(runtime, *, grant_id, directory=None):
    """Recheck authority and both complete versions in the write transaction.

    Only a new governance record is written/exported. Neither original row,
    payload digest, exact ref, archive nor index is changed.
    """
    with snapshot(directory) as (authority, _):
        grant = authority.active(grant_id)
        if grant is None:
            raise IdentityAuthorityError("identity_authority_grant_not_active")
        manifest = grant["manifest"]
        owner = ScopeRef.from_dict(manifest["owner_scope"])

        def mutate(sqlite):
            for item in manifest["items"]:
                validate_pair(sqlite, manifest, item)
            record = RecordEnvelope.create(
                kind="evaluation_packet", title="Authorized identity current-view decision",
                summary="Both original versions retained; exact canonical target is current.",
                source=SOURCE, source_id=PARTITION, scope=owner, content=_content(grant),
                meta={"report_type": SCHEMA, "grant_id": grant_id,
                      "manifest_digest": grant["manifest_digest"]},
                provenance={"identity_scope_preserved": True},
            )
            record.record_id = _record_id(grant)
            existing = sqlite.get_by_exact_ref(record.record_id, scope=owner, source_id=PARTITION)
            if existing is not None:
                if not _valid_record(existing, grant):
                    raise IdentityAuthorityError("identity_decision_record_conflict")
            else:
                sqlite.upsert(record, commit=False)
            return {"ok": True, "grant_id": grant_id, "record_id": record.record_id,
                    "retained_pair_count": len(manifest["items"]), "original_records_changed": 0,
                    "idempotent": existing is not None}, ([] if existing is not None else [record]), []

        return runtime.store.mutate_records_atomically(mutate)


def _valid_record(record, grant):
    return bool(
        record.record_id == _record_id(grant) and record.kind == "evaluation_packet"
        and record.source == SOURCE and record.source_id == PARTITION and record.status == "active"
        and asdict(record.scope) == grant["manifest"]["owner_scope"]
        and canonical(record.content) == canonical(_content(grant))
        and record.meta.get("report_type") == SCHEMA
        and record.meta.get("grant_id") == grant["event_id"]
        and record.meta.get("manifest_digest") == grant["manifest_digest"]
        and record.provenance.get("identity_scope_preserved") is True
    )


class DecisionResolver:
    """Transaction-local resolution; no authority or version cache crosses reads."""

    def __init__(self, sqlite, authority):
        self.sqlite = sqlite
        self.authority = authority
        self.index = {}
        self.states = {}
        self.present = set()
        rows = sqlite.execute(
            "SELECT record_id,tenant_id,agent_id,workspace_id,user_id FROM records "
            "WHERE source=? AND kind='evaluation_packet' ORDER BY record_id LIMIT 1001", (SOURCE,),
        ).fetchall()
        if len(rows) > 1000:
            raise IdentityAuthorityError("identity_decision_capacity_exceeded")
        records = []
        for row in rows:
            record = sqlite.get_by_exact_ref(row["record_id"], scope=ScopeRef(
                tenant_id=row["tenant_id"], agent_id=row["agent_id"],
                workspace_id=row["workspace_id"], user_id=row["user_id"]), source_id=PARTITION)
            if record is None or not isinstance(record.content, dict):
                # A previously durable decision cannot disappear into success.
                raise IdentityAuthorityError("identity_decision_record_unavailable")
            self.present.add(id(record))
            records.append(record)
        if authority is not None:
            from types import SimpleNamespace

            for grant in authority.grants.values():
                if not any(_valid_record(record, grant) for record in records):
                    # Missing/deleted/unapplied decisions stay unresolved.
                    # This descriptor is never treated as a persisted decision.
                    records.append(SimpleNamespace(record_id=_record_id(grant), content=_content(grant),
                        scope=ScopeRef.from_dict(grant["manifest"]["owner_scope"])))
        for record in records:
            manifest = record.content.get("manifest")
            if not isinstance(manifest, dict) or not isinstance(manifest.get("items"), list):
                raise IdentityAuthorityError("identity_decision_record_invalid")
            for ordinal, item in enumerate(manifest["items"]):
                if not isinstance(item, dict):
                    raise IdentityAuthorityError("identity_decision_record_invalid")
                for side in ("source", "target"):
                    ref = item.get(side + "_ref")
                    if not isinstance(ref, list) or len(ref) != 6 or not all(isinstance(v, str) for v in ref):
                        raise IdentityAuthorityError("identity_decision_record_invalid")
                    self.index.setdefault(tuple(ref), []).append((record, ordinal))

    def _selected(self, ref):
        entries = self.index.get(tuple(ref), [])
        active = []
        for record, ordinal in entries:
            grant = self.authority.active(record.content.get("grant_id")) if self.authority else None
            if grant is not None and id(record) in self.present:
                active.append((record, ordinal, grant))
        return active[0] if len(active) == 1 else None

    def pair(self, old_ref, target_ref):
        selected = self._selected(old_ref)
        if selected is None:
            return False
        record, ordinal, grant = selected
        key = (record.record_id, ordinal)
        if key not in self.states:
            try:
                item = grant["manifest"]["items"][ordinal]
                if not _valid_record(record, grant):
                    raise IdentityAuthorityError("identity_decision_record_invalid")
                old, current = validate_pair(self.sqlite, grant["manifest"], item)
                self.states[key] = (old, current, item)
            except (KeyError, IndexError, ValueError, RuntimeError, sqlite3.Error):
                self.states[key] = None
            except Exception as exc:
                from eimemory.identity_ops import _RepairSkipped
                if not isinstance(exc, _RepairSkipped):
                    raise
                self.states[key] = None
        state = self.states[key]
        return bool(state and state[2]["source_ref"] == list(old_ref)
                    and state[2]["target_ref"] == list(target_ref))

    def visible(self, record, requested):
        from eimemory.identity_ops import _exact_ref

        ref = _exact_ref(record)
        entries = self.index.get(ref)
        if not entries:
            return True
        # Historical requests keep the original exact identities. Governance
        # acts only on this owner's canonical current view, never permission.
        if not any(requested == entry[0].scope for entry in entries):
            return True
        selected = self._selected(ref)
        if selected is None:
            return False
        decision, ordinal, grant = selected
        item = grant["manifest"]["items"][ordinal]
        if not self.pair(tuple(item["source_ref"]), tuple(item["target_ref"])):
            return False
        state = self.states[(decision.record_id, ordinal)]
        return bool(list(ref) == item["target_ref"]
                    and canonical(record.to_dict()) == canonical(state[1].to_dict()))


    def repair_disposition(self, ref, requested):
        entries = self.index.get(tuple(ref), [])
        if not any(requested == entry[0].scope for entry in entries):
            return None
        selected = self._selected(ref)
        if selected is not None:
            _, ordinal, grant = selected
            item = grant["manifest"]["items"][ordinal]
            if self.pair(tuple(item["source_ref"]), tuple(item["target_ref"])):
                return ("retained_both_current_canonical" if list(ref) == item["source_ref"]
                        else "retained_current_original")
        return "identity_decision_unresolved"

    def references_valid(self, record_ids, requested):
        for ref, entries in self.index.items():
            if ref[-1] not in record_ids or not any(requested == entry[0].scope for entry in entries):
                continue
            disposition = self.repair_disposition(ref, requested)
            if disposition == "identity_decision_unresolved":
                return False
        return True


def _referenced_ids(value):
    """Include auxiliary summaries that carry an ID but omit the full scope."""
    pending, found = [value], set()
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if isinstance(node.get("record_id"), str):
                found.add(node["record_id"])
            pending.extend(node.values())
        elif isinstance(node, (list, tuple)):
            pending.extend(node)
    return found


def filter_current_records(store, records, *, scope, references=None, deadline_at=0.0):
    """Apply the bound view to candidates; never inject a replacement result."""
    reference_ids = _referenced_ids(references) if references is not None else set()
    if not records and not reference_ids:
        return records, 0
    requested = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    with optional_authority(blocking=False) as authority:
        def read(sqlite):
            resolver = DecisionResolver(sqlite, authority)
            if reference_ids and not resolver.references_valid(reference_ids, requested):
                return [], max(1, len(records))
            kept = [record for record in records if resolver.visible(record, requested)]
            return kept, len(records) - len(kept)

        try:
            if deadline_at:
                from eimemory.storage.recall_deadline import recall_read_scope

                with recall_read_scope(store, {"_recall_collection_deadline_monotonic": deadline_at}):
                    kept, dropped = store.read_consistent(read)
            else:
                kept, dropped = store.read_consistent(read)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            # Corrupt/missing governance cannot authorize a current view.
            return [], max(1, len(records))
    return kept, dropped


def decision_status(runtime, *, grant_id, directory=None):
    with snapshot(directory) as (authority, _):
        grant = authority.grants.get(grant_id)
        if grant is None:
            raise IdentityAuthorityError("identity_authority_grant_missing")

        def read(sqlite):
            resolver = DecisionResolver(sqlite, authority)
            count = sum(resolver.pair(tuple(item["source_ref"]), tuple(item["target_ref"]))
                        for item in grant["manifest"]["items"])
            total = len(grant["manifest"]["items"])
            return {"ok": count == total, "grant_id": grant_id,
                    "retained_pair_count": count, "unresolved_count": total - count,
                    "original_records_changed": 0}

        return runtime.store.read_consistent(read)
