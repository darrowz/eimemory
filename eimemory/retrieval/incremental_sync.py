"""Durable projection snapshots and coalesced change-journal maintenance.

The small SQLite snapshot contains text projections, NOT vectors and NOT a second
authority. It makes a multi-hour bootstrap independent of concurrent live writes.
Once committed, exact change-journal deltas catch up without a full scan.
"""
from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import sqlite3

from .postgres_sync import PostgresVectorIndexSynchronizer, ProjectionCursor, SQLiteProjectionReader
from .postgres_vector import PROJECTION_DIGEST_SCHEMA, embedding_provider_fingerprint, projection_fingerprint


class SnapshotProjectionReader:
    def __init__(self, reader: SQLiteProjectionReader, *, path: Path, fingerprint: str):
        self.path = path
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('snapshot_symlink_forbidden')
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            target = sqlite3.connect(path)
            try:
                target.execute('CREATE TABLE projections(updated_at TEXT,storage_key TEXT PRIMARY KEY,payload TEXT)')
                target.execute('CREATE INDEX projection_cursor ON projections(updated_at,storage_key)')
                target.execute('CREATE TABLE contract(fingerprint TEXT,revision TEXT)')
                # Ensure auxiliary indexes before opening a stable source snapshot.
                with reader.store._lock:
                    reader._ensure_contract_locked()
                    source = reader.store.sqlite.conn
                    source.execute('SAVEPOINT vector_projection_snapshot')
                    try:
                        revision = reader.snapshot_token()
                        cursor = ProjectionCursor()
                        while True:
                            rows = reader.page(cursor, limit=256)
                            if not rows:
                                break
                            target.executemany('INSERT INTO projections VALUES(?,?,?)',
                                [(r['updated_at'],r['storage_key'],json.dumps(r, ensure_ascii=False)) for r in rows])
                            cursor = ProjectionCursor(rows[-1]['updated_at'], rows[-1]['storage_key'])
                        target.execute('INSERT INTO contract VALUES(?,?)', (fingerprint,revision))
                        target.commit()
                    finally:
                        source.execute('RELEASE SAVEPOINT vector_projection_snapshot')
            except Exception:
                target.close()
                path.unlink()  # only the newly-created derived snapshot
                raise
            finally:
                target.close()
        self.conn = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)
        row = self.conn.execute('SELECT fingerprint,revision FROM contract').fetchone()
        if not row or row[0] != fingerprint:
            self.conn.close()
            raise ValueError('projection_snapshot_contract_mismatch')
        self.revision = row[1]

    def snapshot_token(self):
        return self.revision

    def page(self, cursor: ProjectionCursor, *, limit: int):
        return [json.loads(row[0]) for row in self.conn.execute(
            'SELECT payload FROM projections WHERE (updated_at,storage_key)>(?,?) '
            'ORDER BY updated_at,storage_key LIMIT ?', (cursor.updated_at,cursor.storage_key,max(1,min(256,limit))))]

    def close(self):
        self.conn.close()


def delta_snapshot(reader: SQLiteProjectionReader, *, since: str, limit: int):
    """Read a bounded, consistent journal prefix with all ties at its last revision."""
    if reader._memory_authority is None:
        raise ValueError('delta_requires_memory_projection')
    limit = max(2,min(254,int(limit)))  # a key transition can add two keys per revision
    with reader.store._lock:
        reader._ensure_contract_locked()
        conn = reader.store.sqlite.conn
        conn.execute('SAVEPOINT memory_delta_snapshot')
        try:
            floor = conn.execute('SELECT floor_revision FROM memory_vector_journal_contract WHERE singleton=1').fetchone()[0]
            current = int(reader.snapshot_token())
            if not int(floor) <= int(since) <= current:
                raise ValueError('delta_journal_gap')
            changes = conn.execute('SELECT storage_key,revision FROM memory_vector_changes '
                'WHERE revision>? ORDER BY revision,storage_key LIMIT ?', (int(since),limit)).fetchall()
            if len(changes) == limit:
                revision = int(changes[-1]['revision'])
                changes = conn.execute('SELECT storage_key,revision FROM memory_vector_changes '
                    'WHERE revision>? AND revision<=? ORDER BY revision,storage_key', (int(since),revision)).fetchall()
            else:
                revision = current
            keys = [row['storage_key'] for row in changes]
            rows = reader.page(ProjectionCursor(), limit=256, storage_keys=keys)
            head = reader._memory_authority.head()
            return {'revision':str(revision), 'current_revision':str(current),
                    'keys':keys, 'rows':rows, 'head':head}
        finally:
            conn.execute('RELEASE SAVEPOINT memory_delta_snapshot')


