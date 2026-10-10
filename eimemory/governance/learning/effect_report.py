"""Real-effect daily metrics; missing data never becomes success."""
from hashlib import sha256
import json
import math

from eimemory.governance.learning.effect_dataset import day_window, load_observations
from eimemory.models.records import RecordEnvelope, ScopeRef

SCHEMA = "recall.effect_daily_report.v1"
RATES = {
    "correction_rate": ("correction", "suspected", {"suspected", "none"}),
    "reask_rate": ("reask", "suspected", {"suspected", "none"}),
    "task_success_rate": ("task_success", "succeeded", {"succeeded", "failed"}),
    "tool_success_rate": ("tool_chain", "succeeded", {"succeeded", "failed"}),
    "negative_rating_rate": ("rating", "negative", {"positive", "negative"}),
}


def metrics(decisions):
    total = len(decisions)
    result = {"decisions": total, "decisions_with_signals": sum(bool(d["signal_ids"]) for d in decisions)}
    for name, (key, positive, known) in RATES.items():
        denominator = sum(d["labels"].get(key) in known for d in decisions)
        numerator = sum(d["labels"].get(key) == positive for d in decisions)
        result[name] = {"numerator": numerator, "denominator": denominator,
                        "value": numerator / denominator if denominator else None,
                        "unknown": total - denominator,
                        "coverage": denominator / total if total else None}
    latencies = sorted(float(d["labels"]["latency_ms"]) for d in decisions
                       if isinstance(d["labels"].get("latency_ms"), (int, float))
                       and math.isfinite(d["labels"]["latency_ms"]))
    result["latency_ms"] = {"samples": len(latencies), "p50": latencies[math.ceil(len(latencies) * .5) - 1] if latencies else None,
                             "p95": latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None}
    return result


def build_effect_report(runtime, *, channel="hermes", scope, source_ids=("default",),
                        report_date=None, timezone="Asia/Shanghai", persist=False):
    day, start, end = day_window(report_date, timezone)
    dataset = load_observations(runtime.store, channel=channel, scope=scope,
                                source_ids=source_ids, start=start, end=end)
    decisions = dataset.pop("decisions")
    groups = {}
    for d in decisions:
        key = json.dumps([d["release_identity"], d["policy_version"]], sort_keys=True)
        groups.setdefault(key, []).append(d)
    strata = []
    for key, samples in sorted(groups.items()):
        control = metrics([d for d in samples if d["control_cohort"]])
        treatment = metrics([d for d in samples if not d["control_cohort"]])
        release, policy = json.loads(key)
        strata.append({"release_identity": release, "policy_version": policy,
            "control": control, "treatment": treatment,
            "differences": {k: treatment[k]["value"] - control[k]["value"]
                            if treatment[k]["value"] is not None and control[k]["value"] is not None else None for k in RATES},
            "interpretation": "observational_unpaired", "certifies_improvement": False})
    evidence = sorted({s for d in decisions for s in d["signal_ids"]})
    report = {"ok": True, "schema": SCHEMA, **dataset, "date": day, "timezone": timezone,
              "window": {"start": start.isoformat(), "end": end.isoformat()},
              "metrics": metrics(decisions), "ab_strata": strata,
              "signal_ids": evidence, "effect_status": "observed" if evidence else "awaiting_signals",
              "certifies_l5": False, "certifies_improvement": False}
    report["summary"] = "Effect labels observed; task utility requires explicit outcomes." if evidence else "No real-effect labels observed; success is unknown."
    if persist:
        digest = sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()[:24]
        record = RecordEnvelope.create(kind="reflection", title=f"Real-effect daily report {day}",
            summary=report["summary"], scope=ScopeRef.from_dict(dataset["scope"]), status="archived",
            source="recall.effect_report", content=report,
            meta={"report_type": SCHEMA, "date": day, "memory_type": "evolution_artifact"})
        record.record_id = "effect_report_" + digest
        persisted = runtime.store.append(record, existing_match=lambda old: old.content == report)
        report["record_id"] = persisted.record_id
    return report
