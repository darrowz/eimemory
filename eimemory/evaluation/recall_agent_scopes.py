"""Per-agent recall-quality scopes.

G2c: only agents (or topology-bound profiles) with natural proactive traffic
are discovered. After evaluation, only agents with labeled samples
(``sample_count > 0``) are forced through the absolute thresholds. Traffic
agents with ``sample_count == 0`` are deferred (awaiting labeled samples):
they stay in diagnostics and make the combined verdict an evidence wait,
never a pass on the remaining forced agents alone. Declared profiles without
traffic remain in roster/diagnostics and do not block. Results are never
pooled. A forced agent whose only blocker is incomplete evidence also keeps
the combined verdict as an evidence wait.
"""
from __future__ import annotations

from typing import Any, Callable

EVIDENCE_WAIT_REASONS = frozenset({"recall_quality_evidence_incomplete"})


DISCOVERY_WINDOW_DAYS = 14


def recall_agent_scopes(runtime: Any, scope: dict, *, window_days: int = DISCOVERY_WINDOW_DAYS) -> list[dict]:
    """Agents discovered from real traffic, never from a hard-coded list.

    An agent is any ``agent_id`` with natural proactive decisions (deploy
    replays and acceptance-generated decisions excluded) in the last
    ``window_days``. Each gets its own base scope: the bound logical
    workspace/user where that agent's natural traffic lands (most decisions). The requested
    scope's own agent is evaluated only if it was discovered.
    """
    from datetime import datetime, timedelta, timezone
    import os
    from eimemory.evaluation.hermes_profile_coverage import load_topology, natural_profile_scopes

    topology = load_topology()
    if topology is not None:
        # G2c: the protected roster still defines identity, but only profiles
        # with natural traffic are forced. Zero-traffic colleagues stay in
        # diagnostics; they no longer make the gate an evidence wait by
        # themselves. Overrides and discovery cannot invent undeclared agents.
        return natural_profile_scopes(runtime, topology,
            tenant_id=str(scope.get("tenant_id") or "default"), window_days=window_days)

    # Explicit installation override: a comma-separated agent id list.
    override = [item.strip() for item in os.environ.get("EIMEMORY_RECALL_GATE_AGENTS", "").split(",") if item.strip()]

    base = {key: str(scope.get(key) or ("default" if key == "tenant_id" else "")) for key in
            ("tenant_id", "workspace_id", "user_id")}
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    store = getattr(runtime, "store", None)
    run_locked = getattr(store, "run_locked", None)
    rows: list = []
    if run_locked is not None:
        # Each agent is evaluated in the (workspace, user) scope where its real
        # traffic actually lands: the scope with the most natural decisions.
        # Using the installation's base scope for every agent evaluated agents
        # against a store partition they never write to.
        sql = (
            "SELECT agent_id, workspace_id, user_id, COUNT(*) FROM proactive_decisions "
            "WHERE tenant_id=? AND created_at>=? "
            "AND COALESCE(acceptance_generated,0)=0 AND session_id NOT LIKE 'hermes-release-%' "
            "AND session_id NOT LIKE 'cron\\_%' ESCAPE '\\' "
            "GROUP BY agent_id, workspace_id, user_id ORDER BY agent_id")
        params = (base["tenant_id"], since)
        try:
            rows = run_locked(lambda records: records.execute(sql, params).fetchall())
        except Exception:  # noqa: BLE001 - discovery failure yields no scopes, never a pass
            rows = []
    from eimemory.adapters.runtime.channel import base_scope_from_channel, runtime_channel_from_scope

    totals: dict[str, int] = {}
    traffic_scopes: dict[tuple[str, str, str], int] = {}
    for row in rows:
        # Renamed agents report under their canonical id (reads fan out to the
        # legacy id), so one agent is never split into two gate entries.
        from eimemory.identity import canonical_agent_id

        agent = canonical_agent_id(str(row[0] or "").strip())
        if agent in {"", "default"}:
            continue
        count = int(row[3] or 0)
        totals[agent] = totals.get(agent, 0) + count
        traffic_scope = {**base, "agent_id": agent,
                         "workspace_id": str(row[1] or ""), "user_id": str(row[2] or "")}
        channel = runtime_channel_from_scope(traffic_scope)
        workspace = (base_scope_from_channel(channel, traffic_scope)["workspace_id"]
                     if channel else traffic_scope["workspace_id"])
        # Discovery returns a base scope, which collection/review expands once
        # per channel. Count the same logical workspace across channels (and
        # renamed agent ids) before choosing its dominant user partition.
        key = (agent, workspace, traffic_scope["user_id"])
        traffic_scopes[key] = traffic_scopes.get(key, 0) + count
    dominant: dict[str, tuple[int, str, str]] = {}
    for (agent, workspace, user), count in traffic_scopes.items():
        candidate = (count, workspace, user)
        if agent not in dominant or candidate[0] > dominant[agent][0] or (
                candidate[0] == dominant[agent][0] and candidate[1:] < dominant[agent][1:]):
            dominant[agent] = candidate

    def scope_for(agent: str, source: str) -> dict:
        if agent in dominant and dominant[agent][1] and dominant[agent][2]:
            _, workspace, user = dominant[agent]
            return {**base, "agent_id": agent, "workspace_id": workspace, "user_id": user,
                    "discovered_decisions": totals.get(agent, 0), "source": source}
        return {**base, "agent_id": agent, "discovered_decisions": totals.get(agent, 0), "source": source}

    if override:
        return [scope_for(agent, "override") for agent in dict.fromkeys(override)]
    if run_locked is None:
        return []
    return [scope_for(agent, "traffic") for agent in sorted(totals)]


