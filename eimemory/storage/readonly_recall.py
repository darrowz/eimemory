"""Optional read-only SQLite companion for recall hydration (feature-flagged).

Default OFF. When enabled, opens a second ``mode=ro`` URI connection for pure
read paths. Writers and lock fail-closed contracts remain on the primary
connection. This module is the closed design for single-connection serial
throughput; enabling it requires measured production evidence.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


FLAG_ENV = "EIMEMORY_SQLITE_READONLY_RECALL"


def readonly_recall_enabled() -> bool:
    raw = str(os.environ.get(FLAG_ENV, "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def open_readonly_connection(db_path: Path | str) -> sqlite3.Connection | None:
    """Open a read-only URI connection, or None when the feature flag is OFF.

    Fail closed: any open error returns None so callers keep the primary path.
    """
    if not readonly_recall_enabled():
        return None
    path = Path(db_path).resolve()
    if not path.is_file():
        return None
    uri = path.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        conn.close()
        return None
    return conn


def readonly_status(*, db_path: Path | str | None = None) -> dict[str, Any]:
    return {
        "flag_env": FLAG_ENV,
        "enabled": readonly_recall_enabled(),
        "default": "off",
        "db_path": str(db_path or ""),
        "note": "OFF path must remain bit-identical; enable only after measured recall lock wait data.",
    }
