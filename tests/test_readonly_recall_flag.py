"""Optional RO recall connection: default OFF path unchanged."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from eimemory.storage.readonly_recall import (
    FLAG_ENV,
    open_readonly_connection,
    readonly_recall_enabled,
    readonly_status,
)


def test_flag_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv(FLAG_ENV, raising=False)
    assert readonly_recall_enabled() is False
    assert open_readonly_connection("/tmp/no-such-eimemory.sqlite") is None
    status = readonly_status()
    assert status["enabled"] is False
    assert status["default"] == "off"


def test_flag_on_opens_ro_uri(tmp_path, monkeypatch) -> None:
    db = tmp_path / "t.sqlite"
    writer = sqlite3.connect(db)
    writer.execute("CREATE TABLE t(x INTEGER)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()
    writer.close()
    monkeypatch.setenv(FLAG_ENV, "1")
    assert readonly_recall_enabled() is True
    conn = open_readonly_connection(db)
    assert conn is not None
    assert conn.execute("SELECT x FROM t").fetchone()[0] == 1
    # query_only should reject writes
    try:
        conn.execute("INSERT INTO t VALUES (2)")
        conn.commit()
        raised = False
    except sqlite3.Error:
        raised = True
    assert raised
    conn.close()
