"""Exact-namespace, decision-level observations shared by reports and trials."""
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from eimemory.adapters.runtime.channel import normalize_runtime_channel, resolve_channel_scope
from eimemory.models.records import ScopeRef
from eimemory.models.source_partitions import normalize_source_ids


def namespace(channel, scope, source_ids):
    channel = normalize_runtime_channel(channel)
    scope = resolve_channel_scope(channel, scope)
    base = ScopeRef.from_dict(scope)
    if not all((base.agent_id, base.workspace_id, base.user_id)):
        raise ValueError("effect_exact_owner_required")
    sources = normalize_source_ids(source_ids)
    if not sources:
        raise ValueError("effect_sources_required")
    return channel, asdict(base), list(sources)


def instant(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("effect_timestamp_timezone_required")
    return dt.astimezone(timezone.utc)


def day_window(day=None, tz="Asia/Shanghai"):
    zone = ZoneInfo(tz)
    if not day:
        day = (datetime.now(zone).date() - timedelta(days=1)).isoformat()
    start = datetime.fromisoformat(day).replace(tzinfo=zone)
    if start.date().isoformat() != day:
        raise ValueError("effect_date_invalid")
    return day, start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def load_observations(store, *, channel, scope, source_ids, start=None, end=None, limit=100000):
    channel, scope, sources = namespace(channel, scope, source_ids)
    if not 1 <= limit <= 100000:
        raise ValueError("effect_scan_limit_invalid")
    decisions, signals, unanchored = {}, [], 0
    with store._lock:
        conn = store.sqlite.conn
        args = (channel, scope["tenant_id"], scope["agent_id"], scope["workspace_id"], scope["user_id"])
        rows = conn.execute("SELECT decision_id FROM proactive_decisions WHERE channel=? AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? LIMIT ?", (*args, limit + 1)).fetchall()
        if len(rows) > limit:
            raise ValueError("effect_decision_scan_incomplete")
        for row in rows:
            d = store.sqlite.load_proactive_decision(row[0])
            if sorted(d["source_ids"]) == sorted(sources) and d.get("acceptance_generated") is not True:
                decisions[d["decision_id"]] = d
        rows = conn.execute("SELECT signal_id,payload_json,created_at FROM proactive_effect_signals WHERE json_extract(payload_json,'$.channel')=? AND json_extract(payload_json,'$.scope.tenant_id')=? AND json_extract(payload_json,'$.scope.agent_id')=? AND json_extract(payload_json,'$.scope.workspace_id')=? AND json_extract(payload_json,'$.scope.user_id')=? LIMIT ?", (*args, limit + 1)).fetchall()
        if len(rows) > limit:
            raise ValueError("effect_signal_scan_incomplete")
        for row in rows:
            p = json.loads(row[1])
            if sorted(p.get("source_ids", [])) != sorted(sources):
                continue
            d = decisions.get(p["decision_id"])
            anchor = p.get("decision_created_at") or (d or {}).get("created_at")
            if not anchor:
                unanchored += 1
                continue
            if d is None:
                d = {"decision_id": p["decision_id"], "created_at": anchor,
                     "release_identity": p["release_identity"], "policy_version": p["policy_version"],
                     "control_cohort": p["control_cohort"], "pair_id": p.get("pair_id", ""),
                     "retrieval_diagnostics": {}, "items": []}
                decisions[d["decision_id"]] = d
            signals.append({**p, "signal_id": row[0], "observed_at": row[2]})
    selected = {key: d for key, d in decisions.items()
                if (start is None or instant(d["created_at"]) >= start)
                and (end is None or instant(d["created_at"]) < end)}
    by_decision = defaultdict(list)
    for signal in signals:
        by_decision[signal["decision_id"]].append(signal)
    for d in selected.values():
        d["signals"] = by_decision[d["decision_id"]]
        values = {}
        for s in d["signals"]:
            for key, value in s["labels"].items():
                values.setdefault(key, set()).add(value)
        d["labels"] = {k: next(iter(v)) if len(v) == 1 else "conflicting" for k, v in values.items()}
        d["signal_ids"] = sorted(s["signal_id"] for s in d["signals"])
    return {"channel": channel, "scope": scope, "source_ids": sources,
            "decisions": sorted(selected.values(), key=lambda d: (d["created_at"], d["decision_id"])),
            "unanchored_signals": unanchored, "population": "retained_decisions_and_anchored_observations"}
