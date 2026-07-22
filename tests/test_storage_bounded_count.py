from __future__ import annotations

import sqlite3

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.sqlite_store import SqliteRecordStore


SCOPE = ScopeRef(tenant_id="tenant", agent_id="agent", workspace_id="workspace", user_id="user")


def test_bounded_exact_count_stops_at_limit_and_uses_covering_scope_source_index(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    for index in range(12):
        store.append(
            RecordEnvelope.create(
                kind="memory",
                title=f"record {index}",
                scope=SCOPE,
                source_id="alpha" if index < 10 else "beta",
                status="active",
            )
        )

    assert store.count_records_bounded_exact_scope(
        scope=SCOPE, status="active", source_ids=["alpha"], kinds=["memory"], limit=5
    ) == 5
    assert store.count_records_bounded_exact_scope(
        scope=SCOPE, status="active", source_ids=["beta"], limit=5
    ) == 2
    assert store.count_records_bounded_exact_scope(
        scope=SCOPE, status="active", source_ids=[], limit=5
    ) == 0
    assert store.count_records_bounded_exact_scope(
        scope=SCOPE, status="active", source_ids=["alpha"], limit=0
    ) == 0

    plan = store.sqlite.conn.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM (SELECT 1 FROM records "
        "WHERE tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? "
        "AND source_id IN (?) AND status=? AND kind IN (?) LIMIT ?)",
        ("tenant", "agent", "workspace", "user", "alpha", "active", "memory", 5),
    ).fetchall()
    detail = " ".join(str(row[3]) for row in plan).upper()
    assert "COVERING INDEX IDX_RECORDS_SCOPE_SOURCE_STATUS_KIND" in detail
    assert "SCAN RECORDS" not in detail
    store.close()


def test_bounded_exact_count_visits_constant_work_at_100k_rows() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE records(storage_key TEXT PRIMARY KEY,tenant_id TEXT,agent_id TEXT,"
        "workspace_id TEXT,user_id TEXT,source_id TEXT,status TEXT,kind TEXT)"
    )
    connection.execute(
        "CREATE INDEX idx_records_scope_source_status_kind ON records("
        "tenant_id,agent_id,workspace_id,user_id,source_id,status,kind)"
    )
    connection.execute(
        "WITH digits(d) AS (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) "
        "INSERT INTO records SELECT printf('row-%d%d%d%d%d',a.d,b.d,c.d,d.d,e.d),"
        "'tenant','agent','workspace','user','alpha','active','memory' "
        "FROM digits a CROSS JOIN digits b CROSS JOIN digits c CROSS JOIN digits d CROSS JOIN digits e"
    )
    shell = object.__new__(SqliteRecordStore)
    shell.conn = connection
    progress_ticks = 0

    def progress() -> int:
        nonlocal progress_ticks
        progress_ticks += 1
        return 0

    connection.set_progress_handler(progress, 100)
    result = shell.count_records_bounded_exact_scope(
        scope=SCOPE,
        status="active",
        source_ids=["alpha"],
        kinds=["memory"],
        limit=5,
    )
    connection.set_progress_handler(None, 0)

    assert result == 5
    assert progress_ticks < 10
    connection.close()
