"""Exact-scope reversible quarantine, with reviewed manifests for legacy data."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json

from eimemory.knowledge.evidence_contracts import versioned_record_ref
from eimemory.models.records import RecordEnvelope, ScopeRef

SCHEMA = "test_rule_quarantine.v1"
MARKER = "test_rule_quarantine"


def explicitly_test_generated(record):
    return record.kind == "rule" and any(
        container.get("acceptance_generated") is True or container.get("test_generated") is True
        for container in (record.meta, record.content, record.provenance)
    )


def guard_rule_write(record, existing=None):
    if record.kind != "rule":
        return
    # A later deployment write must not resurrect a quarantined legacy id.
    quarantine = (existing.meta.get(MARKER) if existing else None)
    if quarantine and not record.meta.get(MARKER):
        record.meta[MARKER] = deepcopy(quarantine)
    if record.meta.get(MARKER) and record.status != "deprecated":
        raise ValueError("quarantined_test_rule_cannot_activate")
    if explicitly_test_generated(record) and record.status not in {"archived", "deprecated"}:
        record.status = "archived"


def quarantine_test_rules(store, *, scope, source_id="default", apply=False,
                          manifest=None, revert=False, limit=5000):
    base = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    if not all((base.agent_id, base.workspace_id, base.user_id)):
        raise ValueError("test_rule_quarantine_exact_owner_required")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
        raise ValueError("test_rule_quarantine_limit_invalid")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("test_rule_quarantine_source_required")
    requested = {}
    if manifest is not None:
        if not isinstance(manifest, dict) or set(manifest) != {"schema", "scope", "source_id", "rules"} or manifest["schema"] != SCHEMA or manifest["scope"] != asdict(base) or manifest["source_id"] != source_id or not isinstance(manifest["rules"], list):
            raise ValueError("test_rule_manifest_invalid")
        for item in manifest["rules"]:
            if not isinstance(item, dict) or set(item) != {"record_id", "version_digest"} or any(not isinstance(v, str) or not v for v in item.values()) or item["record_id"] in requested:
                raise ValueError("test_rule_manifest_invalid")
            requested[item["record_id"]] = item["version_digest"]
    report = {"schema": SCHEMA, "mode": "restore" if revert else "quarantine", "applied": apply,
              "scanned": 0, "eligible": 0, "changed": 0, "changes": [], "unclassified": [],
              "manifest_template": {"schema": SCHEMA, "scope": asdict(base), "source_id": source_id, "rules": []}}

    def mutation(sqlite):
        records = sqlite.list_records(kinds=["rule"], scope=base, source_ids=[source_id], limit=limit + 1)
        if len(records) > limit:
            raise ValueError("test_rule_quarantine_scan_incomplete")
        records = [r for r in records if r.scope == base and r.source_id == source_id]
        missing = set(requested) - {r.record_id for r in records}
        if missing:
            raise ValueError("test_rule_manifest_missing_or_wrong_scope")
        changed = []
        for r in records:
            report["scanned"] += 1
            ref = versioned_record_ref(r)
            selected = r.record_id in requested
            if selected and requested[r.record_id] != ref["version_digest"]:
                raise ValueError("test_rule_manifest_stale")
            marker = r.meta.get(MARKER)
            if manifest is not None and not selected:
                continue
            if revert:
                if not marker or (manifest is not None and not selected):
                    continue
                original = RecordEnvelope.from_dict(marker["original"])
                if r.status != "deprecated" or versioned_record_ref(original)["version_digest"] != marker["original_digest"]:
                    raise ValueError("test_rule_restore_conflict")
                expected = deepcopy(original)
                expected.status = "deprecated"
                expected.meta[MARKER] = marker
                # Only the quarantine envelope may be restored; reject edits made since.
                if r.to_dict() != expected.to_dict():
                    raise ValueError("test_rule_restore_conflict")
                if explicitly_test_generated(original) and original.status not in {"archived", "deprecated"}:
                    original.status = "archived"
                new = original
            else:
                if marker or r.status in {"archived", "deprecated"}:
                    continue
                if not selected and not explicitly_test_generated(r):
                    item = {"record_id": r.record_id, "version_digest": ref["version_digest"]}
                    report["unclassified"].append(item)
                    continue
                new = deepcopy(r)
                new.status = "deprecated"
                new.meta[MARKER] = {"schema": SCHEMA, "original_digest": ref["version_digest"],
                                    "reason": "reviewed_manifest" if selected else "explicit_test_provenance",
                                    "original": r.to_dict()}
            report["changes"].append({"record_id": r.record_id, "source_id": r.source_id,
                                       "version_digest": ref["version_digest"], "old_status": r.status,
                                       "new_status": new.status})
            if apply:
                sqlite.upsert(new, commit=False, allow_quarantine_restore=revert)
                # Archived audit records cannot enter recall or rule promotion.
                audit = RecordEnvelope.create(kind="reflection", source="storage.test_rule_quarantine",
                    scope=base, source_id=source_id, status="archived", title="Test rule quarantine receipt",
                    summary=report["mode"], content={"schema": SCHEMA, "action": report["mode"],
                        "before": ref, "after": versioned_record_ref(new)}, meta={"memory_type": "evolution_artifact"})
                sqlite.upsert(audit, commit=False)
                changed.extend([new, audit])
                report["changed"] += 1
        report["eligible"] = len(report["changes"])
        report["plan_digest"] = sha256(json.dumps(report["changes"], sort_keys=True).encode()).hexdigest()
        return report, changed, []

    if apply:
        return store.mutate_records_atomically(mutation)
    with store._lock:
        return mutation(store.sqlite)[0]
