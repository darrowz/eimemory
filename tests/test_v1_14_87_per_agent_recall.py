"""Per-agent recall gate with runtime discovery and generic agent names."""
from datetime import datetime, timezone

from eimemory.api.runtime import Runtime
from eimemory.evaluation.recall_agent_scopes import (
    combine_agent_verdicts, recall_agent_scopes, run_per_agent,
)
from eimemory.scheduler.jobs import _per_agent_bounded_gate

BASE = {"tenant_id": "acme", "agent_id": "primary-x", "workspace_id": "lab", "user_id": "operator-1"}


def _decision(conn, agent, session="s-1", acceptance=0, n=1):
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(proactive_decisions)")}
    now = datetime.now(timezone.utc).isoformat()
    for i in range(n):
        values = {}
        for name, row in cols.items():
            notnull, default = row[3], row[4]
            if notnull and default is None:
                values[name] = 0 if "INT" in str(row[2]).upper() or "REAL" in str(row[2]).upper() else ""
        values.update(decision_id=f"{agent}-{session}-{i}", tenant_id="acme", agent_id=agent,
                      workspace_id="lab::channel::hermes", user_id="u", session_id=session,
                      created_at=now, updated_at=now)
        if "acceptance_generated" in cols:
            values["acceptance_generated"] = acceptance
        names = ",".join(values)
        conn.execute(f"INSERT INTO proactive_decisions ({names}) VALUES ({','.join('?' * len(values))})",
                     list(values.values()))
    conn.commit()


def test_discovers_differently_named_agents_from_real_traffic_only(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_RECALL_GATE_AGENTS", raising=False)
    with Runtime.create(root=tmp_path) as runtime:
        assert recall_agent_scopes(runtime, BASE) == []
        conn = runtime.store.sqlite.conn
        _decision(conn, "alpha", n=2)
        _decision(conn, "beta")
        _decision(conn, "gamma", session="hermes-release-abc")  # deploy replay
        _decision(conn, "delta", acceptance=1)                  # acceptance generated
        _decision(conn, "epsilon", session="cron_nightly")      # scheduled
        scopes = recall_agent_scopes(runtime, BASE)
    assert [s["agent_id"] for s in scopes] == ["alpha", "beta"]
    # Each agent is evaluated where its real traffic lands, not the base scope.
    assert all(s["workspace_id"] == "lab::channel::hermes" and s["user_id"] == "u" and s["tenant_id"] == "acme" for s in scopes)
    assert [s["discovered_decisions"] for s in scopes] == [2, 1]


def test_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setenv("EIMEMORY_RECALL_GATE_AGENTS", "zeta, eta,zeta")
    with Runtime.create(root=tmp_path) as runtime:
        assert [s["agent_id"] for s in recall_agent_scopes(runtime, BASE)] == ["zeta", "eta"]


def _report(ok, reason="", n=10):
    return {"sample_count": n, "quality_gate": {"ok": ok, "blocked_reason": reason}}


def test_each_agent_is_judged_separately_and_all_must_pass():
    scopes = [{**BASE, "agent_id": a} for a in ("alpha", "beta")]
    reports = {"alpha": _report(True), "beta": _report(False, "false_recall_rate_above_threshold")}
    result = run_per_agent(scopes, BASE, {}, lambda s: reports[s["agent_id"]])
    assert result["agents"]["alpha"]["ok"] is True and result["agents"]["beta"]["ok"] is False
    assert result["combined"] == {"ok": False, "blocked_reason": "false_recall_rate_above_threshold",
                                  "blocking_agent": "beta", "failing_agents": ["beta"]}


def test_evidence_waits_stay_waits_and_zero_agents_never_pass():
    waits = combine_agent_verdicts({"a": {"ok": True}, "b": {"ok": False, "blocked_reason": "recall_quality_evidence_incomplete"}})
    assert waits["ok"] is False and waits["blocked_reason"] == "recall_quality_evidence_incomplete"
    none = combine_agent_verdicts({})
    assert none["ok"] is False and none["blocked_reason"] == "recall_quality_evidence_incomplete"
    single = run_per_agent([{**BASE, "agent_id": "solo"}], BASE, {}, lambda s: _report(True))
    assert single["combined"]["ok"] is True


def test_bounded_gate_is_never_weaker_than_primary():
    primary = {"ok": True, "blocked_reason": ""}
    passing = {"combined": {"ok": True}}
    failing = {"combined": {"ok": False, "blocked_reason": "x", "failing_agents": ["beta"]}}
    assert _per_agent_bounded_gate(primary, passing) is primary
    assert _per_agent_bounded_gate(primary, failing)["ok"] is False
    assert _per_agent_bounded_gate({"ok": False, "blocked_reason": "p"}, passing)["ok"] is False
    assert _per_agent_bounded_gate(primary, {"ok": False})["ok"] is False
