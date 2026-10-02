from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.promotion import promotion_watch
from eimemory.models.records import ScopeRef


SCOPE = ScopeRef(agent_id="checkpoint-test", workspace_id="isolated", user_id="test")
PATTERN_ID = "checkpoint-policy"


@pytest.fixture(autouse=True)
def disable_external_candidate_source(monkeypatch):
    monkeypatch.setenv("EIMEMORY_POSTGRES_VECTOR_ENABLED", "false")


def _seed(runtime, *, status="shadow", observed_count=0):
    watch = promotion_watch._initial_watch(
        candidate_id="", promotion_request_id="", pattern_id=PATTERN_ID,
    )
    watch["status"] = "active" if status == "active" else promotion_watch.WATCH_STATUS
    watch["observed_count"] = observed_count
    watch["hit_count"] = observed_count
    watch["improvement_count"] = observed_count
    watch["observations"] = [
        {"event_id": f"previous-{index}", "hit": True, "improved": True}
        for index in range(observed_count)
    ]
    runtime.upsert_intent_pattern({
        "id": PATTERN_ID,
        "pattern": "isolated checkpoint fixture",
        "status": status,
        "post_promotion_watch": watch,
    }, scope=SCOPE)


def _pattern(runtime):
    row = runtime.store.pattern_row_for_scope(PATTERN_ID, SCOPE)
    assert row is not None
    return json.loads(row["payload_json"])


def _observe(runtime, *, event_id="checkpoint-event", outcome="good"):
    return promotion_watch.record_promotion_observation(
        runtime, pattern_id=PATTERN_ID, scope=SCOPE, event_id=event_id,
        hit=True, improved=outcome == "good", outcome=outcome,
    )


@pytest.mark.parametrize("status", ["shadow", "active"])
@pytest.mark.parametrize("prior_count", [0, 1])
def test_partial_checkpoint_survives_reopen(tmp_path, status, prior_count):
    with Runtime.create(root=tmp_path, profile="core") as runtime:
        _seed(runtime, status=status, observed_count=prior_count)
        report = _observe(runtime)
        assert report["watch"]["observed_count"] == prior_count + 1
        assert report["status"] == (
            "active" if status == "active" else promotion_watch.WATCH_STATUS
        )

    with Runtime.create(root=tmp_path, profile="core") as reopened:
        stored = _pattern(reopened)
        assert stored["status"] == status
        assert stored["post_promotion_watch"] == report["watch"]
        assert stored["post_promotion_watch"]["required_observations"] == 3


@pytest.mark.parametrize("status", ["shadow", "active"])
def test_partial_duplicate_does_not_recount_after_reopen(tmp_path, status):
    with Runtime.create(root=tmp_path, profile="core") as runtime:
        _seed(runtime, status=status)
        _observe(runtime, outcome="bad")

    with Runtime.create(root=tmp_path, profile="core") as reopened:
        duplicate = _observe(reopened, outcome="bad")
        watch = _pattern(reopened)["post_promotion_watch"]
        assert duplicate["watch"] == watch
        assert watch["observed_count"] == 1
        assert watch["hit_count"] == 1
        assert watch["improvement_count"] == 0
        assert watch["regression_count"] == 1
        assert watch["bad_outcome_count"] == 1
        assert watch["failure_count"] == 1
        assert watch["failure_rate"] == 1.0
        assert [item["event_id"] for item in watch["observations"]] == ["checkpoint-event"]

        second = _observe(reopened, event_id="second-event")
        assert second["watch"]["observed_count"] == 2
        assert second["watch"]["failure_rate"] == 0.5

    with Runtime.create(root=tmp_path, profile="core") as reopened:
        assert _pattern(reopened)["post_promotion_watch"] == second["watch"]


@pytest.mark.parametrize("status", ["shadow", "active"])
def test_partial_checkpoint_uses_outer_transaction(tmp_path, monkeypatch, status):
    with Runtime.create(root=tmp_path, profile="core") as runtime:
        _seed(runtime, status=status)
        store = runtime.store
        original = store.update_intent_pattern_row
        write_calls = []
        with closing(sqlite3.connect(f"{store.sqlite.path.as_uri()}?mode=ro", uri=True)) as reader:
            def committed_count():
                row = reader.execute(
                    "SELECT payload_json FROM intent_patterns WHERE id=?", (PATTERN_ID,),
                ).fetchone()
                return json.loads(row[0])["post_promotion_watch"]["observed_count"]

            def write_without_commit(**kwargs):
                assert store.sqlite.in_transaction
                assert kwargs["commit"] is False
                write_calls.append(kwargs["commit"])
                result = original(**kwargs)
                assert store.sqlite.in_transaction
                assert _pattern(runtime)["post_promotion_watch"]["observed_count"] == 1
                assert committed_count() == 0
                return result

            monkeypatch.setattr(store, "update_intent_pattern_row", write_without_commit)
            _observe(runtime)
            assert write_calls == [False]
            assert not store.sqlite.in_transaction
            assert committed_count() == 1