def combine_agent_verdicts(verdicts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """All agents must pass. Evidence waits stay waits; metric failures stay failures."""
    failing = {agent: v for agent, v in verdicts.items() if v.get("ok") is not True}
    if not verdicts:
        # No agent has natural traffic yet: nothing can pass; this is an
        # evidence wait, not an execution failure.
        return {"ok": False, "blocked_reason": "recall_quality_evidence_incomplete",
                "detail": "no_agent_with_natural_traffic", "failing_agents": []}
    if not failing:
        return {"ok": True, "blocked_reason": "", "failing_agents": []}
    hard = {agent: v for agent, v in failing.items()
            if str(v.get("blocked_reason") or "") not in EVIDENCE_WAIT_REASONS}
    if hard:
        agent = sorted(hard)[0]
        reason = str(hard[agent].get("blocked_reason") or "recall_quality_gate_failed")
        return {"ok": False, "blocked_reason": reason, "blocking_agent": agent,
                "failing_agents": sorted(failing)}
    return {"ok": False, "blocked_reason": "recall_quality_evidence_incomplete",
            "failing_agents": sorted(failing)}


def _quality_gate(report: dict[str, Any]) -> dict[str, Any]:
    """Read the authoritative quality gate, not a real-query report's top-level ok.

    Production reports persist the verdict in ``threshold_gate``. An older
    nightly wrapper used ``quality_gate``. A real-query report also has a
    top-level ``ok`` that only means the report was produced, so that field
    must not certify label trust or recall quality.
    """
    for key in ("quality_gate", "threshold_gate"):
        gate = report.get(key)
        if isinstance(gate, dict) and ("ok" in gate or "recall_quality_evidence" in gate):
            return gate
    return {}


def agent_verdict(report: dict[str, Any]) -> dict[str, Any]:
    gate: dict[str, Any] = _quality_gate(report)
    evidence = gate.get("recall_quality_evidence") if isinstance(gate.get("recall_quality_evidence"), dict) else {}
    verdict = {
        "ok": gate.get("ok") is True,
        "blocked_reason": str(gate.get("blocked_reason") or report.get("eval_skipped_reason")
                              or report.get("error") or ("" if gate.get("ok") is True else "recall_quality_unavailable")),
        "sample_count": report.get("sample_count") if type(report.get("sample_count")) is int else 0,
        "evaluation_contract": str(report.get("evaluation_contract") or evidence.get("evaluation_contract") or ""),
        "label_trust_accepted": evidence.get("label_trust_accepted") is True,
    }
    for key in ("false_recall_rate", "recall_at_k", "mrr"):
        value = report.get(key)
        if type(value) in (int, float):
            verdict[key] = value
    fields = report.get("scope_mismatch_fields")
    if (verdict["blocked_reason"] == "dataset_scope_override_mismatch"
            and isinstance(fields, list)):
        verdict["scope_mismatch_fields"] = [
            key for key in ("tenant_id", "agent_id", "workspace_id", "user_id") if key in fields
        ]
    return verdict


def run_per_agent(scopes: list[dict], primary_scope: dict, primary_report: dict[str, Any],
                  evaluate: Callable[[dict], dict[str, Any]]) -> dict[str, Any]:
    verdicts: dict[str, dict[str, Any]] = {}
    discovered: dict[str, int] = {}
    scope_verdicts: dict[str, dict[str, Any]] = {}
    for item in scopes:
        scope = {key: item[key] for key in ("tenant_id", "agent_id", "workspace_id", "user_id")}
        discovered[scope["agent_id"]] = discovered.get(scope["agent_id"], 0) + int(item.get("discovered_decisions") or 0)
        same = all(str(primary_scope.get(k) or "") == scope[k] for k in ("tenant_id", "agent_id", "workspace_id", "user_id"))
        report = primary_report if same else evaluate(scope)
        verdict = agent_verdict(report if isinstance(report, dict) else {})
        agent = scope["agent_id"]
        if item.get("source") == "trusted_hermes_topology":
            key = item["scope_digest"]
            if key in scope_verdicts:
                raise ValueError("recall_profile_scope_duplicate")
            scope_verdicts[key] = {**verdict, "agent_id": agent,
                "profile_name": item["profile_name"], "source_ids": item["source_ids"],
                "natural_decision_count": int(item.get("discovered_decisions") or 0)}
        if agent not in verdicts:
            verdicts[agent] = verdict
        else:
            previous = verdicts[agent]
            verdicts[agent] = {**(verdict if verdict["ok"] is not True else previous),
                "ok": previous["ok"] is True and verdict["ok"] is True,
                "sample_count": previous["sample_count"] + verdict["sample_count"]}
    forced = {agent: verdict for agent, verdict in verdicts.items()
              if int(verdict.get("sample_count") or 0) > 0}
    deferred = {agent: verdict for agent, verdict in verdicts.items()
                if int(verdict.get("sample_count") or 0) <= 0}
    for agent, verdict in verdicts.items():
        verdict["force_status"] = (
            "forced" if agent in forced else "deferred_awaiting_labeled_samples")
    combined = combine_agent_verdicts(forced)
    if deferred:
        hard = (combined.get("ok") is not True
                and str(combined.get("blocked_reason") or "") not in EVIDENCE_WAIT_REASONS)
        if not hard:
            combined = {
                "ok": False,
                "blocked_reason": "recall_quality_evidence_incomplete",
                "detail": "traffic_agents_awaiting_labeled_samples",
                "failing_agents": sorted(set(combined.get("failing_agents") or []) | set(deferred)),
                "deferred_agents": sorted(deferred),
                "forced_agents": sorted(forced),
            }
        else:
            combined = {**combined, "deferred_agents": sorted(deferred),
                        "forced_agents": sorted(forced)}
    else:
        combined = {**combined, "deferred_agents": [], "forced_agents": sorted(forced)}
    result = {"schema": "recall_quality_per_agent.v1", "rule": "all_discovered_agents_must_pass_separately",
            "discovery": {"source": "EIMEMORY_RECALL_GATE_AGENTS" if any(i.get("source") == "override" for i in scopes)
                          else "proactive_decisions natural traffic", "window_days": DISCOVERY_WINDOW_DAYS,
                          "agents": discovered},
            "agents": verdicts, "combined": combined,
            "per_agent_force_policy": "g2c_traffic_agents_with_samples.v1",
            "deferred_agents": sorted(deferred), "forced_agents": sorted(forced)}
    from eimemory.evaluation.hermes_profile_coverage import load_topology, roster_summary
    topology = load_topology()
    if topology is not None or scope_verdicts:
        if topology is None or any(item.get("topology_digest") != topology["topology_digest"]
                for item in scopes if item.get("source") == "trusted_hermes_topology"):
            raise ValueError("recall_profile_topology_disappeared")
        roster = roster_summary(topology)
        result.update(rule="g2c_traffic_agents_must_pass_separately",
            per_agent_force_policy="g2c_traffic_agents_with_samples.v1",
            profile_inventory=roster, scope_verdicts=scope_verdicts)
        result["discovery"]["source"] = "trusted_hermes_topology"
        result['unbound_profiles'] = [dict(profile_name=row['profile_name'], agent_id=row['agent_id'],
            status='business_identity_unbound', business_qualified=False, natural_samples_created=0)
            for row in roster['profiles'] if row['identity_status'] != 'bound']
    return result
