"""Bounded recurring controller over administrator-signed exact owner grants."""
import json
import os
from pathlib import Path

from eimemory.governance.learning.effect_learning import run_effect_cycle
from eimemory.governance.learning.effect_policy import POLICY_SCHEMA, verify
from eimemory.governance.tool_receipts import _read_secure_file


def run_effect_tick(runtime, *, apply=True):
    override = os.environ.get("EIMEMORY_REAL_EFFECT_POLICY_FILE", "").strip()
    paths = [Path(override)] if override else sorted((runtime.store.root / "state").glob("real-effect-policy-*.json"))
    if len(paths) > 16:
        return {"ok": False, "status": "blocked", "reason": "effect_grant_scan_limit", "applied": False}
    reports, owners = [], set()
    for path in paths:
        try:
            raw = _read_secure_file(path, max_bytes=32768)
            grant = json.loads(raw) if raw else None
            if not verify(grant, POLICY_SCHEMA):
                raise ValueError("effect_policy_signature_or_permissions_invalid")
            owner = json.dumps([grant["channel"], grant["scope"], grant["source_ids"]], sort_keys=True)
            if owner in owners:
                raise ValueError("effect_duplicate_owner_grant")
            owners.add(owner)
            report = run_effect_cycle(runtime, channel=grant["channel"], scope=grant["scope"],
                                      source_ids=grant["source_ids"], apply=apply)
        except (ValueError, KeyError, TypeError) as exc:
            report = {"ok": False, "status": "blocked", "reason": str(exc), "applied": False}
        reports.append({"policy_path": str(path), **report})
    return {"ok": all(r.get("ok") and r.get("status") != "blocked" for r in reports), "status": "completed" if reports else "awaiting_scoped_policy",
            "reports": reports, "applied_count": sum(bool(r.get("applied")) for r in reports),
            "code_changes_authorized": False, "certifies_l5": False}
