"""Signed, restartable data trials consumed by the real proactive recall path."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from zoneinfo import ZoneInfo

from eimemory.governance.learning.effect_dataset import instant, load_observations, namespace
from eimemory.governance.learning.effect_hypotheses import METRICS, current_target, produce_effect_hypotheses
from eimemory.governance.learning.effect_policy import (
    RECEIPT_SCHEMA, canonical, digest, load_effect_policy, namespace_key, seal, stopped, verify,
)
from eimemory.models.records import RecordEnvelope, ScopeRef

SCHEMA = "recall.real_effect_cycle.v1"


def initial_state(key):
    return {"namespace_key": key, "revision": 0, "weights": {}, "threshold": .7, "trial": None, "monitor": None, "cooldowns": {}}


def _check_settings(settings, policy):
    limits = policy["limits"]
    if not isinstance(settings, dict) or set(settings) != {"weights", "threshold"}:
        raise ValueError("effect_state_settings_invalid")
    threshold = settings["threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not .7 <= threshold <= limits["max_threshold"]:
        raise ValueError("effect_state_settings_invalid")
    if not isinstance(settings["weights"], dict) or len(settings["weights"]) > limits["max_targets"]:
        raise ValueError("effect_state_settings_invalid")
    for key, entry in settings["weights"].items():
        ref = entry.get("ref") if isinstance(entry, dict) else None
        weight = entry.get("weight") if isinstance(entry, dict) else None
        if (not isinstance(ref, dict) or set(ref) != {"record_id", "kind", "scope", "source_id", "version_digest"}
                or digest(ref) != key or ref["scope"] != policy["scope"] or ref["source_id"] not in policy["source_ids"]
                or isinstance(weight, bool) or not isinstance(weight, (int, float)) or not limits["min_weight"] <= weight <= 1):
            raise ValueError("effect_state_settings_invalid")


def read_state(store, policy, *, full_chain=False):
    key = namespace_key(policy["channel"], policy["scope"], policy["source_ids"])
    with store._lock:
        conn = store.sqlite.conn
        row = conn.execute("SELECT payload_json,receipt_json FROM real_effect_states WHERE namespace_key=?", (key,)).fetchone()
        ledger = conn.execute("SELECT payload_json,receipt_json FROM real_effect_ledger WHERE namespace_key=? ORDER BY sequence " + ("ASC" if full_chain else "DESC LIMIT 1"), (key,)).fetchall()
    if row is None:
        if ledger:
            raise ValueError("effect_state_missing_from_ledger")
        return initial_state(key), None
    state, receipt = json.loads(row[0]), json.loads(row[1])
    if set(state) != {"namespace_key", "revision", "weights", "threshold", "trial", "monitor", "cooldowns"} or state["namespace_key"] != key:
        raise ValueError("effect_state_namespace_invalid")
    if not verify(receipt, RECEIPT_SCHEMA) or receipt.get("state_digest") != digest(state) or receipt.get("namespace_key") != key or receipt.get("sequence") != state["revision"]:
        raise ValueError("effect_state_signature_invalid")
    if not ledger or json.loads(ledger[-1 if full_chain else 0][1]) != receipt or json.loads(ledger[-1 if full_chain else 0][0]) != state:
        raise ValueError("effect_state_ledger_mismatch")
    if full_chain:
        previous = "0" * 64
        previous_state = digest(initial_state(key))
        for sequence, item in enumerate(ledger, 1):
            body, signed = json.loads(item[0]), json.loads(item[1])
            # The verified latest HMAC anchors every earlier receipt hash. This
            # preserves integrity after old verification keys leave the keyring.
            if (signed.get("schema") != RECEIPT_SCHEMA or signed.get("namespace_key") != key
                    or signed.get("state_digest") != digest(body) or signed.get("sequence") != sequence
                    or signed.get("previous_receipt_digest") != previous
                    or signed.get("before_state_digest") != previous_state):
                raise ValueError("effect_receipt_chain_invalid")
            previous = digest(signed)
            previous_state = digest(body)
    _check_settings({"weights": state["weights"], "threshold": state["threshold"]}, policy)
    if state["trial"]:
        _check_settings(state["trial"]["candidate"], policy)
    if state["monitor"]:
        _check_settings(state["monitor"]["previous"], policy)
    return state, receipt


def recall_policy_view(store, *, channel, scope, source_ids, session_id, acceptance_generated=False):
    disabled = {"enabled": False, "weights": {}, "threshold": .7, "version_suffix": "", "context": {}}
    if acceptance_generated:
        return {**disabled, "reason": "acceptance_generated"}
    try:
        policy = load_effect_policy(store, channel=channel, scope=scope, source_ids=source_ids)
        state, _ = read_state(store, policy)
    except (ValueError, KeyError, TypeError):
        return {**disabled, "reason": "effect_policy_inactive_or_invalid"}
    settings = {"weights": state["weights"], "threshold": state["threshold"]}
    context = {"policy_id": policy["policy_id"], "state_revision": state["revision"], "trial_id": "", "arm": "committed"}
    if state["monitor"]:
        context["monitor_id"] = state["monitor"]["monitor_id"]
    trial = state["trial"]
    if trial:
        now = datetime.now(timezone.utc)
        if trial["policy_id"] != policy["policy_id"] or now >= instant(trial["expires_at"]):
            context["arm"] = "baseline"
        else:
            # Separate assignment domain from the existing proactive suppression cohort.
            bucket = int(digest(["effect-session-assignment", trial["trial_id"], str(session_id)])[:8], 16) % 100
            candidate = bucket < policy["limits"]["canary_percent"]
            context.update(trial_id=trial["trial_id"], arm="candidate" if candidate else "baseline")
            if candidate:
                settings = trial["candidate"]
    return {"enabled": True, **settings, "context": context,
            "baseline_settings": {"weights": state["weights"], "threshold": state["threshold"]},
            "version_suffix": "+effect:" + digest([policy["policy_id"], state["revision"]])[:16]}


def apply_recall_policy(details, view, *, mandatory):
    if not view["enabled"]:
        return details
    from eimemory.knowledge.evidence_contracts import versioned_record_ref
    weighted = []
    changed = False
    for record, confidence in details:
        if mandatory(record):
            weighted.append((record, confidence))
            continue
        entry = view["weights"].get(digest(versioned_record_ref(record)))
        changed = changed or bool(entry) or view["threshold"] > .7
        weight = entry["weight"] if entry else 1.
        adjusted = round(confidence * weight, 4)
        if (entry or view["threshold"] > .7) and adjusted < view["threshold"]:
            continue
        weighted.append((record, adjusted))
    return sorted(weighted, key=lambda item: item[1], reverse=True) if changed else details


def _commit(runtime, policy, before, after, action, *, now, evidence_ids=(), hypothesis_id="", evaluation=None, reason=""):
    key = before["namespace_key"]
    date = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    def mutation(sqlite):
        rollback = action in {"trial_rolled_back", "committed_rolled_back"}
        fresh = load_effect_policy(runtime.store, channel=policy["channel"], scope=policy["scope"], source_ids=policy["source_ids"], now=now,
                                   allow_stopped=rollback, allow_expired=rollback)
        if fresh["policy_id"] != policy["policy_id"]:
            raise ValueError("effect_policy_changed_during_apply")
        current, receipt = read_state(runtime.store, fresh, full_chain=True)
        if current != before:
            raise ValueError("effect_state_concurrent_change")
        if action in {"trial_started", "trial_kept"}:
            used = sqlite.conn.execute("SELECT COUNT(*) FROM real_effect_ledger WHERE namespace_key=? AND record_date=? AND action IN ('trial_started','trial_kept')", (key, date)).fetchone()[0]
            if used >= policy["limits"]["daily_changes"]:
                raise ValueError("effect_daily_budget_exhausted")
            if stopped(runtime.store):
                raise ValueError("effect_kill_switch_present")
        target_ref = (after["trial"] if action == "trial_started" else before["trial"] or {}).get("target_ref")
        if action in {"trial_started", "trial_kept"} and target_ref:
            target = current_target(runtime.store, target_ref)
            from eimemory.retrieval.proactive import ProactiveRecallService
            if target is None or ProactiveRecallService._is_hard_policy(target):
                raise ValueError("effect_trial_target_stale_or_protected")
        after["revision"] = before["revision"] + 1
        signed = seal({"schema": RECEIPT_SCHEMA, "namespace_key": key, "sequence": after["revision"],
            "policy_id": policy["policy_id"], "action": action, "created_at": now.isoformat(),
            "state_digest": digest(after), "before_state_digest": digest(before),
            "previous_receipt_digest": digest(receipt) if receipt else "0" * 64,
            "evidence_ids": sorted(set(evidence_ids)), "hypothesis_id": hypothesis_id,
            "trial_id": (after["trial"] or before["trial"] or before["monitor"] or {}).get("trial_id", ""),
            "evaluation": evaluation, "reason": reason, "code_changes_authorized": False}, RECEIPT_SCHEMA)
        sqlite.conn.execute("INSERT INTO real_effect_ledger VALUES (?,?,?,?,?,?,?)",
            (key, after["revision"], action, now.isoformat(), date, canonical(after), canonical(signed)))
        sqlite.conn.execute("INSERT INTO real_effect_states VALUES (?,?,?) ON CONFLICT(namespace_key) DO UPDATE SET payload_json=excluded.payload_json,receipt_json=excluded.receipt_json",
            (key, canonical(after), canonical(signed)))
        audit = RecordEnvelope.create(kind="reflection", status="archived", source="recall.real_effect_receipt",
            scope=ScopeRef.from_dict(policy["scope"]), title="Real-effect data change receipt", summary=action,
            content={"receipt": signed, "state": after}, meta={"memory_type": "evolution_artifact", "report_type": RECEIPT_SCHEMA})
        audit.record_id = "effect_receipt_" + digest(signed)[:24]
        sqlite.upsert(audit, commit=False)
        return signed, [audit], []
    return runtime.store.mutate_records_atomically(mutation)


def _wilson(bad, total):
    if not total:
        return (0., 1.)
    z = 1.96
    rate = bad / total
    factor = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / factor
    radius = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / factor
    return center - radius, center + radius


def _rate(samples, metric):
    key, negative, known = METRICS[metric]
    sessions = {}
    for d in samples:
        session = d.get("session_id") or next((s.get("session_id") for s in d["signals"]), "")
        if not session:
            continue
        sessions.setdefault(session, []).append(d["labels"].get(key))
    count = sum(any(value in known for value in values) for values in sessions.values())
    bad = sum(negative in values for values in sessions.values())
    return {"samples": count, "bad": bad, "rate": bad / count if count else None,
            "unit": "session", "decisions": len(samples),
            "coverage": count / len(sessions) if sessions else 0., "interval": _wilson(bad, count)}


def evaluate_trial(runtime, policy, trial, *, now):
    dataset = load_observations(runtime.store, channel=policy["channel"], scope=policy["scope"], source_ids=policy["source_ids"],
                                start=instant(trial["started_at"]), end=now)
    arms = {"baseline": [], "candidate": []}
    for d in dataset["decisions"]:
        ctx = d.get("retrieval_diagnostics", {}).get("real_effect_policy", {})
        if ctx.get("trial_id") == trial["trial_id"] and (d["release_identity"] != trial["release_identity"]
                or d["policy_version"].split("+effect:", 1)[0] != trial["base_policy_version"]):
            return "rollback", "trial_release_or_policy_changed", {"signal_ids": d["signal_ids"], "certifies_l5": False}
        if ctx.get("trial_id") == trial["trial_id"] and ctx.get("arm") in arms and not d["control_cohort"]:
            arms[ctx["arm"]].append(d)
    primary = {arm: _rate(samples, trial["metric"]) for arm, samples in arms.items()}
    evidence = sorted({s for samples in arms.values() for d in samples for s in d["signal_ids"]})
    guards = {metric: {arm: _rate(samples, metric) for arm, samples in arms.items()} for metric in METRICS}
    latency = {}
    for arm, samples in arms.items():
        values = sorted(d["labels"]["latency_ms"] for d in samples if type(d["labels"].get("latency_ms")) in {int, float})
        latency[arm] = {"samples": len(values), "coverage": len(values) / len(samples) if samples else 0.,
                        "p95": values[math.ceil(len(values) * .95) - 1] if values else None}
    result = {"primary": primary, "guard_metrics": guards, "latency_ms": latency,
              "signal_ids": evidence, "decision_ids": sorted(d["decision_id"] for samples in arms.values() for d in samples),
              "comparison": "session_randomized_single_change", "certifies_l5": False}
    exposed = {d.get("session_id") or next((s.get("session_id") for s in d["signals"]), "")
               for d in arms["candidate"] if d.get("retrieval_diagnostics", {}).get("real_effect_policy", {}).get("delivery_changed") is True}
    exposed.discard("")
    result["candidate_changed_delivery_sessions"] = len(exposed)
    for comparison in guards.values():
        b, c = comparison["baseline"], comparison["candidate"]
        if min(b["samples"], c["samples"]) >= 5 and c["interval"][0] > b["interval"][1]:
            return "rollback", "observed_metric_regression", result
    b, c = latency["baseline"], latency["candidate"]
    if min(b["samples"], c["samples"]) >= 5 and c["p95"] > max(b["p95"] * 1.5, b["p95"] + 100):
        return "rollback", "observed_latency_regression", result
    if now >= instant(trial["expires_at"]):
        return "rollback", "trial_expired_without_qualified_improvement", result
    b, c = primary["baseline"], primary["candidate"]
    enough = min(b["samples"], c["samples"]) >= policy["limits"]["min_trial_samples"]
    coverage = min(b["coverage"], c["coverage"]) >= .6 and abs(b["coverage"] - c["coverage"]) <= .15
    timing = (min(v["coverage"] for v in latency.values()) >= .6
              and latency["candidate"]["p95"] <= max(latency["baseline"]["p95"] * 1.25, latency["baseline"]["p95"] + 50))
    improved = c["interval"][1] < b["interval"][0]
    if enough and coverage and timing and improved and len(exposed) >= policy["limits"]["min_trial_samples"]:
        return "keep", "qualified_primary_metric_improvement", result
    return "wait", "awaiting_comparable_effect_samples", result


def run_effect_cycle(runtime, *, scope, channel="hermes", source_ids=("default",), apply=False, at_time=None):
    now = instant(at_time) if at_time else datetime.now(timezone.utc)
    channel, scope, sources = namespace(channel, scope, source_ids)
    result = {"ok": True, "schema": SCHEMA, "channel": channel, "scope": scope, "source_ids": sources,
              "applied": False, "code_changes_authorized": False, "certifies_l5": False}
    try:
        policy = load_effect_policy(runtime.store, channel=channel, scope=scope, source_ids=sources, now=now, allow_stopped=True, allow_expired=True)
        before, receipt = read_state(runtime.store, policy, full_chain=True)
    except (ValueError, KeyError, TypeError) as exc:
        return {**result, "status": "blocked", "reason": str(exc)}
    result.update(policy_id=policy["policy_id"], state_revision=before["revision"])
    after = deepcopy(before)
    trial = before["trial"]
    action, reason, evidence, hypothesis_id, evaluation = "", "", [], "", None
    if trial:
        target = trial.get("target_ref")
        if stopped(runtime.store):
            decision, reason = "rollback", "effect_kill_switch_present"
        elif now >= instant(policy["expires_at"]):
            decision, reason = "rollback", "effect_policy_expired"
        elif trial["policy_id"] != policy["policy_id"]:
            decision, reason = "rollback", "effect_policy_reissued"
        elif target and current_target(runtime.store, target) is None:
            decision, reason = "rollback", "trial_target_changed_or_deleted"
        else:
            decision, reason, evaluation = evaluate_trial(runtime, policy, trial, now=now)
            evidence = evaluation["signal_ids"]
        if decision == "wait":
            return {**result, "status": "observing", "reason": reason, "trial_id": trial["trial_id"], "evaluation": evaluation}
        action = "trial_kept" if decision == "keep" else "trial_rolled_back"
        if decision == "keep":
            after.update(deepcopy(trial["candidate"]))
            after["monitor"] = {"monitor_id": trial["trial_id"], "trial_id": trial["trial_id"],
                "release_identity": trial["release_identity"], "base_policy_version": trial["base_policy_version"],
                "policy_id": policy["policy_id"], "target_key": trial["target_key"],
                "previous": {"weights": deepcopy(before["weights"]), "threshold": before["threshold"]},
                "evaluation": evaluation, "started_at": now.isoformat(),
                "expires_at": (now + timedelta(days=7)).isoformat()}
        after["trial"] = None
        after["cooldowns"][trial["target_key"]] = (now + timedelta(days=30 if decision == "keep" else 7)).isoformat()
        hypothesis_id = trial["hypothesis_id"]
    elif before["monitor"]:
        monitor = before["monitor"]
        decision, reason, evaluation = evaluate_monitor(runtime, policy, monitor, now=now)
        if decision == "wait":
            return {**result, "status": "monitoring", "reason": reason, "monitor_id": monitor["monitor_id"], "evaluation": evaluation}
        action = "committed_rolled_back" if decision == "rollback" else "monitor_completed"
        evidence = (evaluation or {}).get("signal_ids", [])
        if decision == "rollback":
            after.update(deepcopy(monitor["previous"]))
            after["cooldowns"][monitor["target_key"]] = (now + timedelta(days=7)).isoformat()
        after["monitor"] = None
    else:
        if now >= instant(policy["expires_at"]):
            return {**result, "status": "blocked", "reason": "effect_policy_expired"}
        if stopped(runtime.store):
            return {**result, "status": "stopped", "reason": "effect_kill_switch_present"}
        hypotheses = produce_effect_hypotheses(runtime, channel=channel, scope=scope, source_ids=sources,
            at_time=now.isoformat(), min_decisions=policy["limits"]["min_hypothesis_decisions"], persist=apply)
        for h in hypotheses["hypotheses"]:
            if h["trial_status"] != "eligible_for_bounded_trial" or h["proposal"]["action"] not in policy["actions"]:
                continue
            target = h["target_ref"]
            target_key = digest(target) if target else "injection_threshold"
            cooldown = before["cooldowns"].get(target_key)
            if cooldown and instant(cooldown) > now:
                continue
            candidate = {"weights": deepcopy(before["weights"]), "threshold": before["threshold"]}
            if target:
                weight = candidate["weights"].get(target_key, {}).get("weight", 1.)
                if weight <= policy["limits"]["min_weight"] or len(candidate["weights"]) >= policy["limits"]["max_targets"] and target_key not in candidate["weights"]:
                    continue
                candidate["weights"][target_key] = {"ref": target, "weight": round(max(policy["limits"]["min_weight"], weight - policy["limits"]["weight_step"]), 4)}
            else:
                if candidate["threshold"] >= policy["limits"]["max_threshold"]:
                    continue
                candidate["threshold"] = round(min(policy["limits"]["max_threshold"], candidate["threshold"] + policy["limits"]["threshold_step"]), 4)
            after["trial"] = {"trial_id": "effect_trial_" + digest([h["hypothesis_id"], before["revision"], policy["policy_id"]])[:24],
                "hypothesis_id": h["hypothesis_id"], "policy_id": policy["policy_id"], "target_ref": target,
                "release_identity": h["release_identity"], "base_policy_version": h["policy_version"].split("+effect:", 1)[0],
                "target_key": target_key, "metric": h["metric"], "candidate": candidate,
                "started_at": now.isoformat(), "expires_at": (now + timedelta(days=policy["limits"]["trial_days"])).isoformat()}
            action, reason, evidence, hypothesis_id = "trial_started", "bounded_hypothesis_trial", h["signal_ids"], h["hypothesis_id"]
            break
        if not action:
            return {**result, "status": "awaiting_evidence", "hypothesis_count": len(hypotheses["hypotheses"])}
    result.update(status=action, reason=reason, evaluation=evaluation, proposed_state=after)
    if apply:
        try:
            signed = _commit(runtime, policy, before, after, action, now=now, evidence_ids=evidence,
                             hypothesis_id=hypothesis_id, evaluation=evaluation, reason=reason)
        except ValueError as exc:
            return {**result, "status": "blocked", "reason": str(exc)}
        result.update(applied=True, receipt=signed, state_revision=after["revision"])
    return result


def evaluate_monitor(runtime, policy, monitor, *, now):
    if stopped(runtime.store):
        return "rollback", "effect_kill_switch_present", None
    if now >= instant(policy["expires_at"]) or monitor["policy_id"] != policy["policy_id"]:
        return "rollback", "effect_policy_expired_or_reissued", None
    dataset = load_observations(runtime.store, channel=policy["channel"], scope=policy["scope"], source_ids=policy["source_ids"],
                                start=instant(monitor["started_at"]), end=now)
    samples = [d for d in dataset["decisions"] if not d["control_cohort"]
               and d.get("retrieval_diagnostics", {}).get("real_effect_policy", {}).get("monitor_id") == monitor["monitor_id"]]
    if any(d["release_identity"] != monitor["release_identity"]
           or d["policy_version"].split("+effect:", 1)[0] != monitor["base_policy_version"] for d in samples):
        return "rollback", "monitor_release_or_policy_changed", {"signal_ids": sorted({s for d in samples for s in d["signal_ids"]})}
    guards = {metric: _rate(samples, metric) for metric in METRICS}
    values = sorted(d["labels"]["latency_ms"] for d in samples if type(d["labels"].get("latency_ms")) in {int, float})
    latency = {"samples": len(values), "p95": values[math.ceil(len(values) * .95) - 1] if values else None}
    result = {"guard_metrics": guards, "latency_ms": latency,
        "signal_ids": sorted({s for d in samples for s in d["signal_ids"]}),
        "comparison": "post_promotion_drift_guard", "certifies_improvement": False, "certifies_l5": False}
    for metric, current in guards.items():
        reference = monitor["evaluation"]["guard_metrics"][metric]["candidate"]
        if min(reference["samples"], current["samples"]) >= 5 and current["interval"][0] > reference["interval"][1]:
            return "rollback", "post_promotion_metric_regression", result
    reference = monitor["evaluation"]["latency_ms"]["candidate"]
    if min(reference["samples"], latency["samples"]) >= 5 and latency["p95"] > max(reference["p95"] * 1.5, reference["p95"] + 100):
        return "rollback", "post_promotion_latency_regression", result
    if now >= instant(monitor["expires_at"]):
        return "complete", "post_promotion_observation_window_closed", result
    return "wait", "observing_committed_effects", result
