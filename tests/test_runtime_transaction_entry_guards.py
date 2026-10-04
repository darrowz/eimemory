"""Commit-owning RuntimeStore APIs must preserve an outer caller transaction."""
from __future__ import annotations

import pytest

from eimemory.models.memory_edges import MemoryEdge
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


@pytest.fixture
def store(tmp_path):
    value = RuntimeStore(tmp_path)
    try:
        yield value
    finally:
        value.close()


def nested_call(store, operation, record):
    scope = record.scope
    calls = {
        "append_proactive_turn": lambda: store.append_proactive_turn({}),
        "record_proactive_decision": lambda: store.record_proactive_decision({}, [], []),
        "transition_proactive_decision": lambda: store.transition_proactive_decision("decision", {}, {}),
        "record_proactive_outcome": lambda: store.record_proactive_outcome("decision", {}),
        "append_proactive_bypass": lambda: store.append_proactive_bypass({}),
        "rewrite": lambda: store.rewrite(record),
        "repair_status_projection_mismatches": lambda: store.repair_status_projection_mismatches(scope=scope),
        "record_event": lambda: store.record_event({"event_type": "audit.event"}, scope=scope),
        "record_outcome": lambda: store.record_outcome("event", {}, scope=scope),
        "record_terminal_bundle": lambda: store.record_terminal_bundle(
            verified_receipts=[], channel="audit", session_id="session", run_id="run", trace_id="trace",
            event_payload={}, outcome_payload={}, trace_record=record, scope=scope,
        ),
        "upsert_intent_pattern": lambda: store.upsert_intent_pattern({}, scope=scope),
        "rollback_intent_pattern": lambda: store.rollback_intent_pattern("pattern", scope=scope),
        "upsert_memory_edge": lambda: store.upsert_memory_edge(MemoryEdge.create(
            from_id="from", to_id="to", edge_type="temporal", confidence=1.0, evidence_id="from", scope=scope, reason="audit",
        )),
        "upsert_memory_edges": lambda: store.upsert_memory_edges([]),
        "update_intent_pattern_row": lambda: store.update_intent_pattern_row(
            pattern_id="pattern", scope_ref=scope, status="active", payload_json="{}",
            last_rollback_reason="", updated_at="2026-10-02T00:00:00+00:00",
        ),
    }
    return calls[operation]()


@pytest.mark.parametrize("operation", [
    "append_proactive_turn", "transition_proactive_decision",
    "record_proactive_outcome", "append_proactive_bypass", "rewrite",
    "repair_status_projection_mismatches", "record_event", "record_outcome", "record_terminal_bundle",
    "upsert_intent_pattern", "rollback_intent_pattern", "upsert_memory_edge", "upsert_memory_edges",
    "update_intent_pattern_row",
])
def test_nested_commit_owner_rejects_before_touching_caller_transaction(store, operation):
    record = RecordEnvelope.create(kind="memory", title="Audit", scope=ScopeRef(tenant_id="audit"))
    with store.locked() as sql:
        sql.execute("CREATE TABLE audit_sentinel (value TEXT)")
        sql.commit()
        sql.execute("BEGIN")
        sql.execute("INSERT INTO audit_sentinel VALUES ('caller-owned')")
        changes = sql.conn.total_changes
        with pytest.raises(RuntimeError, match=f"^{operation}_requires_own_transaction$"):
            nested_call(store, operation, record)
        assert sql.in_transaction
        assert sql.conn.total_changes == changes
        assert sql.execute("SELECT value FROM audit_sentinel").fetchone()[0] == "caller-owned"
        sql.rollback()
        assert sql.execute("SELECT COUNT(*) FROM audit_sentinel").fetchone()[0] == 0


def test_commit_false_pattern_update_preserves_composition(store):
    scope = ScopeRef(tenant_id="audit")
    with store.locked() as sql:
        sql.execute("CREATE TABLE audit_sentinel (value TEXT)")
        sql.commit()
        sql.execute("BEGIN")
        sql.execute("INSERT INTO audit_sentinel VALUES ('caller-owned')")
        assert store.update_intent_pattern_row(
            pattern_id="absent", scope_ref=scope, status="active", payload_json="{}",
            last_rollback_reason="", updated_at="2026-10-02T00:00:00+00:00", commit=False,
        ) == 0
        assert sql.in_transaction
        assert sql.execute("SELECT value FROM audit_sentinel").fetchone()[0] == "caller-owned"
        sql.rollback()


@pytest.mark.parametrize('fail_capture', [False, True])
def test_proactive_savepoint_preserves_outer_transaction(store, fail_capture):
    payload = dict(decision_id='synthetic-decision', channel='codex', source_key='synthetic-source',
        session_id='synthetic-session', query_id='synthetic-query', query_digest='a'*64,
        policy_version='synthetic-policy', scope={'tenant_id':'audit'})
    with store.locked() as sql:
        sql.execute('CREATE TABLE audit_savepoint_sentinel (value TEXT)')
        sql.commit()
        sql.execute('BEGIN')
        sql.execute("INSERT INTO audit_savepoint_sentinel VALUES ('caller-owned')")
        def capture():
            assert sql.in_transaction
            sql.execute("INSERT INTO audit_savepoint_sentinel VALUES ('capture')")
            if fail_capture:
                raise ValueError('synthetic capture failure')
        if fail_capture:
            with pytest.raises(ValueError, match='synthetic capture failure'):
                store.record_proactive_decision(payload, [], [], capture_input=capture)
            assert sql.load_proactive_decision(payload['decision_id']) is None
        else:
            store.record_proactive_decision(payload, [], [], capture_input=capture)
            assert sql.load_proactive_decision(payload['decision_id']) is not None
        assert sql.in_transaction
        assert [r[0] for r in sql.execute('SELECT value FROM audit_savepoint_sentinel').fetchall()] == (
            ['caller-owned'] if fail_capture else ['caller-owned', 'capture'])
        sql.rollback()
        assert sql.load_proactive_decision(payload['decision_id']) is None
        assert sql.execute('SELECT COUNT(*) FROM audit_savepoint_sentinel').fetchone()[0] == 0
