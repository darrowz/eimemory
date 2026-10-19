"""Revision fence for an explicitly memory-only optional vector projection.

The default all-record contract is unchanged. A memory-only index still fences
deletes, kind transitions, status changes and alias edits, but not audit writes.
"""
from __future__ import annotations

from typing import Any


class MemoryProjectionAuthority:
    def __init__(self, store: Any) -> None:
        self.store = store
        self._attached_contract_verified = bool(getattr(store, 'bootstrap_schema', True))
        if not getattr(store, 'bootstrap_schema', True):
            # A recovered ledger may legitimately predate this optional
            # projection. Require it only when a caller actually uses it.
            return

        def _bootstrap(sqlite):
            in_transaction = sqlite.in_transaction
            # The readiness fence reads this exact global active-memory head.
            # A kind/status index still scans and sorts the entire projection;
            # this covering keyset lets cold probes stop at the first row.
            sqlite.execute('CREATE INDEX IF NOT EXISTS idx_records_active_memory_vector_cursor '
                         "ON records(kind,status,updated_at DESC,storage_key DESC) WHERE kind='memory' AND status='active'")
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

    def require_existing_contract(self) -> None:
        self.store.run_locked(self._verify_attached_contract)

    def _verify_attached_contract(self, sqlite) -> None:
        if not self._attached_contract_verified:
            self._require_existing_contract(sqlite)
            self._attached_contract_verified = True

    @staticmethod
    def _require_existing_contract(sqlite) -> None:
        """An attachment never creates a missing journal or resets its floor."""
        required = [("table", name, name) for name in (
            'memory_vector_revision', 'memory_vector_changes', 'memory_vector_journal_contract')]
        required.extend(("index", name, table) for name, table in (
            ('idx_records_active_memory_vector_cursor', 'records'),
            ('idx_memory_vector_changes_revision', 'memory_vector_changes')))
        for operation in ('insert', 'update', 'delete'):
            required.extend((
                ('trigger', 'trg_memory_vector_' + operation, 'records'),
                ('trigger', 'trg_memory_vector_alias_' + operation, 'recall_alias_index')))
        for kind, name, table in required:
            row = sqlite.execute('SELECT type,tbl_name FROM sqlite_master WHERE name=?', (name,)).fetchone()
            if row is None or row['type'] != kind or row['tbl_name'] != table:
                raise RuntimeError('existing_memory_projection_schema_required')
        revision = sqlite.execute('SELECT revision FROM memory_vector_revision WHERE singleton=1').fetchone()
        floor = sqlite.execute('SELECT floor_revision FROM memory_vector_journal_contract WHERE singleton=1').fetchone()
        if (revision is None or floor is None or type(revision['revision']) is not int
            or type(floor['floor_revision']) is not int
            or not 0 <= floor['floor_revision'] <= revision['revision']):
            raise RuntimeError('existing_memory_projection_revision_required')
        sqlite.execute('SELECT storage_key,revision FROM memory_vector_changes LIMIT 0')

    def revision(self) -> str:
        def _read(sqlite):
            self._verify_attached_contract(sqlite)
            return sqlite.execute(
                'SELECT revision FROM memory_vector_revision WHERE singleton=1').fetchone()

        row = self.store.read_consistent(_read)
        if row is None:
            raise RuntimeError('authority_revision_unavailable')
        return str(int(row['revision']))

    def head(self) -> tuple[str, str]:
        def _read(sqlite):
            self._verify_attached_contract(sqlite)
            return sqlite.execute(
                "SELECT updated_at, storage_key FROM records WHERE kind='memory' AND status='active' "
                'ORDER BY updated_at DESC, storage_key DESC LIMIT 1').fetchone()

        row = self.store.read_consistent(_read)
        return ('', '') if row is None else (str(row['updated_at']), str(row['storage_key']))
