"""Prepare real root-reviewable managed knowledge configuration as runtime UID.

This command emits a candidate to stdout only. It installs no authority and
creates no key, registry, incident, child policy, or transaction.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3


def prepare_manifest(*,database_path,source_directory,scopes,release_commit,previous_manifest_digest=''):
    from eimemory.contracts.managed_code_incident_proof import (
        DOMAIN,KEY_SEMANTICS,REGISTERED_INGRESS,exact_scope,hex_value,digest,
    )
    from eimemory.core.managed_incident_proof_authority import python_source_inventory,PRODUCER_FILES
    from eimemory.storage.migrations.code_incident_coverage import schema_ready
    hex_value(release_commit,40)
    if previous_manifest_digest:hex_value(previous_manifest_digest)
    owners=[exact_scope(scope) for scope in scopes]
    if not 1<=len(owners)<=32 or len({digest(scope) for scope in owners})!=len(owners):
        raise ValueError('managed_manifest_scope_inventory_invalid')
    source=Path(source_directory).resolve()
    files=python_source_inventory(source)
    if not set(PRODUCER_FILES)<=files:raise ValueError('managed_manifest_producer_inventory_incomplete')
    producer_files={name:sha256((source/name).read_bytes()).hexdigest() for name in sorted(files)}
    conn=sqlite3.connect(Path(database_path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.set_authorizer(lambda action,*_:sqlite3.SQLITE_OK if action in {
            sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_TRANSACTION} else sqlite3.SQLITE_DENY)
        conn.execute('BEGIN')
        if not schema_ready(conn):raise ValueError('managed_manifest_native_capture_not_ready')
        capture={row[0]:row[1] for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'trg_code_incident_coverage_%' OR name LIKE 'idx_incident_coverage_%' ORDER BY name")}
    finally:conn.close()
    return dict(schema='managed_code_incident_manifest.v1',qualification_domain=DOMAIN,defect_key_semantics=KEY_SEMANTICS,
        enabled=True,scopes=owners,source_directory=str(source),producer_files=producer_files,
        release_commit=release_commit,ingress_sources=list(REGISTERED_INGRESS),
        historical_domain='retained_typed_history_and_complete_registered_ingress',
        schema_digest=digest(capture),previous_manifest_digest=previous_manifest_digest)


def main():
    if hasattr(os,'geteuid') and os.geteuid()==0:
        raise SystemExit('prepare must run as the unprivileged runtime account')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',default='/var/lib/eimemory/state/eimemory.sqlite')
    parser.add_argument('--source-directory',required=True)
    parser.add_argument('--scope',action='append',required=True,help='exact four-coordinate JSON object')
    parser.add_argument('--commit',required=True)
    parser.add_argument('--previous-manifest-digest',default='')
    values=parser.parse_args()
    if values.database!='/var/lib/eimemory/state/eimemory.sqlite':
        raise SystemExit('production CLI database is fixed')
    result=prepare_manifest(database_path=values.database,source_directory=values.source_directory,
        scopes=[json.loads(item) for item in values.scope],release_commit=values.commit,
        previous_manifest_digest=values.previous_manifest_digest)
    print(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False))


if __name__=='__main__':main()
