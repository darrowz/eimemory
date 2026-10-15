"""1.14.87: deduped correction chains, outcome coverage stats, reversible synthetic rule retirement."""
import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.ops import natural_outcomes

SCOPE = {"tenant_id": "default", "agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
CORRECTION = {"text": "you stopped at inability", "context": "ctx", "target_capability": "proactive.judgment",
              "expected_behavior": "Create a concrete plan."}


def test_identical_correction_is_one_chain(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        first = runtime.record_user_correction_replay(dict(CORRECTION), scope=SCOPE, persist=True)
        second = runtime.record_user_correction_replay(dict(CORRECTION), scope=SCOPE, persist=True)
        assert first["ok"] is True and second["ok"] is True
        assert second["persistence"]["reused"] is True
        assert second["ground_truth_rule_id"] == first["ground_truth_rule_id"]
        rules = [r for r in runtime.store.list_records(kinds=["rule"], scope=SCOPE, limit=50)
                 if r.source == "eimemory.correction_replay"]
        assert len(rules) == 1
        gate = runtime.build_ground_truth_pre_answer_gate(query="capability is missing", scope=SCOPE, persist=False)
        assert isinstance(gate, dict)


def _duplicate_rules(runtime, n):
    ids = []
    for i in range(n):
        rule = RecordEnvelope.create(kind="rule", title="Ground truth behavior: proactive.judgment",
            summary="Create a concrete plan.", detail="lesson", scope=ScopeRef.from_dict(SCOPE),
            source="eimemory.correction_replay", status="active", content={"priority": "T0"})
        rule.time.created_at = f"2026-10-0{i + 1}T00:00:00Z"
        runtime.store.append(rule)
        ids.append(rule.record_id)
    return ids


def test_retirement_preview_apply_and_revert_are_reversible(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        ids = _duplicate_rules(runtime, 4)
        plan = natural_outcomes.retirement_plan(runtime.store)
        assert plan["retire_count"] == 3 and plan["groups"][0]["identical_active"] == 4
        newest = plan["groups"][0]["keep_record_id"]
        mismatch = natural_outcomes.apply_retirement(runtime.store, tmp_path, expect_count=5)
        assert mismatch["ok"] is False and mismatch["applied"] == 0
        applied = natural_outcomes.apply_retirement(runtime.store, tmp_path, expect_count=3)
        assert applied["applied"] == 3
        scope = ScopeRef.from_dict(SCOPE)
        statuses = {i: runtime.store.get_by_id(i, scope=scope).status for i in ids}
        assert statuses[newest] == "active"
        assert sorted(v for k, v in statuses.items() if k != newest) == ["deprecated"] * 3
        assert natural_outcomes.retirement_plan(runtime.store)["retire_count"] == 0
        reverted = natural_outcomes.revert_retirement(runtime.store, tmp_path, applied["batch_id"])
        assert reverted["reverted"] == 3
        assert {runtime.store.get_by_id(i, scope=scope).status for i in ids} == {"active"}


def test_outcome_stats_counts_signals_without_treating_them_as_verified(tmp_path, capsys, monkeypatch):
    import sqlite3
    with Runtime.create(root=tmp_path):
        pass
    db = tmp_path / "state" / "eimemory.sqlite"
    report = natural_outcomes.outcome_stats(db)
    assert report["ok"] is True and report["natural"]["decisions"] == 0
    assert report["raw_text_check"]["ok"] is True
    conn = sqlite3.connect(db)
    assert "proactive_effect_signals" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    assert main(["outcomes", "stats"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["report_type"] == "natural_outcome_stats"