def test_partial_checkpoint_and_ledger_roll_back_together(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path, profile="core") as runtime:
        _seed(runtime)
        before = _pattern(runtime)
        ledger_before = runtime.get_policy_rollout_ledger(scope=SCOPE)
        original = promotion_watch._record_watch_ledger
        observed = {}

        def fail_after_ledger_write(*args, **kwargs):
            observed["in_transaction"] = runtime.store.sqlite.in_transaction
            observed["count"] = _pattern(runtime)["post_promotion_watch"]["observed_count"]
            original(*args, **kwargs)
            raise RuntimeError("injected checkpoint ledger failure")

        with monkeypatch.context() as patch:
            patch.setattr(promotion_watch, "_record_watch_ledger", fail_after_ledger_write)
            with pytest.raises(RuntimeError, match="injected checkpoint ledger failure"):
                _observe(runtime)
        assert observed == {"in_transaction": True, "count": 1}
        assert not runtime.store.sqlite.in_transaction
        assert _pattern(runtime) == before
        assert runtime.get_policy_rollout_ledger(scope=SCOPE) == ledger_before
        retry = _observe(runtime)
        assert _pattern(runtime)["post_promotion_watch"] == retry["watch"]
        assert retry["watch"]["observed_count"] == 1


@pytest.mark.parametrize("status", ["shadow", "active"])
def test_partial_checkpoint_write_conflict_rolls_back(tmp_path, monkeypatch, status):
    with Runtime.create(root=tmp_path, profile="core") as runtime:
        _seed(runtime, status=status)
        before = _pattern(runtime)
        ledger_before = runtime.get_policy_rollout_ledger(scope=SCOPE)

        def conflicting_write(**kwargs):
            assert runtime.store.sqlite.in_transaction
            assert kwargs["commit"] is False
            return 0

        with monkeypatch.context() as patch:
            patch.setattr(runtime.store, "update_intent_pattern_row", conflicting_write)
            with pytest.raises(RuntimeError, match="policy_state_conflict"):
                _observe(runtime)
        assert not runtime.store.sqlite.in_transaction
        assert _pattern(runtime) == before
        assert runtime.get_policy_rollout_ledger(scope=SCOPE) == ledger_before
        retry = _observe(runtime)
        assert _pattern(runtime)["post_promotion_watch"] == retry["watch"]


def test_observation_does_not_take_over_existing_transaction(tmp_path):
    with Runtime.create(root=tmp_path, profile="core") as runtime:
        _seed(runtime)
        before = _pattern(runtime)
        with runtime.store.locked() as sqlite:
            sqlite.execute("BEGIN IMMEDIATE")
            try:
                with pytest.raises(RuntimeError, match="record_mutation_requires_own_transaction"):
                    _observe(runtime)
                assert sqlite.in_transaction
                assert _pattern(runtime) == before
            finally:
                sqlite.rollback()


@pytest.mark.parametrize("status", ["shadow", "active"])
def test_partial_checkpoint_write_exception_rolls_back(tmp_path, monkeypatch, status):
    with Runtime.create(root=tmp_path, profile="core") as runtime:
        _seed(runtime, status=status)
        before = _pattern(runtime)
        ledger_before = runtime.get_policy_rollout_ledger(scope=SCOPE)
        original = runtime.store.update_intent_pattern_row

        def fail_after_pattern_write(**kwargs):
            assert kwargs["commit"] is False
            assert original(**kwargs) == 1
            assert runtime.store.sqlite.in_transaction
            assert _pattern(runtime)["post_promotion_watch"]["observed_count"] == 1
            raise sqlite3.OperationalError("injected checkpoint write failure")

        with monkeypatch.context() as patch:
            patch.setattr(runtime.store, "update_intent_pattern_row", fail_after_pattern_write)
            with pytest.raises(sqlite3.OperationalError, match="injected checkpoint write failure"):
                _observe(runtime)
        assert not runtime.store.sqlite.in_transaction
        assert _pattern(runtime) == before
        assert runtime.get_policy_rollout_ledger(scope=SCOPE) == ledger_before

    with Runtime.create(root=tmp_path, profile="core") as reopened:
        assert _pattern(reopened) == before
        retry = _observe(reopened)
        assert retry["watch"]["observed_count"] == 1
        assert _pattern(reopened)["post_promotion_watch"] == retry["watch"]
