"""Per-agent recall-quality scopes.

Every agent with natural proactive traffic is evaluated separately in its own
base scope (same tenant/workspace/user as the primary scope, its own agent id)
with the unchanged thresholds. Results are never pooled. The combined verdict
passes only when every evaluated agent passes; an agent whose only blocker is
incomplete evidence makes the combined verdict an evidence wait, never a pass.
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
    totals: dict[str, int] = {}
    dominant: dict[str, tuple[int, str, str]] = {}
    for row in rows:
        # Renamed agents report under their canonical id (reads fan out to the
        # legacy id), so one agent is never split into two gate entries.
        from eimemory.identity import canonical_agent_id

        agent = canonical_agent_id(str(row[0] or "").strip())
        if agent in {"", "default"}:
            continue
        count = int(row[3] or 0)
        totals[agent] = totals.get(agent, 0) + count
        candidate = (count, str(row[1] or ""), str(row[2] or ""))
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


def agent_verdict(report: dict[str, Any]) -> dict[str, Any]:
    gate = report.get("quality_gate") if isinstance(report.get("quality_gate"), dict) else {}
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
    for item in scopes:
        scope = {key: item[key] for key in ("tenant_id", "agent_id", "workspace_id", "user_id")}
        discovered[scope["agent_id"]] = int(item.get("discovered_decisions") or 0)
        same = all(str(primary_scope.get(k) or "") == scope[k] for k in ("agent_id", "workspace_id", "user_id"))
        report = primary_report if same else evaluate(scope)
        verdicts[scope["agent_id"]] = agent_verdict(report if isinstance(report, dict) else {})
    return {"schema": "recall_quality_per_agent.v1", "rule": "all_discovered_agents_must_pass_separately",
            "discovery": {"source": "EIMEMORY_RECALL_GATE_AGENTS" if any(i.get("source") == "override" for i in scopes)
                          else "proactive_decisions natural traffic", "window_days": DISCOVERY_WINDOW_DAYS,
                          "agents": discovered},
            "agents": verdicts, "combined": combine_agent_verdicts(verdicts)}
