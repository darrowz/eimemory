"""Verified cold prefixes for bounded incident chronology.

Private compressed frames reuse the established payload-segment persistence
and readback implementation. Signed SQLite pointers are committed with prefix
pruning in one transaction, after durable bytes and independent frame readback.
An interrupted pre-commit append cannot remove any original SQL evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import islice
import json
from pathlib import Path
import shutil

from eimemory.contracts.code_incident_coverage import canonical, digest, SCOPE_FIELDS, NATIVE_SOURCES
from eimemory.storage.migrations.code_incident_coverage import JOURNAL, CHECKPOINTS, OBSERVATIONS, trigger_definitions
from eimemory.storage.migrations.code_incident_coverage_archive import ARCHIVES, KEYS, schema_ready
from eimemory.storage.payload_segments import PayloadSegmentStore

SCHEMA = 'code_incident_coverage_archive.v1'
ROWS_SCHEMA = 'code_incident_coverage_archive_rows.v1'
TABLES = (JOURNAL, CHECKPOINTS, OBSERVATIONS)
MAX_ROWS = 256
MAX_READ_SEGMENTS = 64
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
MAX_SEGMENT_FILES = 256
HOT_LIMITS = {JOURNAL: 10_000, CHECKPOINTS: 512, OBSERVATIONS: 512}


class CoverageArchive:
    def __init__(self, store, verify):
        self.conn = store.sqlite.conn
        self.verify = verify
        path = getattr(store.sqlite, 'path', None)
        self.root = Path(path).parent / 'code_incident_coverage_archive' if path is not None else None
        self._reader = None

    def _installed(self):
        return self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ARCHIVES,)).fetchone() is not None

    def _segments(self, write=False):
        if self.root is None: raise ValueError('incident_archive_root_unavailable')
        if write:
            self._write_inventory_bound()
            return PayloadSegmentStore(self.root, max_payload_bytes=MAX_FRAME_BYTES)
        if self._reader is None:
            self._reader = PayloadSegmentStore(self.root, max_payload_bytes=MAX_FRAME_BYTES, read_only=True)
        return self._reader

    def _write_inventory_bound(self):
        # The shared segment store sorts its inventory. Bound that work before
        # constructing a writer; archive_stats alone silently clips its input.
        # Reserve one segment for an append that rotates the current file.
        if self.root is None: raise ValueError('incident_archive_root_unavailable')
        if sum(1 for _ in islice(self.root.glob('payload-*.seg'), MAX_SEGMENT_FILES)) >= MAX_SEGMENT_FILES:
            raise ValueError('incident_archive_capacity_requires_export')

    def _manifest(self, row):
        body = json.loads(row['payload_json'])
        if (digest(body) != row['payload_digest'] or not self.verify(body, SCHEMA)
                or body.get('schema') != SCHEMA or body.get('table_name') not in TABLES
                or any(body.get(key) != row[key] for key in
                       ('table_name', 'first_sequence', 'last_sequence', 'row_count'))
                or any(type(body.get(key)) is not int for key in ('first_sequence', 'last_sequence', 'row_count'))
                or not 1 <= body['row_count'] <= MAX_ROWS
                or body['first_sequence'] < 1
                or body['last_sequence'] - body['first_sequence'] + 1 != body['row_count']):
            raise ValueError('incident_archive_manifest_unverified')
        previous = self.conn.execute(f'SELECT * FROM {ARCHIVES} WHERE table_name=? AND last_sequence<? ORDER BY last_sequence DESC LIMIT 1',
            (body['table_name'], body['first_sequence'])).fetchone()
        if previous is None:
            if body['first_sequence'] != 1 or body.get('previous_digest') != '':
                raise ValueError('incident_archive_prefix_missing')
        elif (previous['last_sequence'] + 1 != body['first_sequence']
                or previous['payload_digest'] != body.get('previous_digest')):
            raise ValueError('incident_archive_chain_changed')
        return body

    def _rows(self, row):
        body = self._manifest(row)
        raw = self._segments().read(body['pointer'])
        if digest(json.loads(raw)) != body.get('rows_digest'):
            raise ValueError('incident_archive_rows_digest_changed')
        packet = json.loads(raw)
        rows = packet.get('rows')
        if (packet.get('schema') != ROWS_SCHEMA or packet.get('table_name') != body['table_name']
                or not isinstance(rows, list) or len(rows) != body['row_count']
                or any(not isinstance(item, dict) or type(item.get('sequence')) is not int
                       or item['sequence'] != body['first_sequence'] + index
                       for index, item in enumerate(rows))):
            raise ValueError('incident_archive_physical_range_changed')
        return rows

    def high_water(self, table):
        if not self._installed(): return 0
        row = self.conn.execute(f'SELECT * FROM {ARCHIVES} WHERE table_name=? ORDER BY last_sequence DESC LIMIT 1', (table,)).fetchone()
        return self._manifest(row)['last_sequence'] if row is not None else 0

    def range_rows(self, table, after, through):
        if not self._installed() or after >= through: return []
        rows = self.conn.execute(f'''SELECT * FROM {ARCHIVES}
            WHERE table_name=? AND last_sequence>? AND first_sequence<=?
            ORDER BY first_sequence LIMIT ?''', (table, after, through, MAX_READ_SEGMENTS + 1)).fetchall()
        if len(rows) > MAX_READ_SEGMENTS: raise ValueError('incident_archive_read_bound_exceeded')
        result = []
        for row in rows:
            result.extend(item for item in self._rows(row) if after < item['sequence'] <= through)
        if len({item['sequence'] for item in result}) != len(result):
            raise ValueError('incident_archive_overlapping_ranges')
        return result

    def key_rows(self, table, *, scope=None, record_id=None, payload_digest=None, limit=1):
        if not self._installed(): return []
        conditions, args = ['table_name=?'], [table]
        if scope is not None:
            for key in SCOPE_FIELDS: conditions.append(key+'=?'); args.append(scope[key])
        if record_id is not None: conditions.append('record_id=?'); args.append(record_id)
        if payload_digest is not None: conditions.append('payload_digest=?'); args.append(payload_digest)
        args.append(limit)
        keys = self.conn.execute(f"SELECT * FROM {KEYS} WHERE {' AND '.join(conditions)} ORDER BY original_sequence DESC LIMIT ?", args).fetchall()
        result = []
        for key in keys:
            manifest = self.conn.execute(f'SELECT * FROM {ARCHIVES} WHERE sequence=?', (key['archive_sequence'],)).fetchone()
            if manifest is None: raise ValueError('incident_archive_key_parent_missing')
            matches = [item for item in self._rows(manifest) if item['sequence'] == key['original_sequence']]
            if len(matches) != 1: raise ValueError('incident_archive_key_row_missing')
            item = matches[0]
            if (key['table_name'] != manifest['table_name']
                    or any(item.get(field) != key[field] for field in (*SCOPE_FIELDS, 'record_id', 'payload_digest'))):
                raise ValueError('incident_archive_key_binding_changed')
            result.append(item)
        return result

    def maintain(self, *, seal, keep=None, batches=16):
        """Called inside the coverage owner's exclusive write transaction."""
        if not self.conn.in_transaction or not schema_ready(self.conn):
            raise ValueError('incident_archive_exclusive_schema_required')
        limits = dict(HOT_LIMITS if keep is None else keep)
        if (set(limits) != set(TABLES) or any(type(value) is not int or value < 1 for value in limits.values())
                or type(batches) is not int or not 1 <= batches <= 64):
            raise ValueError('incident_archive_budget_invalid')
        archived, writer = {table: 0 for table in TABLES}, None
        try:
            for table in TABLES:
                for _ in range(batches):
                    # Keyset retention never scans or serializes record bodies.
                    boundary = self.conn.execute(f'SELECT sequence FROM {table} ORDER BY sequence DESC LIMIT 1 OFFSET ?', (limits[table] - 1,)).fetchone()
                    if boundary is None: break
                    page = [dict(row) for row in self.conn.execute(f'SELECT * FROM {table} WHERE sequence<? ORDER BY sequence LIMIT ?',
                        (boundary['sequence'], MAX_ROWS)).fetchall()]
                    if not page: break
                    prior = self.conn.execute(f'SELECT * FROM {ARCHIVES} WHERE table_name=? ORDER BY last_sequence DESC LIMIT 1', (table,)).fetchone()
                    head = self._manifest(prior)['last_sequence'] if prior is not None else 0
                    if page[0]['sequence'] != head + 1:
                        raise ValueError('incident_archive_hot_prefix_gap')
                    while True:
                        packet = dict(schema=ROWS_SCHEMA, table_name=table, rows=page)
                        raw = canonical(packet).encode()
                        if len(raw) <= MAX_FRAME_BYTES: break
                        if len(page) == 1: raise ValueError('incident_archive_row_exceeds_frame_bound')
                        page = page[:max(1, len(page) // 2)]
                    if any(row['sequence'] != head + index + 1 for index, row in enumerate(page)):
                        raise ValueError('incident_archive_hot_prefix_gap')
                    if writer is None: writer = self._segments(write=True)
                    self._write_inventory_bound()
                    usage = writer.archive_stats(max_segments=MAX_SEGMENT_FILES)
                    if (usage['archive_bytes'] + len(raw) > MAX_ARCHIVE_BYTES
                            or shutil.disk_usage(self.root).free < 2 * MAX_FRAME_BYTES):
                        raise ValueError('incident_archive_capacity_requires_export')
                    pointer = writer.append(raw)
                    if writer.read(pointer) != raw: raise ValueError('incident_archive_durable_readback_failed')
                    body = seal(dict(schema=SCHEMA, table_name=table,
                        first_sequence=page[0]['sequence'], last_sequence=page[-1]['sequence'], row_count=len(page),
                        previous_digest=prior['payload_digest'] if prior is not None else '',
                        rows_digest=digest(packet), pointer=pointer, created_at=datetime.now(timezone.utc).isoformat()), SCHEMA)
                    if not self.verify(body, SCHEMA): raise ValueError('incident_archive_signature_invalid')
                    cursor = self.conn.execute(f'''INSERT INTO {ARCHIVES}(table_name,first_sequence,last_sequence,row_count,payload_json,payload_digest,created_at)
                        VALUES(?,?,?,?,?,?,?)''', (table, body['first_sequence'], body['last_sequence'], len(page), canonical(body), digest(body), body['created_at']))
                    for row in page:
                        if table != JOURNAL or (row['table_name'] == 'records' and row['operation'] == 'INSERT'
                                                 and row['source'] in NATIVE_SOURCES and row['record_kind'] == 'incident'):
                            self.conn.execute(f'''INSERT INTO {KEYS}(table_name,tenant_id,agent_id,workspace_id,user_id,record_id,payload_digest,original_sequence,archive_sequence)
                                VALUES(?,?,?,?,?,?,?,?,?)''', (table, *(row[key] for key in SCOPE_FIELDS), row['record_id'], row['payload_digest'], row['sequence'], cursor.lastrowid))
                    # DDL and DELETE are transactional on this one database.
                    # Only this reviewed owner suspends the exact delete guard;
                    # rollback/crash restores it together with all original rows.
                    name = f'trg_{table}_no_delete'
                    expected = trigger_definitions()[name]
                    actual = self.conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
                    if actual is None or actual[0].strip().rstrip(';') != expected:
                        raise ValueError('incident_archive_original_delete_guard_changed')
                    self.conn.execute('DROP TRIGGER '+name)
                    self.conn.execute(f'DELETE FROM {table} WHERE sequence>=? AND sequence<=?',
                        (body['first_sequence'], body['last_sequence']))
                    self.conn.execute(expected)
                    if len(self._rows(self.conn.execute(f'SELECT * FROM {ARCHIVES} WHERE sequence=?', (cursor.lastrowid,)).fetchone())) != len(page):
                        raise ValueError('incident_archive_postprune_readback_failed')
                    archived[table] += len(page)
            remaining = {table: self.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in TABLES}
            return dict(ok=True, archived=archived, hot_rows=remaining, hot_limits=limits,
                        backlog={table: max(0, remaining[table] - limits[table]) for table in TABLES},
                        archive_bytes=writer.archive_stats(max_segments=MAX_SEGMENT_FILES)['archive_bytes'] if writer is not None else None,
                        archive_capacity_bytes=MAX_ARCHIVE_BYTES,
                        archive_schema=SCHEMA, raw_evidence_deleted=False, prefix_pruning_atomic=True)
        except BaseException:
            # SQL rollback is owned by the caller. Durable frames without a
            # committed pointer remain harmless orphan evidence, never a gap.
            raise
