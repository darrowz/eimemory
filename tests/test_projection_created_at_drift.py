"""records.created_at must stay a projection of the payload envelope.

Prod 2026-10-04..06: 242 Hongtu identity-repair candidates (entity_record,
knowledge_page, replay_result, capability_score, paper_source, ...) had a
first-seen created_at column while payload_json carried the re-emitted
envelope's created_at, so the exact-snapshot identity repair refused them as
``source_projection_or_digest_mismatch`` and ``eimemory nightly`` exited 1.
"""
from __future__ import annotations

from eimemory.identity import hongtu_scope
from eimemory.identity_ops import repair_hongtu_identity
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.inline_digest_repair import repair_inline_projection_created_at
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = ScopeRef.from_dict(hongtu_scope({"user_id": "darrow"}))
OLD = "2026-10-02T19:32:25Z"
NEW = "2026-10-04T19:32:32Z"


def _entity(created_at: str, record_id: str = "ent_created_at_drift", scope: ScopeRef = SCOPE) -> RecordEnvelope:
    record = RecordEnvelope.create(
        kind="entity_record",
        title="Entity: Example",
        summary="Relation compiler output",
        content={"entity": "Example"},
        scope=scope,
        source="eimemory.knowledge.relations",
    )
    record.record_id = record_id
    record.time.created_at = created_at
    record.time.updated_at = created_at
    return record


def _row(store, record_id):
    return store.sqlite.conn.execute(
        "SELECT created_at, updated_at, payload_json, payload_digest FROM records WHERE record_id = ?",
        (record_id,),
    ).fetchone()


def _reemit(store, created_at: str, scope: ScopeRef = SCOPE) -> RecordEnvelope:
    record = _entity(created_at, scope=scope)
    with store._lock:
        store.sqlite.upsert(record)
    return record


def test_upsert_keeps_created_at_column_equal_to_payload(tmp_path):
    store = RuntimeStore(tmp_path)
    try:
        _reemit(store, OLD)
        record = _reemit(store, NEW)
        row = _row(store, record.record_id)
        assert row["created_at"] == NEW == record.time.created_at
        assert row["updated_at"] == NEW
        assert repair_inline_projection_created_at(store, scope=SCOPE)["eligible"] == 0
    finally:
        store.close()


def test_reemitted_identity_candidate_is_repaired_not_blocked(tmp_path):
    store = RuntimeStore(tmp_path)
    try:
        _reemit(store, OLD)
        _reemit(store, NEW)
        runtime = type("R", (), {"store": store})()
        report = repair_hongtu_identity(runtime, apply=True, scope=SCOPE)
        assert report["ok"] is True, report["outcome_counts"]
        assert report["outcome_counts"] == {"applied": 1}
    finally:
        store.close()


def test_legacy_created_at_drift_blocks_identity_repair_until_reconciled(tmp_path):
    store = RuntimeStore(tmp_path)
    try:
        record = _reemit(store, NEW)
        # Pre-1.14.46 upserts left the first-seen created_at in the column.
        store.sqlite.conn.execute("UPDATE records SET created_at = ? WHERE record_id = ?", (OLD, record.record_id))
        store.sqlite.conn.commit()
        before = _row(store, record.record_id)
        runtime = type("R", (), {"store": store})()

        blocked = repair_hongtu_identity(runtime, apply=True, scope=SCOPE)
        assert blocked["ok"] is False
        assert blocked["outcome_counts"] == {"source_projection_or_digest_mismatch": 1}

        dry = repair_inline_projection_created_at(store, scope=SCOPE)
        assert dry["eligible"] == 1 and dry["applied"] is False and dry["unproven"] == []
        assert dry["changes"][0]["old_time"] == OLD and dry["changes"][0]["new_time"] == NEW
        assert _row(store, record.record_id)["created_at"] == OLD

        fixed = repair_inline_projection_created_at(store, scope=SCOPE, apply=True)
        assert fixed["repaired"] == 1
        assert fixed["changes"][0]["payload_integrity"] == "checksum_verified"
        after = _row(store, record.record_id)
        assert after["created_at"] == NEW
        assert (after["payload_json"], after["payload_digest"]) == (before["payload_json"], before["payload_digest"])
        assert repair_inline_projection_created_at(store, scope=SCOPE, apply=True)["repaired"] == 0

        repaired = repair_hongtu_identity(runtime, apply=True, scope=SCOPE)
        assert repaired["ok"] is True and repaired["outcome_counts"] == {"applied": 1}
    finally:
        store.close()


def test_created_at_repair_refuses_unverified_payload(tmp_path):
    store = RuntimeStore(tmp_path)
    try:
        record = _reemit(store, NEW)
        store.sqlite.conn.execute(
            "UPDATE records SET created_at = ?, payload_digest = ? WHERE record_id = ?",
            (OLD, "0" * 64, record.record_id),
        )
        store.sqlite.conn.commit()
        refused = repair_inline_projection_created_at(store, scope=SCOPE, apply=True)
        assert refused["repaired"] == 0 and refused["unproven"] == [record.record_id]
        assert _row(store, record.record_id)["created_at"] == OLD
    finally:
        store.close()


def test_created_at_repair_requires_exact_owner_and_bounded_limit(tmp_path):
    store = RuntimeStore(tmp_path)
    try:
        import pytest

        with pytest.raises(ValueError, match="exact_owner_required"):
            repair_inline_projection_created_at(store, scope=ScopeRef(agent_id="hongtu", workspace_id="", user_id="darrow"))
        with pytest.raises(ValueError, match="limit_invalid"):
            repair_inline_projection_created_at(store, scope=SCOPE, limit=0)
        for index in range(3):
            row = _entity(NEW, f"ent_drift_{index}")
            with store._lock:
                store.sqlite.upsert(row)
        store.sqlite.conn.execute("UPDATE records SET created_at = ?", (OLD,))
        store.sqlite.conn.commit()
        with pytest.raises(ValueError, match="scan_incomplete"):
            repair_inline_projection_created_at(store, scope=SCOPE, limit=2)
        assert {r[0] for r in store.sqlite.conn.execute("SELECT created_at FROM records")} == {OLD}
    finally:
        store.close()


def test_cli_repair_created_at_is_dry_run_unless_apply(tmp_path, monkeypatch, capsys):
    import json

    from eimemory.cli.main import main as cli_main

    root = tmp_path / "runtime"
    monkeypatch.setenv("EIMEMORY_ROOT", str(root))
    # The CLI resolves its default Hongtu scope (OS user on the host).
    cli_scope = ScopeRef.from_dict(hongtu_scope({"agent_id": "cli", "workspace_id": ""}))
    store = RuntimeStore(root)
    try:
        record = _reemit(store, NEW, scope=cli_scope)
        store.sqlite.conn.execute("UPDATE records SET created_at = ? WHERE record_id = ?", (OLD, record.record_id))
        store.sqlite.conn.commit()
    finally:
        store.close()

    assert cli_main(["storage", "repair-created-at"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["eligible"] == 1 and dry["applied"] is False
    assert dry["eligible_by_kind"] == {"entity_record": 1}

    assert cli_main(["storage", "repair-created-at", "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["ok"] is True and applied["repaired"] == 1

    store = RuntimeStore(root)
    try:
        assert _row(store, record.record_id)["created_at"] == NEW
    finally:
        store.close()
