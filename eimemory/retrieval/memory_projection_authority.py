"""Revision fence for an explicitly memory-only optional vector projection.

The default all-record contract is unchanged. A memory-only index still fences
deletes, kind transitions, status changes and alias edits, but not audit writes.
"""
from __future__ import annotations

from typing import Any


class MemoryProjectionAuthority:
    def __init__(self, store: Any) -> None:
        self.store = store
        with store._lock:
            conn = store.sqlite.conn
            in_transaction = conn.in_transaction
            conn.execute('CREATE TABLE IF NOT EXISTS memory_vector_revision ('
                         'singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL)')
            conn.execute('INSERT OR IGNORE INTO memory_vector_revision VALUES(1,0)')
            conn.execute('CREATE TABLE IF NOT EXISTS memory_vector_changes ('
                         'storage_key TEXT PRIMARY KEY, revision INTEGER NOT NULL)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_memory_vector_changes_revision '
                         'ON memory_vector_changes(revision,storage_key)')
            conn.execute('CREATE TABLE IF NOT EXISTS memory_vector_journal_contract ('
                         'singleton INTEGER PRIMARY KEY CHECK(singleton=1), floor_revision INTEGER NOT NULL)')
            installed = conn.execute('SELECT floor_revision FROM memory_vector_journal_contract '
                                     'WHERE singleton=1').fetchone()
            if installed is None:
                conn.execute('INSERT INTO memory_vector_journal_contract '
                             'SELECT 1,revision FROM memory_vector_revision WHERE singleton=1')
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                row_names = ('OLD', 'NEW') if operation == 'UPDATE' else (('OLD',) if operation == 'DELETE' else ('NEW',))
                condition = ' OR '.join(f"{row}.kind = 'memory'" for row in row_names)
                if installed is None:
                    conn.execute(f'DROP TRIGGER IF EXISTS trg_memory_vector_{operation.lower()}')
                    conn.execute(f'DROP TRIGGER IF EXISTS trg_memory_vector_alias_{operation.lower()}')
                journal = ' '.join(
                    'INSERT INTO memory_vector_changes(storage_key,revision) '
                    f'SELECT {row}.storage_key,revision FROM memory_vector_revision WHERE singleton=1 '
                    'ON CONFLICT(storage_key) DO UPDATE SET revision=excluded.revision;'
                    for row in row_names)
                conn.execute(f'CREATE TRIGGER IF NOT EXISTS trg_memory_vector_{operation.lower()} '
                             f'AFTER {operation} ON records WHEN {condition} BEGIN '
                             'UPDATE memory_vector_revision SET revision=revision+1 WHERE singleton=1; '
                             + journal + ' END')
                alias_condition = ' OR '.join(
                    f"EXISTS(SELECT 1 FROM records WHERE storage_key={row}.storage_key AND kind='memory')"
                    for row in row_names)
                conn.execute(f'CREATE TRIGGER IF NOT EXISTS trg_memory_vector_alias_{operation.lower()} '
                             f'AFTER {operation} ON recall_alias_index WHEN {alias_condition} BEGIN '
                             'UPDATE memory_vector_revision SET revision=revision+1 WHERE singleton=1; '
                             + journal + ' END')
            if not in_transaction:
                conn.commit()

    def revision(self) -> str:
        with self.store._lock:
            row = self.store.sqlite.conn.execute(
                'SELECT revision FROM memory_vector_revision WHERE singleton=1').fetchone()
        if row is None:
            raise RuntimeError('authority_revision_unavailable')
        return str(int(row['revision']))

    def head(self) -> tuple[str, str]:
        with self.store._lock:
            row = self.store.sqlite.conn.execute(
                "SELECT updated_at, storage_key FROM records WHERE kind='memory' AND status='active' "
                'ORDER BY updated_at DESC, storage_key DESC LIMIT 1').fetchone()
        return ('', '') if row is None else (str(row['updated_at']), str(row['storage_key']))
