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
    tenant/workspace/user of ``scope`` with that agent id. The requested
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
    if override:
        return [{**base, "agent_id": agent, "discovered_decisions": 0, "source": "override"}
                for agent in dict.fromkeys(override)]
    if run_locked is None:
        return []
    sql = (
                "SELECT agent_id, COUNT(*) FROM proactive_decisions WHERE tenant_id=? AND created_at>=? "
                "AND COALESCE(acceptance_generated,0)=0 AND session_id NOT LIKE 'hermes-release-%' "
                "AND session_id NOT LIKE 'cron\\_%' ESCAPE '\\' GROUP BY agent_id ORDER BY agent_id")
    params = (base["tenant_id"], since)
    try:
        rows = run_locked(lambda records: records.execute(sql, params).fetchall())
    except Exception:  # noqa: BLE001 - discovery failure yields no scopes, never a pass
        return []
    return [{**base, "agent_id": str(row[0]), "discovered_decisions": int(row[1])}
            for row in rows if str(row[0] or "").strip() not in {"", "default"}]


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
