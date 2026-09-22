"""Revision fence for an explicitly memory-only optional vector projection.

The default all-record contract is unchanged. A memory-only index still fences
deletes, kind transitions, status changes and alias edits, but not audit writes.
"""
from __future__ import annotations

from typing import Any


class MemoryProjectionAuthority:
    def __init__(self, store: Any) -> None:
        self.store = store

        def _bootstrap(sqlite):
            in_transaction = sqlite.conn.in_transaction
            sqlite.execute('CREATE TABLE IF NOT EXISTS memory_vector_revision ('
                         'singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL)')
            sqlite.execute('INSERT OR IGNORE INTO memory_vector_revision VALUES(1,0)')
            sqlite.execute('CREATE TABLE IF NOT EXISTS memory_vector_changes ('
                         'storage_key TEXT PRIMARY KEY, revision INTEGER NOT NULL)')
            sqlite.execute('CREATE INDEX IF NOT EXISTS idx_memory_vector_changes_revision '
                         'ON memory_vector_changes(revision,storage_key)')
            sqlite.execute('CREATE TABLE IF NOT EXISTS memory_vector_journal_contract ('
                         'singleton INTEGER PRIMARY KEY CHECK(singleton=1), floor_revision INTEGER NOT NULL)')
            installed = sqlite.execute('SELECT floor_revision FROM memory_vector_journal_contract '
                                     'WHERE singleton=1').fetchone()
            if installed is None:
                sqlite.execute('INSERT INTO memory_vector_journal_contract '
                             'SELECT 1,revision FROM memory_vector_revision WHERE singleton=1')
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                row_names = ('OLD', 'NEW') if operation == 'UPDATE' else (('OLD',) if operation == 'DELETE' else ('NEW',))
                condition = ' OR '.join(f"{row}.kind = 'memory'" for row in row_names)
                if installed is None:
                    sqlite.execute(f'DROP TRIGGER IF EXISTS trg_memory_vector_{operation.lower()}')
                    sqlite.execute(f'DROP TRIGGER IF EXISTS trg_memory_vector_alias_{operation.lower()}')
                journal = ' '.join(
                    'INSERT INTO memory_vector_changes(storage_key,revision) '
                    f'SELECT {row}.storage_key,revision FROM memory_vector_revision WHERE singleton=1 '
                    'ON CONFLICT(storage_key) DO UPDATE SET revision=excluded.revision;'
                    for row in row_names)
                sqlite.execute(f'CREATE TRIGGER IF NOT EXISTS trg_memory_vector_{operation.lower()} '
                             f'AFTER {operation} ON records WHEN {condition} BEGIN '
                             'UPDATE memory_vector_revision SET revision=revision+1 WHERE singleton=1; '
                             + journal + ' END')
                alias_condition = ' OR '.join(
                    f"EXISTS(SELECT 1 FROM records WHERE storage_key={row}.storage_key AND kind='memory')"
                    for row in row_names)
                sqlite.execute(f'CREATE TRIGGER IF NOT EXISTS trg_memory_vector_alias_{operation.lower()} '
                             f'AFTER {operation} ON recall_alias_index WHEN {alias_condition} BEGIN '
                             'UPDATE memory_vector_revision SET revision=revision+1 WHERE singleton=1; '
                             + journal + ' END')
            if not in_transaction:
                sqlite.commit()

        store.run_locked(_bootstrap)

    def revision(self) -> str:
        def _read(sqlite):
            return sqlite.execute(
                'SELECT revision FROM memory_vector_revision WHERE singleton=1').fetchone()

        row = self.store.read_consistent(_read)
        if row is None:
            raise RuntimeError('authority_revision_unavailable')
        return str(int(row['revision']))

    def head(self) -> tuple[str, str]:
        def _read(sqlite):
            return sqlite.execute(
                "SELECT updated_at, storage_key FROM records WHERE kind='memory' AND status='active' "
                'ORDER BY updated_at DESC, storage_key DESC LIMIT 1').fetchone()

        row = self.store.read_consistent(_read)
        return ('', '') if row is None else (str(row['updated_at']), str(row['storage_key']))