def maintain_memory_projection(*, store, repository, config, batch_size=4, max_pages=25):
    reader = SQLiteProjectionReader(store, max_text_chars=config.projection_text_chars, projection_memory_only=True)
    fingerprint = embedding_provider_fingerprint(config.embedding_provider, config)
    projection_fp = projection_fingerprint(config)
    snapshot_key = sha256(json.dumps([str(store.root.resolve()), fingerprint,projection_fp,
                                     config.vector_dimension]).encode()).hexdigest()
    path = store.root / 'state' / 'vector-projections' / (snapshot_key + '.sqlite')
    state = repository.read_index_state()
    floor = store.sqlite.conn.execute(
        'SELECT floor_revision FROM memory_vector_journal_contract WHERE singleton=1').fetchone()[0]
    compatible = (state.ready and state.watermark and state.authority_revision.isdecimal()
        and int(state.authority_revision) >= int(floor)
        and state.embedding_fingerprint == fingerprint and state.projection_fingerprint == projection_fp
        and state.projection_digest_schema == PROJECTION_DIGEST_SCHEMA)
    if path.exists() or not compatible:
        snapshot = SnapshotProjectionReader(reader, path=path, fingerprint=snapshot_key)
        try:
            result = PostgresVectorIndexSynchronizer(reader=snapshot, repository=repository,
                embedding_provider=config.embedding_provider, config=config).sync(
                    batch_size=batch_size,max_pages=max_pages)
        finally:
            snapshot.close()
        result['maintenance_mode'] = 'durable_snapshot'
        if result.get('ok') and result.get('complete'):
            path.unlink()
            result['caught_up'] = reader.snapshot_token() == snapshot.revision
        return result
    processed = embedded = pages = 0
    for _ in range(max(1,min(10000,max_pages))):
        snapshot = delta_snapshot(reader, since=state.authority_revision, limit=batch_size)
        if snapshot['revision'] == state.authority_revision:
            return {'ok':True, 'complete':True, 'caught_up':True, 'processed':processed,
                    'embedded':embedded, 'pages':pages, 'maintenance_mode':'incremental',
                    'watermark':state.watermark, 'authority_revision':state.authority_revision}
        helper = PostgresVectorIndexSynchronizer(reader=reader, repository=repository,
            embedding_provider=config.embedding_provider, config=config)
        rows = snapshot['rows']
        cached = repository.reusable_embeddings(
            projection_digests={r['storage_key']:helper._candidate_projection(r,vector=(),run_id=state.watermark)['projection_digest'] for r in rows},
            embedding_fingerprint=fingerprint,projection_fingerprint=projection_fp)
        missing = [r for r in rows if r['storage_key'] not in cached]
        vectors = []
        provider_batch = max(1,min(batch_size,int(getattr(config.embedding_provider,'max_batch',batch_size))))
        for offset in range(0,len(missing),provider_batch):
            vectors.extend(config.embedding_provider.embed(
                [helper._embedding_text(r) for r in missing[offset:offset+provider_batch]],
                timeout_seconds=helper._embedding_timeout_seconds()))
        if len(vectors) != len(missing) or any(len(v) != config.vector_dimension
                or not all(isfinite(float(x)) for x in v) for v in vectors):
            raise ValueError('embedding_dimension_mismatch')
        cached.update({r['storage_key']:v for r,v in zip(missing,vectors,strict=True)})
        projections = [helper._candidate_projection(r,vector=cached[r['storage_key']],run_id=state.watermark) for r in rows]
        helper.attach_fragments(projections)
        repository.apply_memory_delta(expected_state=state, projections=projections,
            changed_keys=snapshot['keys'], authority_revision=snapshot['revision'], authoritative_head=snapshot['head'])
        pages += 1
        processed += len(snapshot['keys'])
        embedded += len(missing)
        state = repository.read_index_state()
    caught_up = reader.snapshot_token() == state.authority_revision
    return {'ok':True,'complete':caught_up,'caught_up':caught_up,'processed':processed,
            'embedded':embedded,'pages':pages,'maintenance_mode':'incremental',
            'watermark':state.watermark,'authority_revision':state.authority_revision}
