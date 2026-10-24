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
    assert all(s["workspace_id"] == "lab" and s["user_id"] == "u" and s["tenant_id"] == "acme" for s in scopes)
    assert [s["discovered_decisions"] for s in scopes] == [2, 1]


def test_discovered_base_scope_collects_and_reviews_actual_channel(tmp_path, monkeypatch):
    from eimemory.adapters.runtime.channel import resolve_channel_scope
    from eimemory.evaluation.production_query_auto_review import auto_review_pending_production_queries
    from eimemory.models.records import RecordEnvelope, ScopeRef

    monkeypatch.delenv("EIMEMORY_RECALL_GATE_AGENTS", raising=False)
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", "fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz")
    with Runtime.create(root=tmp_path) as runtime:
        conn = runtime.store.sqlite.conn
        _decision(conn, "alpha")
        scope = recall_agent_scopes(runtime, BASE)[0]
        exact = ScopeRef.from_dict(resolve_channel_scope("hermes", scope))
        # A real-shaped pending observation already in the traffic partition
        # must be reviewed there, without creating a nested channel workspace.
        pending = RecordEnvelope.create(kind="evaluation_packet", title="Pending production query",
            content={"case_id": "real-case", "channel": "hermes"}, scope=exact,
            source="eimemory.production_recall.pending_case", source_id="hermes",
            meta={"report_type": "production_recall_pending_case"})
        runtime.store.append(pending)
        report = auto_review_pending_production_queries(runtime, scope=scope, dry_run=True)
        assert report["by_channel"]["hermes"]["scanned"] == 1
        assert report["by_channel"]["openclaw"]["scanned"] == 0
        assert report["review_results"][0]["reasons"] == ["pending_capture_boundary_invalid"]
        assert report["accepted_count"] == 0
        assert {item.scope.workspace_id for item in runtime.store.list_records_by_meta_value(
            kinds=["evaluation_packet"], scope=exact, meta_key="report_type",
            meta_value="production_recall_pending_case", status="active", limit=10)} == {"lab::channel::hermes"}


def test_channel_counts_choose_same_logical_workspace_before_dominance(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_RECALL_GATE_AGENTS", raising=False)
    with Runtime.create(root=tmp_path) as runtime:
        conn = runtime.store.sqlite.conn
        _decision(conn, "alpha", session="hermes", n=2)
        _decision(conn, "alpha", session="codex", n=2)
        _decision(conn, "alpha", session="other-user", n=3)
        conn.execute("UPDATE proactive_decisions SET workspace_id='lab::channel::codex' WHERE session_id='codex'")
        conn.execute("UPDATE proactive_decisions SET user_id='other' WHERE session_id='other-user'")
        conn.commit()
        discovered = recall_agent_scopes(runtime, BASE)
    assert discovered == [{"tenant_id": "acme", "workspace_id": "lab", "user_id": "u",
                           "agent_id": "alpha", "discovered_decisions": 7, "source": "traffic"}]


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
    assert result["combined"]["ok"] is False
    assert result["combined"]["blocked_reason"] == "false_recall_rate_above_threshold"
    assert result["combined"]["blocking_agent"] == "beta"
    assert result["combined"]["failing_agents"] == ["beta"]
    assert result["forced_agents"] == ["alpha", "beta"]
    assert result["deferred_agents"] == []


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

def test_zero_sample_traffic_agents_are_deferred_not_forced():
    scopes = [{**BASE, "agent_id": a, "discovered_decisions": n}
              for a, n in (("hongrui", 10), ("hongtai", 5), ("hongxin", 3))]
    reports = {
        "hongrui": _report(True, n=3),
        "hongtai": _report(False, "recall_quality_evidence_incomplete", n=0),
        "hongxin": _report(False, "recall_quality_evidence_incomplete", n=0),
    }
    result = run_per_agent(scopes, {**BASE, "agent_id": "hongrui"}, reports["hongrui"],
                           lambda s: reports[s["agent_id"]])
    assert result["forced_agents"] == ["hongrui"]
    assert result["deferred_agents"] == ["hongtai", "hongxin"]
    assert result["agents"]["hongrui"]["force_status"] == "forced"
    assert result["agents"]["hongtai"]["force_status"] == "deferred_awaiting_labeled_samples"
    assert result["combined"]["ok"] is False
    assert result["combined"]["blocked_reason"] == "recall_quality_evidence_incomplete"
    assert result["combined"]["detail"] == "traffic_agents_awaiting_labeled_samples"
    # Never pass on the labeled agent alone while traffic peers await samples.
    assert "hongrui" not in result["combined"].get("failing_agents", [])
    assert set(result["combined"]["deferred_agents"]) == {"hongtai", "hongxin"}


def test_all_forced_agents_passing_with_no_deferred_still_passes():
    scopes = [{**BASE, "agent_id": "solo", "discovered_decisions": 4}]
    primary = _report(True, n=2)
    result = run_per_agent(scopes, {**BASE, "agent_id": "solo"}, primary,
                           lambda s: primary)
    assert result["combined"]["ok"] is True
    assert result["forced_agents"] == ["solo"]
    assert result["deferred_agents"] == []


def test_hard_fail_on_forced_agent_beats_deferred_evidence_wait():
    scopes = [{**BASE, "agent_id": a} for a in ("alpha", "beta")]
    reports = {
        "alpha": _report(False, "false_recall_rate_above_threshold", n=4),
        "beta": _report(False, "recall_quality_evidence_incomplete", n=0),
    }
    result = run_per_agent(scopes, BASE, {}, lambda s: reports[s["agent_id"]])
    assert result["combined"]["ok"] is False
    assert result["combined"]["blocked_reason"] == "false_recall_rate_above_threshold"
    assert result["combined"]["blocking_agent"] == "alpha"
    assert result["deferred_agents"] == ["beta"]

