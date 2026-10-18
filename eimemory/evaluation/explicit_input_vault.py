"""Private, opt-in, bounded original inputs for explicit recall review.

Only signed capture digests are exported. This cache has no label authority.
Its identity is the exact request/capture, independent of proactive decisions.
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import os

from eimemory.evaluation.query_input_vault import configured_capture_scopes


def capture_explicit_input(runtime, *, capture_id, scope, request, query, result):
    if os.environ.get('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '0') != '1':
        return {'status': 'disabled'}
    try:
        allowed = configured_capture_scopes()
    except ValueError:
        return {'status': 'scope_policy_invalid'}
    sources = request.get('source_ids')
    # New automatic explicit capture always needs an exact opt-in policy.
    if (allowed is None or not isinstance(sources, list) or len(sources) != 1
            or not any(all(entry[k] == v for k, v in asdict(scope).items())
                       and entry['channel'] == request['channel']
                       and entry['source_id'] == sources[0] for entry in allowed)):
        return {'status': 'scope_not_enabled'}
    if not isinstance(query, str) or not 0 < len(query) <= 16000:
        return {'status': 'input_bounds_rejected'}
    payload = json.dumps({'request': request, 'query': query, 'result': result},
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(payload.encode()) > 131072:
        return {'status': 'input_bounds_rejected'}
    digest = sha256(payload.encode()).hexdigest()
    try:
        return _save(runtime, capture_id=capture_id, payload=payload, digest=digest)
    except Exception:
        # This optional private cache must never turn a successful read into
        # an RPC failure, including failure to acquire the SQLite writer lock.
        return {'status': 'capture_unavailable'}


def _save(runtime, *, capture_id, payload, digest):
    with runtime.store.locked() as conn:
        owns = not conn.in_transaction
        if owns:
            conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute('SAVEPOINT explicit_query_capture')
            conn.execute('CREATE TABLE IF NOT EXISTS explicit_query_input_vault ('
                         'capture_id TEXT PRIMARY KEY, payload TEXT NOT NULL, input_digest TEXT NOT NULL,'
                         'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            conn.execute('INSERT OR IGNORE INTO explicit_query_input_vault '
                         '(capture_id,payload,input_digest) VALUES(?,?,?)', (capture_id, payload, digest))
            row = conn.execute('SELECT input_digest FROM explicit_query_input_vault WHERE capture_id=?',
                               (capture_id,)).fetchone()
            if row['input_digest'] != digest:
                raise ValueError('explicit_input_identity_conflict')
            conn.execute("DELETE FROM explicit_query_input_vault WHERE created_at<datetime('now','-30 days')")
            conn.execute('DELETE FROM explicit_query_input_vault WHERE rowid IN ('
                         'SELECT rowid FROM explicit_query_input_vault ORDER BY rowid DESC LIMIT -1 OFFSET 10000)')
            live = conn.execute('SELECT 1 FROM explicit_query_input_vault WHERE capture_id=?', (capture_id,)).fetchone()
            conn.execute('RELEASE explicit_query_capture')
            if owns:
                conn.commit()
            return {'status': 'captured', 'input_digest': digest} if live else {'status': 'input_unavailable'}
        except Exception:
            if conn.in_transaction:
                try:
                    conn.execute('ROLLBACK TO explicit_query_capture')
                    conn.execute('RELEASE explicit_query_capture')
                except Exception:
                    pass
            if owns:
                conn.rollback()
            return {'status': 'capture_unavailable'}


def load_explicit_input(runtime, *, capture):
    body = capture.content
    commitment = body.get('capture_input') or {}
    if commitment.get('status') != 'captured':
        raise ValueError('explicit_original_input_unavailable')
    with runtime.store.locked() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='explicit_query_input_vault'").fetchone():
            raise ValueError('explicit_original_input_unavailable')
        row = conn.execute('SELECT payload,input_digest FROM explicit_query_input_vault '
                           "WHERE capture_id=? AND created_at>=datetime('now','-30 days')",
                           (capture.record_id,)).fetchone()
    if (row is None or sha256(row['payload'].encode()).hexdigest() != row['input_digest']
            or row['input_digest'] != commitment.get('input_digest')):
        raise ValueError('explicit_original_input_digest_mismatch')
    try:
        payload = json.loads(row['payload'])
        from eimemory.evaluation.explicit_recall import _digest, _private_result
        if (payload['request'] != body['request']
                or sha256(payload['query'].encode()).hexdigest() != body['request']['query_digest']
                or _digest(payload['result']) != body['actual_result_digest']
                or _private_result(payload['result']) != body['result']):
            raise ValueError('explicit_original_input_digest_mismatch')
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError('explicit_original_input_digest_mismatch') from None
    return {**payload, 'input_digest': row['input_digest']}
