"""Pure lookup contracts over temporary synthetic SQLite promotion records.

Never exercise apply, recovery, Git, deployment, providers, or live state.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from eimemory.governance.promotion import promotion_code_apply as code_apply
from eimemory.identity import HONGTU_AGENT_ID, HONGTU_WORKSPACE_ID, LEGACY_HONGTU_SCOPE_ALIASES
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.jsonl import canonical_payload_json
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.sqlite_store import MAX_QUERY_LIMIT


SCOPE = ScopeRef(tenant_id="synthetic", agent_id="lookup", workspace_id="local", user_id="one")
SOURCE = code_apply.CODE_APPLY_TRANSACTION_SOURCE
IN_FLIGHT = code_apply.CODE_APPLY_TRANSACTION_IN_FLIGHT
QUARANTINED = code_apply.CODE_APPLY_TRANSACTION_QUARANTINED


@pytest.fixture(autouse=True)
def local_only(monkeypatch, tmp_path):
    def reject(*args, **kwargs):
        raise AssertionError("Only local synthetic SQLite lookups are authorized")

    monkeypatch.setattr(subprocess, "Popen", reject)
    monkeypatch.setattr(os, "system", reject)
    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "bind", reject)
    monkeypatch.setattr(code_apply, "_pm", reject)
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "synthetic-root"))
    monkeypatch.setenv("EIMEMORY_CONFIG_DIR", str(tmp_path / "empty-config"))
    monkeypatch.delenv("EIMEMORY_CONFIG_PATH", raising=False)


@pytest.fixture
def runtime(tmp_path):
    store = RuntimeStore(tmp_path / "sqlite-only")
    try:
        yield SimpleNamespace(store=store)
    finally:
        store.close()


def insert(runtime, name, *, scope=SCOPE, source=SOURCE, status=IN_FLIGHT,
           transaction_type="code_apply", repo_root="/synthetic/repo", stamp=0,
           source_id="default", kind="promotion_request", record_id=None):
    record = RecordEnvelope.create(
        kind=kind, title="Synthetic transaction lookup", scope=scope,
        source=source, source_id=source_id, status=status,
        content={"transaction_type": transaction_type, "repo_root": str(repo_root),
                 "candidate_id": "untrusted-candidate-not-an-identity"},
    )
    record.record_id = record_id or f"synthetic-{name}"
    timestamp = (datetime(2026, 10, 2, tzinfo=timezone.utc) + timedelta(seconds=stamp)).isoformat()
    record.time.created_at = record.time.updated_at = timestamp
    payload = canonical_payload_json(record.to_dict())
    # Insert only fixture records, without invoking runtime append/export paths.
    with runtime.store.locked() as sql:
        sql.execute(
            "INSERT INTO records (storage_key,record_id,kind,status,title,summary,detail,"
            "content_text,source,source_id,agent_id,workspace_id,user_id,tenant_id,"
            "meta_json,payload_json,payload_digest,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sql._storage_key(record), record.record_id, kind, status, record.title, "", "", "",
             source, source_id, scope.agent_id, scope.workspace_id, scope.user_id, scope.tenant_id,
             "{}", payload, sha256(payload.encode()).hexdigest(),
             record.time.created_at, record.time.updated_at),
        )
        sql.commit()
    return record


def lookup(runtime, **kwargs):
    return code_apply._inflight_code_apply_transactions(runtime, **kwargs)


def ids(records):
    return [record.record_id for record in records]


@pytest.mark.parametrize("noise", ["source", "status", "type", "repo", "kind"])
def test_filter_before_default_limit_cannot_hide_old_transaction(runtime, tmp_path, noise):
    root = tmp_path / "repo"
    target = insert(runtime, "old", repo_root=root)
    options = {"source": {"source": "ordinary-promotion"},
               "status": {"status": "completed"},
               "type": {"transaction_type": "another-operation"},
               "repo": {"repo_root": tmp_path / "other"},
               "kind": {"kind": "memory"}}[noise]
    for index in range(100):
        insert(runtime, f"noise-{index}", stamp=index + 1, **({"repo_root": root} | options))
    assert ids(lookup(runtime, scope=SCOPE, repo_root=root)) == [target.record_id]


def test_repo_filter_beyond_storage_query_cap_is_not_offset_scanning(runtime, tmp_path):
    root = tmp_path / "target"
    target = insert(runtime, "older-than-cap", repo_root=root)
    for index in range(MAX_QUERY_LIMIT + 1):
        insert(runtime, f"other-repo-{index}", repo_root=tmp_path / "other", stamp=index + 1)
    assert ids(lookup(runtime, scope=SCOPE, repo_root=root, limit=1)) == [target.record_id]


@pytest.mark.parametrize("requested,expected_count", [(0, 1), (-4, 1), ("2", 2), (2, 2), (100, 4)])
def test_limit_applies_to_matches_in_original_order(runtime, requested, expected_count):
    records = [insert(runtime, str(index), stamp=index // 2) for index in range(4)]
    insert(runtime, "newer-unrelated", stamp=10, source="ordinary-promotion")
    expected = sorted(records, key=lambda item: (item.time.updated_at, item.record_id), reverse=True)
    assert ids(lookup(runtime, scope=SCOPE, limit=requested)) == ids(expected[:expected_count])


def test_storage_limit_cap_and_hydration_work_are_bounded(runtime, monkeypatch):
    for index in range(MAX_QUERY_LIMIT + 1):
        insert(runtime, f"matching-{index:05d}", stamp=index)
    hydrated = []
    original = runtime.store.sqlite._record_from_storage_row

    def track(row, **kwargs):
        hydrated.append(row["payload_json"])
        return original(row, **kwargs)

    monkeypatch.setattr(runtime.store.sqlite, "_record_from_storage_row", track)
    results = lookup(runtime, limit=MAX_QUERY_LIMIT + 900)
    assert len(results) == len(hydrated) == MAX_QUERY_LIMIT
    assert results[0].record_id == f"synthetic-matching-{MAX_QUERY_LIMIT:05d}"


def test_global_scopes_and_duplicate_ids_keep_exact_source_and_identity(runtime, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    one = insert(runtime, "same", scope=SCOPE, source_id="import-one", repo_root=root)
    two = insert(runtime, "same", scope=replace(SCOPE, user_id="two"), source_id="import-two", repo_root=root, stamp=1)
    three = insert(runtime, "same", scope=replace(SCOPE, tenant_id="other"), source_id="import-three", repo_root=root, stamp=2)

    def reject(*args, **kwargs):
        raise AssertionError("Broad record-id rehydration loses exact scoped identity")

    monkeypatch.setattr(runtime.store, "get_by_id", reject)
    results = lookup(runtime, scope=None, repo_root=root)
    assert [(asdict(item.scope), item.source_id) for item in results] == [
        (asdict(item.scope), item.source_id) for item in (three, two, one)]


@pytest.mark.parametrize("mapping", [False, True])
def test_scope_preserves_user_shared_visibility_and_isolation(runtime, mapping):
    mine = insert(runtime, "mine", scope=SCOPE)
    shared = insert(runtime, "shared", scope=replace(SCOPE, user_id=""), stamp=1)
    for field, value in [("tenant_id", "other"), ("agent_id", "other"),
                         ("workspace_id", "other"), ("user_id", "other")]:
        insert(runtime, field, scope=replace(SCOPE, **{field: value}), stamp=10)
    assert ids(lookup(runtime, scope=asdict(SCOPE) if mapping else SCOPE)) == ids([shared, mine])
    assert ids(lookup(runtime, scope=replace(SCOPE, user_id=""))) == ids([shared])


def test_canonical_scope_keeps_legacy_scope_aliases(runtime):
    scope = ScopeRef(tenant_id="synthetic", agent_id=HONGTU_AGENT_ID,
                     workspace_id=HONGTU_WORKSPACE_ID, user_id="one")
    expected = [insert(runtime, "canonical", scope=scope)]
    for index, (agent, workspace) in enumerate(LEGACY_HONGTU_SCOPE_ALIASES):
        alias = replace(scope, agent_id=agent, workspace_id=workspace)
        expected.append(insert(runtime, f"alias-{index}", scope=alias, stamp=index + 1))
    assert set(ids(lookup(runtime, scope=scope))) == set(ids(expected))


def test_quarantined_opt_in_and_exact_resolved_repo_root(runtime, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    alias = tmp_path / "repo-alias"
    alias.symlink_to(root, target_is_directory=True)
    active = insert(runtime, "active", repo_root=root)
    quarantine = insert(runtime, "quarantine", status=QUARANTINED, repo_root=root, stamp=1)
    insert(runtime, "completed", status="completed", repo_root=root, stamp=3)
    insert(runtime, "different-spelling", repo_root=alias, stamp=4)
    assert ids(lookup(runtime, repo_root=alias)) == ids([active])
    assert ids(lookup(runtime, repo_root=alias, include_quarantined=True)) == ids([quarantine, active])
    assert len(lookup(runtime, repo_root=None, include_quarantined=True)) == 3


@pytest.mark.parametrize("fault", ["missing", "scope", "source_id", "record_id", "kind", "status", "source", "repo", "type"])
def test_hydration_missing_or_identity_mismatch_is_not_empty_success(runtime, monkeypatch, fault):
    target = insert(runtime, "hydrate")
    wrong = deepcopy(target)
    if fault == "scope":
        wrong.scope = replace(SCOPE, tenant_id="unexpected")
    elif fault in {"source_id", "record_id", "kind", "status", "source"}:
        setattr(wrong, fault, "memory" if fault == "kind" else "unexpected")
    elif fault in {"repo", "type"}:
        wrong.content["repo_root" if fault == "repo" else "transaction_type"] = "unexpected"
    monkeypatch.setattr(runtime.store.sqlite, "_record_from_storage_row",
                        lambda *_args, **_kwargs: None if fault == "missing" else wrong)
    with pytest.raises(RuntimeError, match="code_apply_transaction_unavailable_or_mismatched"):
        lookup(runtime, scope=SCOPE, repo_root=Path("/synthetic/repo"))


@pytest.mark.parametrize("column", ["source", "status"])
def test_projection_drift_cannot_hide_envelope_inflight_status_or_source(runtime, column):
    target = insert(runtime, "projection-drift")
    with runtime.store.locked() as sql:
        sql.execute(f"UPDATE records SET {column}=? WHERE storage_key=?",
                    ("unexpected", sql._storage_key(target)))
        sql.commit()
    with pytest.raises(RuntimeError, match="code_apply_transaction_unavailable_or_mismatched"):
        lookup(runtime, scope=SCOPE)


def test_sql_filters_precede_limit_and_only_selected_records_are_hydrated(runtime, monkeypatch, tmp_path):
    root = tmp_path / "repo"
    expected = [insert(runtime, f"match-{index}", repo_root=root, stamp=index) for index in range(3)]
    for index in range(101):
        insert(runtime, f"noise-{index}", source="ordinary", repo_root=root, stamp=index + 10)
    statements = []
    hydrated = []
    original_execute = runtime.store.sqlite.execute
    original_hydrate = runtime.store.sqlite._record_from_storage_row

    def execute(sql, params=()):
        statements.append((sql, params))
        return original_execute(sql, params)

    def hydrate(row, **kwargs):
        hydrated.append(row["record_id"])
        return original_hydrate(row, **kwargs)

    monkeypatch.setattr(runtime.store.sqlite, "execute", execute)
    monkeypatch.setattr(runtime.store.sqlite, "_record_from_storage_row", hydrate)
    assert ids(lookup(runtime, scope=SCOPE, repo_root=root, limit=2)) == ids(expected[:0:-1])
    assert hydrated == ids(expected[:0:-1])
    selections = [(sql, params) for sql, params in statements if "FROM selected_records" in sql]
    assert len(selections) == 1
    sql, params = selections[0]
    before_limit = sql.split("LIMIT", 1)[0]
    assert all(field in before_limit for field in
               ("$.source", "$.status", "$.content.transaction_type", "$.content.repo_root",
                "tenant_id", "agent_id", "workspace_id", "user_id"))
    assert params[-1] == 2
    assert "JOIN records USING (storage_key)" in sql


@pytest.mark.parametrize("fault", ["malformed", "digest", "missing_table", "timeout"])
def test_unreadable_or_failed_queries_do_not_report_no_inflight(runtime, monkeypatch, fault):
    target = insert(runtime, "unreadable")
    if fault == "timeout":
        def timeout(*args, **kwargs):
            raise TimeoutError("synthetic query deadline")
        monkeypatch.setattr(runtime.store, "read_consistent", timeout)
        monkeypatch.setattr(runtime.store, "list_records", timeout)
    else:
        with runtime.store.locked() as sql:
            if fault == "missing_table":
                sql.execute("ALTER TABLE records RENAME TO synthetic_hidden_records")
            elif fault == "malformed":
                sql.execute("UPDATE records SET payload_json='{' WHERE storage_key=?", (sql._storage_key(target),))
            else:
                sql.execute("UPDATE records SET payload_digest='wrong' WHERE storage_key=?", (sql._storage_key(target),))
            sql.commit()
    with runtime.store.locked():
        with pytest.raises((RuntimeError, sqlite3.OperationalError, TimeoutError)):
            lookup(runtime, scope=SCOPE)


def test_query_is_read_only_and_keeps_caller_transaction(runtime):
    target = insert(runtime, "read-only")
    with runtime.store.locked() as sql:
        sql.execute("BEGIN")
        before = [tuple(row) for row in sql.execute("SELECT * FROM records")]
        changes = sql.conn.total_changes
        assert ids(lookup(runtime, scope=SCOPE)) == ids([target])
        assert sql.in_transaction
        assert sql.conn.total_changes == changes
        assert [tuple(row) for row in sql.execute("SELECT * FROM records")] == before
        sql.rollback()


def test_valid_empty_result_remains_empty(runtime):
    assert lookup(runtime, scope=SCOPE) == []
