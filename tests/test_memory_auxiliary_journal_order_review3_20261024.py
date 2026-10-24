"""Auxiliary snapshots must retain commit order after a projection outage."""
from contextlib import closing
import json

import pytest

from eimemory.storage.runtime_store import RuntimeStore


SCOPE = {"tenant_id": "review3", "agent_id": "peer", "workspace_id": "journal", "user_id": "owner"}


def _payload(store, table, identifier):
    with store.locked() as sqlite:
        row = sqlite.execute(f"SELECT payload_json FROM {table} WHERE id=?", (identifier,)).fetchone()
        return json.loads(str(row[0]))


def _outage(*args, **kwargs):
    raise OSError("synthetic auxiliary journal projection outage")


@pytest.mark.parametrize("stream", ["intent_patterns", "event_outcomes"])
def test_latest_auxiliary_snapshot_survives_outage_backlog_and_rebuild(tmp_path, monkeypatch, stream):
    with closing(RuntimeStore(tmp_path)) as store:
        if stream == "event_outcomes":
            store.record_event({"id": "parent-event", "user_phrase": "review3 queue recovery", "event_type": "repair"}, scope=SCOPE)
        with monkeypatch.context() as fault:
            fault.setattr(store, "_flush_committed_exports", _outage)
            if stream == "intent_patterns":
                store.upsert_intent_pattern({"id": "auxiliary-state", "pattern": "review3 queue recovery", "default_event_type": "repair", "execution_policy": ["verify before reporting"]}, scope=SCOPE)
            else:
                store.record_outcome("parent-event", {"id": "auxiliary-state", "outcome": "good", "recorded_at": "2026-10-24T00:00:00+00:00"}, scope=SCOPE)
            for index in range(101):
                store.record_event({"id": f"unrelated-event-{index}", "user_phrase": "independent unrelated job", "event_type": "review"}, scope=SCOPE)
        if stream == "intent_patterns":
            result = store.rollback_intent_pattern("auxiliary-state", scope=SCOPE, reason="verified withdrawal after outage")
            assert result["ok"], result
            key, expected = "status", "rolled_back"
        else:
            store.record_outcome("parent-event", {"id": "auxiliary-state", "outcome": "bad", "recorded_at": "2026-10-24T00:00:01+00:00"}, scope=SCOPE)
            key, expected = "outcome", "bad"
        assert _payload(store, stream, "auxiliary-state")[key] == expected
        assert store.flush_exports()["remaining"] == 0
        report = store.rebuild_sqlite_from_jsonl(replace=True)
        assert report["ok"], report
        assert _payload(store, stream, "auxiliary-state")[key] == expected


@pytest.mark.parametrize("withdraw_after_daily_budget", [False, True])
def test_rebuild_preserves_auto_policy_snapshot_and_promotion_ledger(tmp_path, withdraw_after_daily_budget):
    with closing(RuntimeStore(tmp_path)) as store:
        identifiers = []
        for index in range(3 if withdraw_after_daily_budget else 1):
            identifier = f"auto-policy-{index}"
            identifiers.append(identifier)
            result = store.upsert_intent_pattern({
                "id": identifier,
                "pattern": f"review3 automatic recovery {index}",
                "default_event_type": "repair",
                "source_opportunity_id": f"auto-opportunity-{index}",
                "source_opportunity": {"opportunity_id": f"auto-opportunity-{index}", "opportunity_type": "intent_policy"},
            }, scope=SCOPE)
            assert result["_promotion_budget_decision"] == "ok", result
        if withdraw_after_daily_budget:
            assert store.rollback_intent_pattern("auto-policy-0", scope=SCOPE, reason="manual verified withdrawal")["ok"]
        before_patterns = [_payload(store, "intent_patterns", identifier) for identifier in identifiers]
        before_ledger = store.get_policy_rollout_ledger(scope=SCOPE, limit=20)
        assert len(before_ledger) == len(identifiers) + int(withdraw_after_daily_budget)
        report = store.rebuild_sqlite_from_jsonl(replace=True)
        assert report["ok"], report
        assert [_payload(store, "intent_patterns", identifier) for identifier in identifiers] == before_patterns
        assert store.get_policy_rollout_ledger(scope=SCOPE, limit=20) == before_ledger
