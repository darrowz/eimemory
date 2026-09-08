"""Explicit repair of stale digests left by legacy L1 metadata-only backfills.

Never relax normal hydration checks or bless arbitrary payload changes. The
entire pre-backfill payload must reproduce the stored cryptographic digest.
Only the two non-authoritative maintenance markers are allowed to differ.
"""
from dataclasses import asdict
from hashlib import sha256
import json

from eimemory.adapters.runtime.channel import resolve_channel_scope, SUPPORTED_RUNTIME_CHANNELS
from eimemory.models.records import ScopeRef
from .jsonl import payload_digest


_MARKERS = frozenset({'l1_extracted_at', 'l1_backfill_batch'})


def repair_legacy_l1_inline_digests(store, *, scope, apply=False, limit=5000):
    base = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    if not all((base.agent_id, base.workspace_id, base.user_id)):
        raise ValueError('digest_repair_exact_owner_required')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
        raise ValueError('digest_repair_limit_invalid')
    workspaces = sorted({base.workspace_id, *(resolve_channel_scope(c, asdict(base))['workspace_id']
                                              for c in SUPPORTED_RUNTIME_CHANNELS)})
    report = {'schema':'legacy_l1_inline_digest_repair.v1', 'applied':False,
              'scanned':0, 'eligible':0, 'repaired':0, 'unproven':[], 'changes':[]}
    with store._lock:
        conn = store.sqlite.conn
        if conn.in_transaction:
            raise ValueError('digest_repair_active_transaction')
        rows = conn.execute(
            'SELECT storage_key,record_id,kind,status,tenant_id,agent_id,workspace_id,user_id,'
            'source_id,payload_json,payload_pointer_json,payload_digest FROM records '
            "WHERE kind='memory' AND status='active' AND payload_pointer_json='' "
            'AND tenant_id=? AND agent_id=? AND user_id=? AND workspace_id IN ('
            + ','.join('?' for _ in workspaces) + ') ORDER BY storage_key LIMIT ?',
            (base.tenant_id or 'default', base.agent_id, base.user_id, *workspaces, limit+1),
        ).fetchall()
        if len(rows)>limit:
            raise ValueError('digest_repair_scan_incomplete')
        changes=[]
        for row in rows:
            report['scanned']+=1
            payload=json.loads(row['payload_json'])
            expected=str(row['payload_digest'] or '')
            current=payload_digest(payload)
            if not expected or expected==current:
                continue
            meta=payload.get('meta')
            before={**payload, 'meta':{k:v for k,v in meta.items() if k not in _MARKERS}} if isinstance(meta,dict) else None
            record=store.sqlite._record_from_payload_dict(payload)
            if (before is None or not _MARKERS.issubset(meta)
                    or payload_digest(before)!=expected or record is None
                    or not store.sqlite._record_matches_projection_row(record,row)):
                report['unproven'].append(row['record_id'])
                continue
            changes.append((row,current))
            report['changes'].append({'record_id':row['record_id'],'storage_key':row['storage_key'],
                                      'old_digest':expected,'new_digest':current})
        report['eligible']=len(changes)
        report['plan_digest']=sha256(json.dumps(report['changes'],sort_keys=True).encode()).hexdigest()
        if apply:
            with conn:
                for row,current in changes:
                    changed=conn.execute(
                        'UPDATE records SET payload_digest=? WHERE storage_key=? '
                        "AND payload_json=? AND payload_digest=? AND payload_pointer_json=''",
                        (current,row['storage_key'],row['payload_json'],row['payload_digest']),
                    ).rowcount
                    if changed!=1:
                        raise ValueError('digest_repair_authority_changed')
            report['applied']=True
            report['repaired']=len(changes)
    return report


def repair_inline_projection_timestamps(store, *, scope, apply=False, limit=5000):
    """Restore derived SQL timestamps from checksum-verified record envelopes."""
    from datetime import datetime
    base = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    if not all((base.agent_id, base.workspace_id, base.user_id)):
        raise ValueError('timestamp_repair_exact_owner_required')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
        raise ValueError('timestamp_repair_limit_invalid')
    workspaces = sorted({base.workspace_id, *(resolve_channel_scope(c, asdict(base))['workspace_id']
                                              for c in SUPPORTED_RUNTIME_CHANNELS)})
    report = {'schema':'inline_projection_timestamp_repair.v1','applied':False,'repaired':0,'changes':[],'unproven':[]}
    with store._lock:
        conn = store.sqlite.conn
        if conn.in_transaction:
            raise ValueError('timestamp_repair_active_transaction')
        rows = conn.execute("SELECT * FROM records WHERE kind='memory' AND status='active' "
            "AND payload_pointer_json='' AND tenant_id=? AND agent_id=? AND user_id=? "
            'AND workspace_id IN ('+','.join('?' for _ in workspaces)+') ORDER BY storage_key LIMIT ?',
            (base.tenant_id or 'default',base.agent_id,base.user_id,*workspaces,limit+1)).fetchall()
        if len(rows)>limit:
            raise ValueError('timestamp_repair_scan_incomplete')
        changes=[]
        for row in rows:
            if not row['payload_digest']:
                report['unproven'].append(row['record_id']); continue
            record=store.sqlite._record_from_storage_row(row,hydrate=True)
            if record is None or not store.sqlite._record_matches_projection_row(record,row):
                report['unproven'].append(row['record_id']); continue
            if datetime.fromisoformat(str(row['updated_at']).replace('Z','+00:00')) == datetime.fromisoformat(str(record.time.updated_at).replace('Z','+00:00')):
                continue
            changes.append((row,str(record.time.updated_at)))
            report['changes'].append({'storage_key':row['storage_key'],'record_id':row['record_id'],
                'old_time':row['updated_at'],'new_time':str(record.time.updated_at)})
        report['eligible']=len(changes)
        if apply:
            with conn:
                for row,new_time in changes:
                    changed=conn.execute('UPDATE records SET updated_at=? WHERE storage_key=? '
                        "AND updated_at=? AND payload_json=? AND payload_digest=? AND payload_pointer_json=''",
                        (new_time,row['storage_key'],row['updated_at'],row['payload_json'],row['payload_digest'])).rowcount
                    if changed!=1:
                        raise ValueError('timestamp_repair_authority_changed')
            report.update(applied=True,repaired=len(changes))
    return report
