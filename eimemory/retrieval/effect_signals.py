"""Privacy-bounded observations; never verified task or L5 evidence."""
from __future__ import annotations

from hashlib import sha256
import json
import math
import re

from eimemory.core.clock import now_iso

SCHEMA = "recall.effect_signal.v1"
LABELS = {
    "tool_chain": {"succeeded", "failed", "unknown", "not_run"},
    "task_success": {"succeeded", "failed", "unknown"},
    "correction": {"suspected", "none", "unknown"},
    "reask": {"suspected", "none", "unknown"},
    "rating": {"positive", "negative", "unknown"},
}
PHASE_FIELDS = {
    "turn_completed": {"tool_chain", "task_success"},
    "next_user": {"correction", "reask"},
    "explicit_rating": {"rating"},
}


def validate_signal(phase, event_id, labels):
    if not isinstance(phase, str) or phase not in PHASE_FIELDS:
        raise ValueError("effect_signal_phase_invalid")
    if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", event_id):
        raise ValueError("effect_signal_event_id_invalid")
    if not isinstance(labels, dict) or not PHASE_FIELDS[phase] <= set(labels) or set(labels) - PHASE_FIELDS[phase] - {"latency_ms"}:
        raise ValueError("effect_signal_labels_invalid")
    for key, value in labels.items():
        if key == "latency_ms":
            if phase != "turn_completed" or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 86_400_000:
                raise ValueError("effect_signal_latency_invalid")
        elif not isinstance(value, str) or value not in LABELS[key]:
            raise ValueError("effect_signal_label_invalid")
    return dict(labels)


def record_signal(store, *, channel, scope, source_ids, session_id, turn_id,
                  decision_id, phase, event_id, labels):
    labels = validate_signal(phase, event_id, labels)
    with store._lock:
        conn = store.sqlite.conn
        if conn.in_transaction:
            raise ValueError("effect_signal_transaction_active")
        try:
            conn.execute("BEGIN IMMEDIATE")
            signal_id = sha256(f"{decision_id}\0{phase}\0{event_id}".encode()).hexdigest()
            old = conn.execute("SELECT payload_json FROM proactive_effect_signals WHERE signal_id=?", (signal_id,)).fetchone()
            if old:
                stored = json.loads(old[0])
                expected = {"decision_id": decision_id, "channel": channel, "scope": scope,
                            "source_ids": sorted(source_ids), "session_id": session_id, "turn_id": turn_id,
                            "phase": phase, "event_id": event_id, "labels": labels}
                if any(stored.get(k) != v for k, v in expected.items()):
                    raise ValueError("effect_signal_identity_conflict")
                conn.commit()
                return {"ok": True, "signal_id": signal_id, "replayed": True}
            decision = store.sqlite.load_proactive_decision(decision_id)
            if not decision or any((decision[key] != value) for key, value in {
                "channel": channel, "scope": scope, "session_id": session_id, "turn_id": turn_id,
            }.items()) or sorted(decision["source_ids"]) != sorted(source_ids):
                raise ValueError("effect_signal_decision_namespace_mismatch")
            if decision.get("acceptance_generated") is True:
                raise ValueError("effect_signal_acceptance_decision_rejected")
            payload = {
                "schema": SCHEMA, "decision_id": decision_id, "channel": channel,
                "scope": scope, "source_ids": sorted(source_ids), "session_id": session_id,
                "turn_id": turn_id, "phase": phase, "event_id": event_id, "labels": labels,
                "provenance": "host_observed", "verified_task_outcome": False,
                "release_identity": decision["release_identity"], "policy_version": decision["policy_version"],
                "decision_created_at": decision["created_at"],
                "real_effect_policy": decision.get("retrieval_diagnostics", {}).get("real_effect_policy", {}),
                "control_cohort": decision["control_cohort"], "pair_id": decision["pair_id"],
                # Attribute only actual model delivery, not offered memories.
                "injected_items": [{"record_id": i["record_id"], "source_id": i["source_id"], "citation": i["citation"],
                                    "mandatory": bool(i["mandatory"]),
                                    "record_ref": i.get("render_evidence", {}).get("effect_record_ref")}
                                   for i in decision["items"] if i["ever_injected"]],
            }
            body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            conn.execute("INSERT INTO proactive_effect_signals VALUES (?,?,?,?,?)",
                         (signal_id, decision_id, phase, body, now_iso()))
            conn.commit()
            return {"ok": True, "signal_id": signal_id, "replayed": bool(old)}
        except BaseException:
            conn.rollback()
            raise
