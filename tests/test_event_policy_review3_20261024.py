"""Public event/policy ownership and durable rollback regressions."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.promotion_watch import initialize_promotion_watch
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = {"tenant_id": "review3", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"}


def _snapshot(runtime):
    with runtime.store.locked():
        return {
            table: [tuple(row) for row in runtime.store.sqlite.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
            for table in ("events", "event_outcomes", "intent_patterns", "policy_rollout_ledger", "export_outbox", "records")
        }


def _in_transaction(runtime):
    with runtime.store.locked():
        return runtime.store.sqlite.in_transaction


def _pattern_payload(runtime):
    with runtime.store.locked():
        return json.loads(runtime.store.sqlite.execute("SELECT payload_json FROM intent_patterns WHERE id='review3-policy'").fetchone()[0])


def _event(runtime, identifier, scope=SCOPE):
    return runtime.record_event({"id": identifier, "user_phrase": "review3 queue recovery", "event_type": "repair"}, scope=scope)


def _pattern(runtime, identifier="review3-policy", scope=SCOPE, **changes):
    return runtime.upsert_intent_pattern({"id": identifier, "pattern": "review3 queue recovery", "default_event_type": "repair", "execution_policy": ["verify before reporting"], **changes}, scope=scope)


@pytest.mark.parametrize("field", tuple(SCOPE))
def test_outcome_id_cannot_overwrite_another_owner(tmp_path, field):
    other = {**SCOPE, field: "other"}
    with Runtime.create(root=tmp_path) as runtime:
        _event(runtime, "owner-event")
        _event(runtime, "other-event", other)
        runtime.record_outcome("owner-event", {"id": "shared-outcome", "outcome": "good"}, scope=SCOPE)
        before = _snapshot(runtime)
        with pytest.raises(ValueError, match="outcome_scope_conflict"):
            runtime.record_outcome("other-event", {"id": "shared-outcome", "outcome": "bad"}, scope=other)
        assert _snapshot(runtime) == before
        assert not _in_transaction(runtime)


def test_outcome_identity_cannot_be_rebound_to_another_event(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        _event(runtime, "first-event")
        _event(runtime, "second-event")
        runtime.record_outcome("first-event", {"id": "stable-outcome", "outcome": "good"}, scope=SCOPE)
        before = _snapshot(runtime)
        with pytest.raises(ValueError, match="outcome_event_conflict"):
            runtime.record_outcome("second-event", {"id": "stable-outcome", "outcome": "bad"}, scope=SCOPE)
        assert _snapshot(runtime) == before
        assert not _in_transaction(runtime)


@pytest.mark.parametrize("field", tuple(SCOPE))
def test_pattern_id_cannot_overwrite_another_owner(tmp_path, field):
    with Runtime.create(root=tmp_path) as runtime:
        _pattern(runtime)
        before = _snapshot(runtime)
        with pytest.raises(ValueError, match="pattern_scope_conflict"):
            _pattern(runtime, scope={**SCOPE, field: "other"})
        assert _snapshot(runtime) == before
        assert not _in_transaction(runtime)


@pytest.mark.parametrize("field", ("agent_id", "workspace_id", "user_id"))
def test_inherited_pattern_is_readable_but_cannot_be_rolled_back_by_child(tmp_path, field):
    shared = {**SCOPE, field: ""}
    with Runtime.create(root=tmp_path) as runtime:
        _pattern(runtime, scope=shared)
        suggestions = runtime.search_policy("review3 queue recovery", scope=SCOPE)["policy_suggestions"]
        assert any(item.get("pattern_id") == "review3-policy" or item.get("id") == "review3-policy" for item in suggestions)
        before = _snapshot(runtime)
        result = runtime.rollback_intent_pattern("review3-policy", scope=SCOPE, reason="child rollback")
        assert result["ok"] is False
        assert result["error"] == "pattern_not_found"
        assert _snapshot(runtime) == before


def test_manual_policy_rollback_survives_log_rebuild(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        _pattern(runtime)
        result = runtime.rollback_intent_pattern("review3-policy", scope=SCOPE, reason="verified rollback")
        assert result["ok"] is True
        before = _pattern_payload(runtime)
        assert before["status"] == "rolled_back"
        report = runtime.store.rebuild_sqlite_from_jsonl(replace=True)
        assert report["ok"] is True, report
        assert _pattern_payload(runtime) == before
        assert all(item.get("id") != "review3-policy" for item in runtime.search_policy("review3 queue recovery", scope=SCOPE)["policy_suggestions"])


@pytest.mark.parametrize("field", tuple(SCOPE))
def test_outcome_cannot_attach_to_an_existing_foreign_event(tmp_path, field):
    with Runtime.create(root=tmp_path) as runtime:
        _event(runtime, "owner-event")
        before = _snapshot(runtime)
        with pytest.raises(ValueError, match="event_scope_conflict"):
            runtime.record_outcome("owner-event", {"outcome": "good"}, scope={**SCOPE, field: "other"})
        assert _snapshot(runtime) == before
        assert not _in_transaction(runtime)


@pytest.mark.parametrize("kind", ("pattern", "outcome"))
def test_same_owner_mutable_state_roundtrip_survives_rebuild(tmp_path, kind):
    with Runtime.create(root=tmp_path) as runtime:
        _event(runtime, "mutable-event")
        for state in ("first", "middle", "first"):
            if kind == "pattern":
                _pattern(runtime, execution_policy=[state])
            else:
                runtime.record_outcome("mutable-event", {"id": "mutable-outcome", "outcome": state,
                    "recorded_at": "2026-10-24T01:00:00+00:00"}, scope=SCOPE)
        table = "intent_patterns" if kind == "pattern" else "event_outcomes"
        with runtime.store.locked():
            before = [json.loads(row[0]) for row in runtime.store.sqlite.execute(f"SELECT payload_json FROM {table} ORDER BY id").fetchall()]
        report = runtime.store.rebuild_sqlite_from_jsonl(replace=True)
        assert report["ok"] is True, report
        with runtime.store.locked():
            after = [json.loads(row[0]) for row in runtime.store.sqlite.execute(f"SELECT payload_json FROM {table} ORDER BY id").fetchall()]
        assert after == before


def test_promotion_watch_state_survives_rebuild(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        _pattern(runtime)
        candidate = runtime.store.append(RecordEnvelope.create(kind="capability_candidate", title="Review3 watch",
            scope=ScopeRef.from_dict(SCOPE), source="review3.fixture"))
        report = initialize_promotion_watch(runtime, candidate=candidate, scope=SCOPE,
            promotion_request_id="", applied_pattern_ids=["review3-policy"])
        assert report["ok"] is True, report
        before = _pattern_payload(runtime)
        assert before["status"] == "shadow"
        assert before["post_promotion_watch"]["status"] == "shadow_observe"
        rebuilt = runtime.store.rebuild_sqlite_from_jsonl(replace=True)
        assert rebuilt["ok"] is True, rebuilt
        assert _pattern_payload(runtime) == before


def _prepare_operation(runtime, kind):
    if kind == "outcome":
        _event(runtime, "fault-event")
    if kind == "rollback":
        _pattern(runtime)


def _operation(runtime, kind):
    if kind == "event":
        return _event(runtime, "fault-event")
    if kind == "outcome":
        return runtime.record_outcome("fault-event", {"id": "fault-outcome", "outcome": "good"}, scope=SCOPE)
    if kind == "rollback":
        return runtime.rollback_intent_pattern("review3-policy", scope=SCOPE, reason="fault recovery")
    return _pattern(runtime)


def _stream(kind):
    return {"event": "events", "outcome": "event_outcomes", "pattern": "intent_patterns", "rollback": "intent_patterns"}[kind]


@pytest.mark.parametrize("kind", ("event", "outcome", "pattern", "rollback"))
def test_committed_auxiliary_write_survives_log_failure_and_recovers(tmp_path, monkeypatch, kind):
    with Runtime.create(root=tmp_path) as runtime:
        _prepare_operation(runtime, kind)
        log = runtime.store._auxiliary_log(_stream(kind))
        append = log.append_payload
        def fail(*args, **kwargs):
            raise OSError("injected projection failure")
        monkeypatch.setattr(log, "append_payload", fail)
        result = _operation(runtime, kind)
        assert result.get("id") or result.get("ok") is True
        if kind == "outcome":
            assert "closed_loop" in result
        assert not _in_transaction(runtime)
        with runtime.store.locked():
            pending = runtime.store.sqlite.execute("SELECT COUNT(*) FROM export_outbox WHERE stream=? AND state='pending'", (_stream(kind),)).fetchone()[0]
        assert pending >= 1
        monkeypatch.setattr(log, "append_payload", append)
        assert runtime.store.flush_exports()["remaining"] == 0
        rebuilt = runtime.store.rebuild_sqlite_from_jsonl(replace=True)
        assert rebuilt["ok"] is True, rebuilt
        if kind == "rollback":
            assert _pattern_payload(runtime)["status"] == "rolled_back"


class _InterruptedWrite(BaseException):
    pass


@pytest.mark.parametrize("kind", ("event", "outcome", "pattern", "rollback"))
def test_precommit_interrupt_rolls_back_auxiliary_state_and_outbox(tmp_path, monkeypatch, kind):
    with Runtime.create(root=tmp_path) as runtime:
        _prepare_operation(runtime, kind)
        before = _snapshot(runtime)
        enqueue = runtime.store.sqlite.enqueue_export
        def interrupt(**kwargs):
            result = enqueue(**kwargs)
            if kwargs["stream"] == _stream(kind):
                raise _InterruptedWrite()
            return result
        monkeypatch.setattr(runtime.store.sqlite, "enqueue_export", interrupt)
        with pytest.raises(_InterruptedWrite):
            _operation(runtime, kind)
        assert _snapshot(runtime) == before
        assert not _in_transaction(runtime)
        monkeypatch.setattr(runtime.store.sqlite, "enqueue_export", enqueue)
        assert _operation(runtime, kind)


@pytest.mark.parametrize("kind", ("pattern", "outcome"))
def test_independent_connections_cannot_claim_same_auxiliary_id(tmp_path, kind):
    with Runtime.create(root=tmp_path) as first, Runtime.create(root=tmp_path) as second:
        scopes = [SCOPE, {**SCOPE, "user_id": "other"}]
        runtimes = [first, second]
        if kind == "outcome":
            for index, runtime in enumerate(runtimes):
                _event(runtime, "race-event-" + str(index), scopes[index])
        barrier = Barrier(2)
        def attempt(index):
            barrier.wait(timeout=5)
            try:
                if kind == "pattern":
                    _pattern(runtimes[index], scope=scopes[index])
                else:
                    runtimes[index].record_outcome("race-event-" + str(index), {"id": "race-outcome", "outcome": "good"}, scope=scopes[index])
                return index
            except ValueError as exc:
                assert str(exc) == ("pattern_scope_conflict" if kind == "pattern" else "outcome_scope_conflict")
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [index for index in pool.map(attempt, range(2)) if index is not None]
        assert len(winners) == 1
        table, identifier = ("intent_patterns", "review3-policy") if kind == "pattern" else ("event_outcomes", "race-outcome")
        with first.store.locked():
            row = first.store.sqlite.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone()
        assert row["user_id"] == scopes[winners[0]]["user_id"]
        if kind == "outcome":
            assert row["event_id"] == "race-event-" + str(winners[0])


def test_low_level_identity_rejection_preserves_callers_transaction(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        _pattern(runtime)
        before = _snapshot(runtime)
        with runtime.store.locked():
            sqlite = runtime.store.sqlite
            sqlite.execute("BEGIN IMMEDIATE")
            sqlite.record_event({"id": "outer-event"}, scope=SCOPE, commit=False)
            with pytest.raises(ValueError, match="pattern_scope_conflict"):
                sqlite.upsert_intent_pattern({"id": "review3-policy", "pattern": "foreign"}, scope={**SCOPE, "user_id": "other"}, commit=False)
            assert sqlite.in_transaction
            assert sqlite.execute("SELECT id FROM events WHERE id='outer-event'").fetchone() is not None
            sqlite.rollback()
        assert _snapshot(runtime) == before


def test_policy_event_attribution_is_preserved_during_rebuild(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        _pattern(runtime)
        event = runtime.record_event({"id": "bound-event", "policy_attribution": {"policy_suggestion_ids": ["review3-policy"]}}, scope=SCOPE)
        assert event["policy_version_ids"]
        runtime.record_outcome(event["id"], {"id": "bound-outcome", "outcome": "good"}, scope=SCOPE)
        with runtime.store.locked():
            before_event = json.loads(runtime.store.sqlite.execute("SELECT payload_json FROM events WHERE id='bound-event'").fetchone()[0])
            before_outcome = json.loads(runtime.store.sqlite.execute("SELECT payload_json FROM event_outcomes WHERE id='bound-outcome'").fetchone()[0])
        rebuilt = runtime.store.rebuild_sqlite_from_jsonl(replace=True)
        assert rebuilt["ok"] is True, rebuilt
        with runtime.store.locked():
            after_event = json.loads(runtime.store.sqlite.execute("SELECT payload_json FROM events WHERE id='bound-event'").fetchone()[0])
            after_outcome = json.loads(runtime.store.sqlite.execute("SELECT payload_json FROM event_outcomes WHERE id='bound-outcome'").fetchone()[0])
        assert after_event == before_event
        assert after_outcome == before_outcome
