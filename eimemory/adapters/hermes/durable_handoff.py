"""Read-only verification of Hermes' committed transcript handoff.

Only plain-text, complete turns are supported. Historical turns require the
explicit host enqueue-time snapshot contract and verified database boundaries. The host's documented
initialize hermes_home and session binding select the database, never messages.
No SessionDB constructor (schema maintenance) or whole-history read is needed.
"""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3


def snapshot_turn(messages):
    if not isinstance(messages, list) or not messages:
        return None
    tail = messages[-128:]
    if any(not isinstance(m, dict) for m in tail):
        return None
    start = next((i for i in range(len(tail) - 1, -1, -1)
                  if tail[i].get('role') == 'user'), None)
    if start is None:
        return None
    turn = tail[start:]
    if any(m.get('content') is not None and (not isinstance(m['content'], str)
           or len(m['content']) > 64000) for m in turn):
        return None
    # Copy only evidence fields in the bounded current turn, never history or
    # arbitrary host metadata. Incompatible custom objects remain unsupported.
    keys = ('id', '_row_id', '_db_persisted', 'role', 'content', 'tool_name',
            'tool_call_id', 'tool_calls', 'session_id', 'scope', 'source_id', 'source_event_id')
    try:
        selected = [{k: m[k] for k in keys if k in m} for m in turn]
        if len(json.dumps(selected, ensure_ascii=False)) > 1_000_000:
            return None
        return deepcopy(selected)
    except (ValueError, TypeError, RecursionError):
        return None


def validate_durable_turn(turn, *, hermes_home, bound_session, session_id, host_snapshot=None):
    """Return isolated canonical evidence and a content-free diagnostic code."""
    if not turn:
        return None, 'unsupported_turn_shape'
    if not hermes_home or session_id != bound_session:
        return None, 'unsupported_host_binding'
    if (any(m.get('_db_persisted') is not True or type(m.get('_row_id')) is not int
            or m['_row_id'] <= 0 for m in turn)
            or turn[0].get('role') != 'user' or turn[-1].get('role') != 'assistant'
            or turn[-1].get('tool_calls')):
        return None, 'unpersisted_or_incomplete_turn'
    ids = [m['_row_id'] for m in turn]
    if any(a >= b for a, b in zip(ids, ids[1:])):
        return None, 'invalid_row_order'
    try:
        from hermes_constants import get_hermes_home, get_default_hermes_root
        home = Path(hermes_home).resolve()
        root = get_default_hermes_root().resolve()
        if home != get_hermes_home().resolve():
            return None, 'profile_mismatch'
        profile = 'default' if home == root else home.name if home.parent == root / 'profiles' else None
        if profile is None:
            return None, 'unsupported_profile'
        historical = False
        if host_snapshot is not None:
            from agent.memory_sync_snapshot import CompletedTurnSnapshot
            if (type(host_snapshot) is not CompletedTurnSnapshot
                    or host_snapshot.session_id != session_id
                    or host_snapshot.hermes_home != str(home)
                    or host_snapshot.messages() != turn):
                return None, 'invalid_host_snapshot'
            historical = True
        # mode=ro cannot create a missing DB. One read transaction binds row,
        # completeness and profile checks to the same committed snapshot.
        with closing(sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro',
                                     uri=True, timeout=0.1)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            owner = db.execute('SELECT profile_name FROM sessions WHERE id=?', (session_id,)).fetchone()
            if owner is None or owner['profile_name'] != profile:
                return None, 'session_profile_mismatch'
            marks = ','.join('?' for _ in ids)
            rows = db.execute(
                'SELECT id, role, content, tool_name, tool_call_id, tool_calls FROM messages '
                f'WHERE session_id=? AND active=1 AND id IN ({marks}) '
                'AND (content IS NULL OR length(content)<=64000) '
                'AND (tool_calls IS NULL OR length(tool_calls)<=1000000) ORDER BY id',
                [session_id, *ids]).fetchall()
            # Metadata only: no adjacent message bodies are loaded. Require the
            # exact latest user boundary and all active rows through the tail.
            boundary = db.execute(
                'SELECT id, role FROM messages WHERE session_id=? AND active=1 '
                'AND id>=? ORDER BY id LIMIT 129', (session_id, ids[0])).fetchall()
            # A host enqueue-time snapshot may precede another user turn. The
            # exact original range must still be active and complete, and the
            # immediately following row must begin a new turn (never extra tools
            # or assistant continuation). Plain caller lists remain current-tail only.
            if historical and len(boundary) > len(ids):
                if boundary[len(ids)]['role'] != 'user':
                    return None, 'incomplete_or_stale_boundary'
                boundary = boundary[:len(ids)]
            if ([r['id'] for r in boundary] != ids or len(rows) != len(ids)
                    or [r['id'] for r in boundary if r['role'] == 'user'] != [ids[0]]):
                return None, 'incomplete_or_stale_boundary'
            pending = {}
            for msg, row in zip(turn, rows):
                if any(msg.get(k) != row[k] for k in ('role', 'content', 'tool_name', 'tool_call_id')):
                    return None, 'row_identity_mismatch'
                calls = json.loads(row['tool_calls']) if row['tool_calls'] else None
                if (msg.get('tool_calls') or None) != calls:
                    return None, 'tool_identity_mismatch'
                if calls:
                    if msg['role'] != 'assistant' or pending:
                        return None, 'unsupported_tool_sequence'
                    for call in calls:
                        cid, name = call['id'], call['function']['name']
                        if not cid or cid in pending:
                            return None, 'unsupported_tool_sequence'
                        pending[cid] = name
                elif msg['role'] == 'tool':
                    cid = msg.get('tool_call_id')
                    if cid not in pending or pending.pop(cid) != msg.get('tool_name'):
                        return None, 'unsupported_tool_sequence'
                elif pending:
                    return None, 'unsupported_tool_sequence'
                if 'id' in msg and msg['id'] != row['id']:
                    return None, 'row_identity_mismatch'
            if pending:
                return None, 'unsupported_tool_sequence'
        for msg in turn:
            msg['id'] = msg['_row_id']
        return turn, 'validated_durable_turn'
    except (ImportError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
        return None, 'host_context_unavailable'
