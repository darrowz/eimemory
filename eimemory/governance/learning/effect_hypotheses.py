"""Failures -> versioned attribution candidates, never assumed causality."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

from eimemory.governance.learning.effect_dataset import instant, load_observations
from eimemory.knowledge.evidence_contracts import versioned_record_ref
from eimemory.models.records import RecordEnvelope, ScopeRef

SCHEMA = "recall.effect_hypothesis.v1"
METRICS = {
    "task_failure_rate": ("task_success", "failed", {"failed", "succeeded"}),
    "negative_rating_rate": ("rating", "negative", {"positive", "negative"}),
    "correction_rate": ("correction", "suspected", {"none", "suspected"}),
    "reask_rate": ("reask", "suspected", {"none", "suspected"}),
}


def current_target(store, ref):
    if not isinstance(ref, dict) or set(ref) != {"record_id", "kind", "scope", "source_id", "version_digest"}:
        return None
    record = store.get_by_id(ref["record_id"], scope=ref["scope"], exact_scope=True)
    return record if record and versioned_record_ref(record) == ref and record.status == "active" else None


def produce_effect_hypotheses(runtime, *, scope, channel="hermes", source_ids=("default",),
                              at_time=None, lookback_days=7, min_decisions=10, persist=False):
    if not 1 <= lookback_days <= 30 or not 2 <= min_decisions <= 1000:
        raise ValueError("effect_hypothesis_bounds_invalid")
    now = instant(at_time) if at_time else datetime.now(timezone.utc)
    dataset = load_observations(runtime.store, channel=channel, scope=scope, source_ids=source_ids,
                                start=now - timedelta(days=lookback_days), end=now)
    groups, unattributed, protected, stale = {}, [], set(), set()
    for d in dataset["decisions"]:
        labels = d["labels"]
        negatives = [m for m, (key, negative, known) in METRICS.items() if labels.get(key) == negative]
        failure = bool(negatives) or labels.get("tool_chain") == "failed"
        injected = {}
        for signal in d["signals"]:
            for item in signal.get("injected_items", []):
                ref = item.get("record_ref")
                if not isinstance(ref, dict) or ref.get("scope") != dataset["scope"] or ref.get("source_id") not in dataset["source_ids"]:
                    continue
                identity = json.dumps(ref, sort_keys=True)
                injected[identity] = item
        if failure and not injected:
            unattributed.append({"decision_id": d["decision_id"], "signal_ids": d["signal_ids"],
                                 "reason": "missing_injected_version_refs"})
        for identity, item in injected.items():
            ref = item["record_ref"]
            record = current_target(runtime.store, ref)
            if not record:
                stale.add(ref["record_id"])
                continue
            from eimemory.retrieval.proactive import ProactiveRecallService
            if item.get("mandatory") or ProactiveRecallService._is_hard_policy(record):
                protected.add(ref["record_id"])
                continue
            for metric, (key, negative, known) in METRICS.items():
                if labels.get(key) not in known:
                    continue
                group_key = json.dumps([ref, d["release_identity"], d["policy_version"], metric], sort_keys=True)
                group = groups.setdefault(group_key, {"ref": ref, "release": d["release_identity"], "metric": metric,
                    "policy_version": d["policy_version"],
                    "decisions": {}, "sessions": set(), "signal_ids": set()})
                group["decisions"][d["decision_id"]] = labels[key] == negative
                group["sessions"].add(d.get("session_id") or next((s.get("session_id") for s in d["signals"]), ""))
                group["signal_ids"].update(d["signal_ids"])
    # A threshold trial needs negative evidence across several distinct optional
    # memories. Deduplicate decisions when several memories were delivered together.
    aggregate = {}
    for group in groups.values():
        key = json.dumps([group["release"], group["policy_version"], group["metric"]], sort_keys=True)
        item = aggregate.setdefault(key, {"ref": None, "release": group["release"], "metric": group["metric"],
            "policy_version": group["policy_version"],
            "decisions": {}, "sessions": set(), "signal_ids": set(), "refs": {}})
        item["decisions"].update(group["decisions"])
        item["sessions"].update(group["sessions"])
        item["signal_ids"].update(group["signal_ids"])
        if sum(group["decisions"].values()):
            item["refs"][json.dumps(group["ref"], sort_keys=True)] = group["ref"]
    candidates = list(groups.values()) + [g for g in aggregate.values() if len(g["refs"]) >= 3]
    hypotheses = []
    for group in candidates:
        count = len(group["decisions"])
        bad = sum(group["decisions"].values())
        if not bad:
            continue
        eligible = count >= min_decisions and len(group["sessions"]) >= 3 and bad / count >= .4
        payload = {"schema": SCHEMA, "channel": dataset["channel"], "scope": dataset["scope"],
            "source_ids": dataset["source_ids"], "target_ref": group["ref"], "release_identity": group["release"],
            "policy_version": group["policy_version"],
            "metric": group["metric"], "observed_decisions": count, "negative_decisions": bad,
            "negative_rate": bad / count, "sessions": len(group["sessions"]),
            "decision_ids": sorted(group["decisions"]), "signal_ids": sorted(group["signal_ids"]),
            "attribution": "cooccurrence_candidate", "causality_verified": False,
            "proposal": {"action": "lower_injection_weight", "max_decrement": .15} if group["ref"] else
                        {"action": "raise_injection_threshold", "max_increment": .05},
            "trial_status": "eligible_for_bounded_trial" if eligible else "awaiting_evidence",
            "allowed_side_effect": "recall_policy_data_only", "certifies_l5": False}
        if group["ref"] is None:
            payload["supporting_refs"] = [group["refs"][key] for key in sorted(group["refs"])]
        payload["hypothesis_id"] = "effect_hypothesis_" + sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
        if persist:
            record = RecordEnvelope.create(kind="reflection", source="recall.effect_hypothesis", status="archived",
                scope=ScopeRef.from_dict(dataset["scope"]), title="Real-effect attribution hypothesis",
                summary="Observed negative cooccurrence; verify by bounded independent trial.", content=payload,
                meta={"report_type": SCHEMA, "memory_type": "evolution_artifact"})
            record.record_id = payload["hypothesis_id"]
            runtime.store.append(record, existing_match=lambda old, value=payload: old.content == value)
        hypotheses.append(payload)
    hypotheses.sort(key=lambda h: (h["trial_status"] == "eligible_for_bounded_trial", h["target_ref"] is not None,
                                   h["negative_decisions"], h["hypothesis_id"]), reverse=True)
    return {"ok": True, "schema": "recall.effect_hypothesis_batch.v1", "channel": dataset["channel"],
            "scope": dataset["scope"], "source_ids": dataset["source_ids"], "hypotheses": hypotheses,
            "unattributed_failures": unattributed, "protected_targets": sorted(protected), "stale_targets": sorted(stale),
            "certifies_l5": False}
