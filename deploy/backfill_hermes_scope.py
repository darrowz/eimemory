"""Explicit, additive Hermes legacy-scope backfill; never widens query ACLs.

Only active Hermes memories for an explicitly supplied tenant/user are copied
from the old generic host namespace to the canonical Hermes namespace. Originals
remain intact. No raw logs, claims, receipts, other sources or other users move.
"""
from dataclasses import asdict, replace
from hashlib import sha256
import json


def backfill(store, *, tenant, user, apply=False, expected_digest=''):
    from eimemory.models.records import ScopeRef
    if not tenant or not user or user == 'default':
        raise ValueError('explicit_owner_required')
    source = ScopeRef(tenant_id=tenant, agent_id='default', workspace_id='hermes::channel::hermes', user_id=user)
    target = replace(source, agent_id='hongtu', workspace_id='embodied::channel::hermes')
    store.assert_connection_lock_held()
    if store.conn.in_transaction:
        raise ValueError('existing_transaction')
    store.conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
    try:
        ids = store.conn.execute("""SELECT record_id FROM records WHERE tenant_id=? AND user_id=?
            AND agent_id=? AND workspace_id=? AND kind='memory' AND status='active'
            AND source_id='hermes' AND source IN ('hermes.memory','hermes.memory_write','hermes.l1')
            ORDER BY record_id LIMIT 2001""", (tenant, user, source.agent_id, source.workspace_id)).fetchall()
        if len(ids) > 2000:
            raise ValueError('backfill_bound_exceeded')
        copies = []
        fingerprints = []
        for row in ids:
            original = store.get_by_id(row[0], scope=source)
            if original is None:
                raise ValueError('source_unavailable')
            digest = sha256(json.dumps(original.to_dict(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            existing = store.get_by_id(original.record_id, scope=target)
            if existing is not None:
                marker = existing.provenance.get('hermes_scope_backfill', {})
                if marker.get('source_digest') == digest and marker.get('source_scope') == asdict(source):
                    continue
                raise ValueError('target_conflict')
            copy = replace(original, scope=target, provenance={**original.provenance,
                'identity_scope_preserved': True,
                'hermes_scope_backfill': {'source_scope': asdict(source), 'source_digest': digest}})
            copies.append(copy)
            fingerprints.append([original.record_id, digest])
        digest = sha256(json.dumps({'source':asdict(source),'target':asdict(target),'records':fingerprints},sort_keys=True).encode()).hexdigest()
        if apply:
            if digest != expected_digest:
                raise ValueError('plan_changed')
            for copy in copies:
                store.upsert(copy, commit=False)
            store.conn.commit()
        else:
            store.conn.rollback()
        return {'ok':True,'planned':len(copies),'written':len(copies) if apply else 0,
                'digest':digest,'source_scope':asdict(source),'target_scope':asdict(target),
                'record_ids':[r.record_id for r in copies]}
    except BaseException:
        store.conn.rollback()
        raise


def main():
    import argparse
    from pathlib import Path
    import threading
    from eimemory.storage.sqlite_store import SqliteRecordStore
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--tenant',required=True);p.add_argument('--user',required=True)
    p.add_argument('--apply',action='store_true');p.add_argument('--expected-digest',default='')
    args=p.parse_args()
    if not args.database.is_file():raise ValueError('database_missing')
    store=SqliteRecordStore(args.database)
    lock=threading.RLock();store.bind_runtime_lock(lock)
    try:
        with lock:print(json.dumps(backfill(store,tenant=args.tenant,user=args.user,apply=args.apply,expected_digest=args.expected_digest)))
    finally:store.conn.close()

if __name__=='__main__':main()
