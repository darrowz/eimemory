from __future__ import annotations

from pathlib import Path

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.retrieval.proactive import ProactiveRecallService
from eimemory.storage.runtime_store import RuntimeStore


BASE_SCOPE = {
    "tenant_id": "tenant-a",
    "agent_id": "agent-a",
    "workspace_id": "workspace-a",
    "user_id": "user-a",
}
RELEASE = {
    "release_commit": "a" * 40,
    "release_version": "1.9.80",
    "deployment_receipt_id": "receipt-a",
    "release_session_id": "release-session-a",
}


class FixedEngine:
    policy_version = "governed-recall.test"

    def __init__(self, records: list[RecordEnvelope]) -> None:
        self.records = records

    def bind(self, _memory) -> None:
        return None

    def recall(self, _request) -> RecallBundle:
        selected = [
            {
                "record_id": record.record_id,
                "source_id": record.source_id,
                "evidence": ["keyword_exact"],
                "score": 0.99,
            }
            for record in self.records
        ]
        return RecallBundle(
            items=list(self.records),
            rules=[],
            reflections=[],
            confidence=0.99,
            next_action_hint="",
            explanation={
                "fusion": {"policy_version": "rrf.test", "selected": selected},
                "scoring": [
                    {
                        "record_id": record.record_id,
                        "source_id": record.source_id,
                        "quality_score": 0.99,
                    }
                    for record in self.records
                ],
            },
        )


def _record(text: str, *, source_id: str = "alpha", scope: dict | None = None) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="memory",
        title=text,
        summary=text,
        content={"text": text, "memory_type": "durable_fact"},
        scope=ScopeRef.from_dict(resolve_channel_scope("codex", scope or BASE_SCOPE)),
        source="codex.memory",
        source_id=source_id,
        meta={"memory_type": "durable_fact", "force_capture": True},
    )


def _all_persistent_bytes(root: Path) -> bytes:
    return b"".join(
        path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def test_proactive_ledgers_never_persist_turn_query_or_rendered_record_plaintext(tmp_path) -> None:
    turn_secret = "TURN-SECRET-CANARY-7fb5d147"
    query_secret = "QUERY-SECRET-CANARY-93c88d1a"
    rendered_secret = "RENDERED-SECRET-CANARY-1a2f817e"
    record = _record(rendered_secret)
    runtime = Runtime(RuntimeStore(tmp_path), recall_engine=FixedEngine([record]))
    service = ProactiveRecallService(runtime, release_identity=RELEASE, control_percent=0)

    service.complete_turn(
        channel="codex",
        scope=BASE_SCOPE,
        source_ids=["alpha"],
        session_id="secure-session",
        turn_id="turn-1",
        user_summary=turn_secret,
        assistant_summary="acknowledged",
    )
    decision = service.decide(
        channel="codex",
        scope=BASE_SCOPE,
        source_ids=["alpha"],
        session_id="secure-session",
        query_id="turn-2",
        query=f"Recall policy {query_secret}",
    )
    assert rendered_secret in decision["context"]
    runtime.close()

    persisted = _all_persistent_bytes(tmp_path)
    assert turn_secret.encode() not in persisted
    assert query_secret.encode() not in persisted
    assert rendered_secret.encode() not in persisted


def test_proactive_plaintext_migration_clears_all_legacy_columns(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    conn = store.sqlite.conn
    exact = resolve_channel_scope("codex", BASE_SCOPE)
    conn.execute(
        "INSERT INTO proactive_turns(channel,tenant_id,agent_id,workspace_id,user_id,source_key,"
        "session_id,turn_id,summary,entities_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("codex", *[exact[key] for key in ("tenant_id", "agent_id", "workspace_id", "user_id")],
         "source-key", "session", "turn", "legacy-summary", '["legacy-entity"]', "2020-01-01"),
    )
    conn.execute(
        "INSERT INTO proactive_decisions(decision_id,channel,tenant_id,agent_id,workspace_id,user_id,"
        "source_key,source_ids_json,session_id,turn_id,query_id,query_digest,query_text,task_type,"
        "effective_query_digest,policy_version,release_commit,release_version,deployment_receipt_id,"
        "release_session_id,release_bound,control_cohort,pair_id,context_text,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("pd:legacy", "codex", *[exact[key] for key in ("tenant_id", "agent_id", "workspace_id", "user_id")],
         "source-key", '["alpha"]', "session", "turn", "turn", "d" * 64, "legacy-query", "code.task",
         "e" * 64, "policy", "a" * 40, "1.9.80", "receipt", "release", 1, 0, "pair", "legacy-context",
         "2020-01-01", "2020-01-01"),
    )
    conn.execute(
        "INSERT INTO proactive_decision_items(decision_id,citation,record_id,source_id,confidence,state,"
        "item_order,title_text,content_text,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("pd:legacy", "pm:0123456789abcdefabcd", "record", "alpha", 0.9, "volunteered", 1,
         "legacy-title", "legacy-content", "2020-01-01"),
    )
    conn.execute(
        "DELETE FROM schema_migrations WHERE migration_id='proactive.storage_text_free.v1'"
    )
    conn.commit()
    store.close()

    migrated = RuntimeStore(tmp_path)
    row = migrated.sqlite.conn.execute(
        "SELECT query_text,context_text FROM proactive_decisions WHERE decision_id='pd:legacy'"
    ).fetchone()
    item = migrated.sqlite.conn.execute(
        "SELECT title_text,content_text FROM proactive_decision_items WHERE decision_id='pd:legacy'"
    ).fetchone()
    turn = migrated.sqlite.conn.execute(
        "SELECT summary,entities_json FROM proactive_turns WHERE turn_id='turn'"
    ).fetchone()
    assert tuple(row) == ("", "")
    assert tuple(item) == ("", "")
    assert tuple(turn) == ("", "[]")
    migrated.close()
    persisted = _all_persistent_bytes(tmp_path)
    for legacy in (
        b"legacy-summary",
        b"legacy-entity",
        b"legacy-query",
        b"legacy-context",
        b"legacy-title",
        b"legacy-content",
    ):
        assert legacy not in persisted


