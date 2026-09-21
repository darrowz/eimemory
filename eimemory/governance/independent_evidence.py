"""Local operator CLI. Not registered as an RPC/model tool.

Init/propose/approve/revoke are explicit commands; there is no auto-promotion.
Proposals contain PRIVATE original evidence and must not be added to Git.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from eimemory.storage import independent_evidence as catalog


def private_write(path, value):
    raw=(catalog.canonical(value)+'\n').encode()
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def read_packet(path):
    with open(path,'rb') as f:
        raw=f.read(catalog.MAX_PACKET_BYTES+1)
    if len(raw)>catalog.MAX_PACKET_BYTES:
        raise catalog.CatalogError('packet_bound')
    return catalog.strict_json(raw)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True)
    actions=parser.add_subparsers(dest='action',required=True)
    init=actions.add_parser('init')
    init.add_argument('--confirm-schema-write',action='store_true',required=True)
    inspect=actions.add_parser('inspect')
    inspect.add_argument('--reference',required=True)
    inspect.add_argument('--output',required=True)
    propose=actions.add_parser('propose')
    propose.add_argument('--spec',required=True)
    propose.add_argument('--output',required=True)
    approve=actions.add_parser('approve')
    approve.add_argument('--proposal',required=True)
    approve.add_argument('--expected-digest',required=True)
    approve.add_argument('--reviewer',required=True)
    approve.add_argument('--review-receipt-sha256',required=True)
    for attestation in catalog.ATTESTATIONS:
        approve.add_argument('--attest-'+attestation.replace('_','-'),action='store_true',required=True)
    revoke=actions.add_parser('revoke')
    revoke.add_argument('--contract-id',required=True)
    revoke.add_argument('--reviewer',required=True)
    actions.add_parser('status')
    args=parser.parse_args(argv)
    try:
        write=args.action in {'init','approve','revoke'}
        with catalog.connect(args.root,writable=write) as conn:
            if args.action=='init':
                catalog.install(conn)
                result={'ok':True,'schema':catalog.SCHEMA}
            elif args.action=='inspect':
                packet=catalog.review_fragments(conn,read_packet(args.reference))
                private_write(args.output,packet)
                result={'ok':True,'private_packet_digest':catalog.digest(packet)}
            elif args.action=='propose':
                packet=catalog.prepare(conn,read_packet(args.spec))
                private_write(args.output,packet)
                result={'ok':True,'proposal_digest':catalog.digest(packet)}
            elif args.action=='approve':
                packet=read_packet(args.proposal)
                cid=catalog.approve(conn,packet,expected_digest=args.expected_digest,
                    reviewer=args.reviewer,review_receipt=args.review_receipt_sha256,
                    attestations={k:getattr(args,'attest_'+k) for k in catalog.ATTESTATIONS})
                result={'ok':True,'contract_id':cid}
            elif args.action=='revoke':
                catalog.revoke(conn,args.contract_id,reviewer=args.reviewer)
                result={'ok':True,'revoked':args.contract_id}
            else:
                state=catalog.validate_schema(conn)
                rows=conn.execute('SELECT status,count(*) FROM ie_v1_contract GROUP BY status').fetchall()
                result={'ok':True,'schema':catalog.SCHEMA,'epoch':state[1],'counts':dict(rows),'freshness':'not_evaluated'}
        print(json.dumps(result))
        return 0
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
        # Do not print raw SQL, paths, private packet values or exception strings.
        print('{"ok":false,"reason":"independent_evidence_operation_failed"}')
        return 1


if __name__=='__main__':
    sys.exit(main())