def test_restart_rehydrates_only_exact_scope_and_source_record_refs(tmp_path) -> None:
    correct = _record("exact authoritative content")
    first_runtime = Runtime(RuntimeStore(tmp_path), recall_engine=FixedEngine([correct]))
    first_runtime.store.append(correct)
    first = ProactiveRecallService(first_runtime, release_identity=RELEASE, control_percent=0)
    request = {
        "channel": "codex",
        "scope": BASE_SCOPE,
        "source_ids": ["alpha"],
        "session_id": "restart-session",
        "query_id": "restart-turn",
        "query": "Recall exact authoritative content",
    }
    original = first.decide(**request)
    first_runtime.close()

    second_runtime = Runtime(RuntimeStore(tmp_path), recall_engine=FixedEngine([]))
    wrong = _record(
        "WRONG CROSS SCOPE CONTENT",
        source_id="alpha",
        scope={**BASE_SCOPE, "workspace_id": "other-workspace"},
    )
    wrong.record_id = correct.record_id
    second_runtime.store.append(wrong)
    second = ProactiveRecallService(second_runtime, release_identity=RELEASE, control_percent=0)
    replay = second.decide(**request)

    assert replay["context"] == original["context"]
    assert "exact authoritative content" in replay["context"]
    assert "WRONG CROSS SCOPE CONTENT" not in replay["context"]
    assert len(replay["items"]) <= 3
    second_runtime.close()


def test_deleted_authoritative_record_is_never_resurrected_from_candidate_cache(tmp_path) -> None:
    record = _record("must disappear after authoritative deletion")
    runtime = Runtime(RuntimeStore(tmp_path), recall_engine=FixedEngine([record]))
    runtime.store.append(record)
    service = ProactiveRecallService(runtime, release_identity=RELEASE, control_percent=0)
    request = {
        "channel": "codex",
        "scope": BASE_SCOPE,
        "source_ids": ["alpha"],
        "session_id": "delete-session",
        "query_id": "delete-turn",
        "query": "Recall deletion behavior",
    }
    first = service.decide(**request)
    assert first["context"]
    runtime.store.sqlite.conn.execute(
        "DELETE FROM records WHERE record_id=? AND source_id=?",
        (record.record_id, record.source_id),
    )
    runtime.store.sqlite.conn.commit()

    replay = service.decide(**request)

    assert replay["items"] == []
    assert replay["context"] == ""
    runtime.close()


def test_restart_without_authoritative_context_ref_is_observable_not_silent(tmp_path) -> None:
    first_runtime = Runtime(RuntimeStore(tmp_path), recall_engine=FixedEngine([]))
    first = ProactiveRecallService(first_runtime, release_identity=RELEASE, control_percent=0)
    first.complete_turn(
        channel="codex",
        scope=BASE_SCOPE,
        source_ids=["alpha"],
        session_id="lost-window-session",
        turn_id="lost-window-turn",
        user_summary="ephemeral semantic context",
        assistant_summary="acknowledged",
    )
    first_runtime.close()

    second_runtime = Runtime(RuntimeStore(tmp_path), recall_engine=FixedEngine([]))
    second = ProactiveRecallService(second_runtime, release_identity=RELEASE, control_percent=0)
    second.decide(
        channel="codex",
        scope=BASE_SCOPE,
        source_ids=["alpha"],
        session_id="lost-window-session",
        query_id="next-turn",
        query="continue",
    )

    assert second_runtime.store.list_proactive_bypasses(limit=5)[0]["reason"] == (
        "turn_context_unavailable_after_restart"
    )
    second_runtime.close()
