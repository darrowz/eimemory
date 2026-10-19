"""Isolated authority fixtures. None of these receipts are production evidence."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import os
import py_compile
import subprocess
import sys
import shutil
from types import SimpleNamespace
from pathlib import Path

import pytest

from eimemory.contracts.managed_code_incident_proof import (
    DOMAIN, FIELDS, SOURCE_KEY, TRANSACTION_SCHEMA, digest, stable_defect_key,
    validate_snapshot, transaction_snapshot,
)
from eimemory.core import managed_incident_proof_authority as authority
from eimemory.governance.evolution.managed_code_incident_proof import collect_physical_facts
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.ops.release_closure_failure import record_release_closure_failure

SCOPE = dict(tenant_id='default',agent_id='managed-fixture',workspace_id='workspace',user_id='owner')
COMMIT = 'a'*40


def state():
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    conn.executescript('''CREATE TABLE authority_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE managed_knowledge(owner TEXT NOT NULL,defect_key TEXT NOT NULL,record_id TEXT NOT NULL,
        source_digest TEXT NOT NULL,source_time TEXT NOT NULL,sequence INTEGER,human_input INTEGER NOT NULL,
        PRIMARY KEY(owner,record_id,source_digest));''')
    return conn


def original_facts():
    key=stable_defect_key('release.closure_internal_failure',['typed_code'])
    facts=dict(scope=SCOPE,source_record_id='incident-original',source_digest='b'*64,
        defect_key=key,source_created_at='2026-10-19T10:00:00.123456+00:00',
        source_received_at='2026-10-19T10:00:00.123Z',
        history=[],after=0,through=1,high_water=1,complete=True,
        rows=[dict(table_name='records',operation='INSERT',record_kind='incident',source='eimemory.release_closure_failure',
            **SCOPE,sequence=1,record_id='incident-original',payload_digest='b'*64,
            source_time='2026-10-19T10:00:00.123456+00:00',incident_class='release.closure_internal_failure',
            codes_json='["typed_code"]',human_input=0)],
        observed_at='2026-10-19T10:00:01+00:00',activation_reference={
            'schema':'eimemory.runtime_activation_receipt_ref.v1','receipt_id':'1'*64,'digest':'2'*64},
        incident={'incident_id':'incident-original','incident_digest':'e'*64},
        global_facts={key:None for key in FIELDS})
    facts['first_source_evidence']={'schema':'managed_native_first_physical_source.v1',
        'activation_reference':facts['activation_reference'],'created_at':facts['source_created_at']}
    return facts


def activation(facts):
    return dict(schema='eimemory.runtime_activation_attestation.v1',signature_verified=True,
        reference=facts['activation_reference'],registry_generation='c'*64,scope=SCOPE,
        peer_pid=123,peer_uid=1001,manager='system',unit='eimemory-learn-watch.service',
        observed_at='2026-10-19T10:00:00+00:00',trigger_kind='scheduled_timer',job_state='running',job_id=42)


def test_real_root_registry_can_prove_only_registered_domain_and_remembers_cross_release():
    conn=state();facts=original_facts();manifest=dict(scopes=[SCOPE])
    authority._ingest(conn,facts,manifest)
    assert authority._known(conn,facts)==(False,False,1,'')
    assert authority._activation_facts(facts,activation(facts),{'generation':'c'*64,'runtime_uid':1001},{'pid':123,'uid':1001})==(False,'')
    # Release/policy/producer changes cannot make the same typed defect unknown.
    later=deepcopy(facts);later.update(source_record_id='incident-next-release',source_digest='d'*64,
        source_created_at='2026-10-20T10:00:00+00:00',after=1,through=2,high_water=2)
    later['rows'][0].update(sequence=2,record_id=later['source_record_id'],payload_digest=later['source_digest'],
        source_time=later['source_created_at'])
    authority._ingest(conn,later,manifest)
    assert authority._known(conn,later)==(True,False,2,'')
    # The original fact is still novel at its original actual receive boundary.
    assert authority._known(conn,facts)==(False,False,1,'')


@pytest.mark.parametrize('change,reason',[
    ({'trigger_kind':'unknown'},'activation_cause_unknown'),
    ({'signature_verified':False},'original_activation_unverified'),
    ({'reference':{'fixture':'different-inv'}},'original_activation_unverified'),
    ({'peer_pid':0},'original_activation_unverified'),
    ({'job_state':'done'},'activation_cause_unknown'),
    ({'job_id':0},'activation_cause_unknown'),
    ({'observed_at':'2026-10-19T12:00:00+00:00'},'activation_source_time_unbound'),
])
def test_unsigned_unknown_stale_or_wrong_peer_never_becomes_scheduled(change,reason):
    facts=original_facts();value={**activation(facts),**change}
    assert authority._activation_facts(facts,value,{'generation':'c'*64,'runtime_uid':1001},{'pid':123,'uid':1001})==(None,reason)


def test_human_knowledge_late_input_mutation_and_gap_never_prove_absence():
    facts=original_facts();conn=state();manifest=dict(scopes=[SCOPE]);authority._ingest(conn,facts,manifest)
    next_=deepcopy(facts);next_.update(after=1,through=2,high_water=2)
    next_['rows'][0].update(sequence=2,record_id='late-human',payload_digest='d'*64,human_input=1,
        source='eimemory.code_maintenance',source_time='2026-10-18T00:00:00+00:00')
    authority._ingest(conn,next_,manifest)
    assert authority._known(conn,facts)==(None,None,1,'late_prior_typed_knowledge')
    broken=deepcopy(next_);broken['rows'][0]['sequence']=3
    with pytest.raises(ValueError,match='prefix_incomplete'):authority._ingest(conn,broken,manifest)
    broken=deepcopy(next_);broken['rows'][0]['operation']='DELETE';authority._ingest(conn,broken,manifest)
    assert conn.execute("SELECT value FROM authority_meta WHERE key='uncertainty'").fetchone()[0]=='typed_ingress_mutation_or_unregistered'


def test_historical_typed_prehistory_and_cross_scope_isolation():
    facts=original_facts();conn=state();facts['history']=[dict(scope=SCOPE,record_id='original-known-request',
        defect_key=facts['defect_key'],source_digest='d'*64,source_time='2026-10-18T00:00:00+00:00',human_input=False)]
    authority._ingest(conn,facts,dict(scopes=[SCOPE]))
    assert authority._known(conn,facts)==(True,False,1,'')
    other={**facts,'scope':{**SCOPE,'user_id':'unrelated'}}
    assert authority._known(conn,other)==(None,None,None,'original_receive_sequence_unverified')


def test_unclassified_manual_code_report_is_unknown_and_same_symptom_cannot_reset_novelty():
    facts=original_facts();conn=state()
    facts['history']=[dict(scope=SCOPE,record_id='unclassified-original-human-code-report',defect_key='*',
        source_digest='d'*64,source_time='2026-10-18T00:00:00+00:00',human_input=True)]
    authority._ingest(conn,facts,dict(scopes=[SCOPE]))
    assert authority._known(conn,facts)==(None,None,1,'unclassified_typed_prehistory')
    # No source field establishes that two same-symptom causes differ. A new
    # causal story/title/release is not an input to the protected key codec.
    assert stable_defect_key('release.closure_internal_failure',['typed_code'])==facts['defect_key']


def test_issuer_real_hmac_original_bytes_and_readback_not_only_a_flag(tmp_path,monkeypatch):
    """Actual issue/sign/immutable-file/SQLite logic; host FS boundary is separate."""
    conn=state();conn.executescript('''CREATE TABLE managed_receipts(source_record_id TEXT,source_digest TEXT,
        receipt_id TEXT,digest TEXT,PRIMARY KEY(source_record_id,source_digest));
        CREATE TABLE managed_manifest_history(digest TEXT PRIMARY KEY);''')
    facts=original_facts();manifest=dict(scopes=[SCOPE]);token=authority.digest(manifest)
    conn.execute('INSERT INTO managed_manifest_history VALUES(?)',(token,));conn.commit()
    @contextmanager
    def writable(*_args):
        try:yield conn;conn.commit()
        except BaseException:conn.rollback();raise
    @contextmanager
    def readonly(*_args):
        memory=sqlite3.connect(':memory:');memory.row_factory=sqlite3.Row
        conn.backup(memory)
        try:yield memory
        finally:memory.close()
    def worker(_manifest,_registry,_id,_digest,after):
        value=deepcopy(facts)
        if after:
            value.update(after=1,through=1,high_water=1,rows=[])
        return value
    monkeypatch.setattr(authority,'RECEIPT_DIRECTORY',str(tmp_path))
    monkeypatch.setattr(authority,'_configuration',lambda _: (manifest,b'private-root-fixture-key-'*4))
    monkeypatch.setattr(authority,'_state',writable);monkeypatch.setattr(authority,'_readonly_state',readonly)
    monkeypatch.setattr(authority,'_worker',worker)
    # Windows exercises signing/state bytes, not POSIX authority path permissions.
    monkeypatch.setattr(authority,'_read',lambda path,**_:Path(path).read_bytes())
    monkeypatch.setattr(authority,'_fsync_directory',lambda _:None)
    for flag in ('O_NOFOLLOW','O_CLOEXEC'):
        if not hasattr(authority.os,flag):monkeypatch.setattr(authority.os,flag,0,raising=False)
    registry=dict(generation='c'*64,runtime_uid=1001)
    calls=[]
    def verify_original(rid,rdigest):
        calls.append((rid,rdigest));return activation(facts)
    reference=authority.issue(facts['source_record_id'],facts['source_digest'],
        verified_activation_receipt=verify_original,registry=registry,peer={'uid':1001,'pid':999})
    assert calls==[('1'*64,'2'*64)]
    original=(tmp_path/(reference['receipt_id']+'.json')).read_bytes()
    changes=conn.total_changes
    snapshot=authority.verify(reference['receipt_id'],reference['digest'],registry=registry)['snapshot']
    assert snapshot['status']=='proven' and snapshot['qualification_domain']==DOMAIN
    assert snapshot['global_facts']=={key:None for key in FIELDS}
    assert snapshot['managed_facts']=={key:False for key in FIELDS}
    validate_snapshot(snapshot)
    assert conn.total_changes==changes
    assert (tmp_path/(reference['receipt_id']+'.json')).read_bytes()==original
    # The transport alone is doubled; the endpoint re-reads the actual HMAC
    # receipt and original root SQLite index above. No boolean certifier stub.
    from types import ModuleType
    client=ModuleType('eimemory.core.runtime_manager')
    client.verify_managed_incident_proof=lambda rid,rdigest:authority.verify(rid,rdigest,registry=registry)
    monkeypatch.setitem(sys.modules,'eimemory.core.runtime_manager',client)
    from eimemory.governance.l5.l5_product_completion import build_product_completion
    transaction=dict(transaction_id='consumer-only-fixture',transaction_schema_version=TRANSACTION_SCHEMA,
        incident_id=snapshot['incident_id'],incident_digest=snapshot['incident_digest'],**SCOPE,
        origin='system_detector',detector='eimemory.release_closure_failure.v1',
        **snapshot['global_facts'],managed_incident_proof=snapshot,
        terminal_receipt_digest='9'*64,qualifying_terminal_outcome='succeeded_sedimented',
        evidence_verified=True,observation_valid=True)
    # Remaining gate inputs are explicit consumer fixtures, not actual 8h or
    # deployment evidence. This test makes no production completion claim.
    def completion(**changes):
        return build_product_completion(dict(ok=True,status='ready'),
            provider=dict(ready=True,catalog_ready=True,advertisement_fresh=True),
            transaction={**transaction,**changes},current_lineage=dict(ok=True,compatible=True))
    complete=completion()
    assert complete['schema_version']=='l5_readiness.v5' and complete['qualification_domain']==DOMAIN
    assert complete['product_l5_complete'] is True
    assert complete['global_provenance']=={key:None for key in FIELDS}
    assert transaction['known_before_detection'] is None
    assert not completion(terminal_receipt_digest='')['product_l5_complete']
    assert not completion(observation_valid=False)['product_l5_complete']
    assert not completion(evidence_verified=False)['product_l5_complete']
    assert not completion(qualifying_terminal_outcome='aborted_no_external_effect')['product_l5_complete']
    altered=deepcopy(snapshot);altered['managed_facts']['manual_bootstrap']=True
    altered['status']='adverse';altered['digest']=digest({k:v for k,v in altered.items() if k!='digest'})
    rejected=completion(managed_incident_proof=altered)
    assert rejected['product_l5_complete'] is False and 'managed_incident_proof_unverified' in rejected['gaps']
    legacy=dict(transaction);legacy.pop(SOURCE_KEY)
    old=build_product_completion(dict(ok=True,status='ready'),provider=dict(ready=True,catalog_ready=True,advertisement_fresh=True),
        transaction=legacy,current_lineage=dict(ok=True,compatible=True))
    assert old['schema_version']=='l5_readiness.v4' and old['product_l5_complete'] is False
    late=deepcopy(facts);late.update(after=1,through=2,high_water=2,history=[])
    late['rows'][0].update(sequence=2,record_id='late-original-report',payload_digest='d'*64,
        human_input=1,source='eimemory.code_maintenance',source_time='2026-10-18T00:00:00+00:00')
    monkeypatch.setattr(authority,'_worker',lambda *_:deepcopy(late))
    rechecked=authority.verify(reference['receipt_id'],reference['digest'],registry=registry)
    assert rechecked['snapshot']==snapshot and not rechecked['qualification_current']
    assert rechecked['current_managed_facts']['known_before_detection'] is None
    # Authenticated original source remains usable for an already-authorized
    # repair. Only current managed qualification is withdrawn by the late fact.
    from eimemory.governance.evolution.managed_code_incident_proof import verified_managed_snapshot
    assert verified_managed_snapshot(snapshot)==snapshot
    assert completion()['product_l5_complete'] is False
    assert conn.total_changes==changes and (tmp_path/(reference['receipt_id']+'.json')).read_bytes()==original
    # Replay retains the original signed clock/bytes and does not call a later cause.
    replay=authority.issue(facts['source_record_id'],facts['source_digest'],
        verified_activation_receipt=lambda *_:pytest.fail('must keep original receipt'),
        registry=registry,peer={'uid':1001,'pid':1000})
    assert replay==reference and (tmp_path/(reference['receipt_id']+'.json')).read_bytes()==original
    packet=json.loads(original);packet['payload']['snapshot']['managed_facts']['manual_bootstrap']=True
    mutated=authority.canonical(packet);(tmp_path/(reference['receipt_id']+'.json')).write_bytes(mutated)
    from hashlib import sha256
    with pytest.raises(ValueError,match='signature_invalid'):
        authority.verify(reference['receipt_id'],sha256(mutated).hexdigest(),registry=registry)


def proof_fixture():
    material=dict(schema='managed_code_incident_proof.v1',qualification_domain=DOMAIN,
        reference={'schema':'managed_code_incident_proof_ref.v1','receipt_id':'1'*64,'digest':'2'*64},scope=SCOPE,
        source_record_id='incident-original',source_digest='b'*64,incident_id='incident-original',incident_digest='e'*64,
        defect_key=stable_defect_key('release.closure_internal_failure',['typed_code']),
        defect_key_semantics='conservative_native_symptom_signature.v1',
        activation_reference={'schema':'eimemory.runtime_activation_receipt_ref.v1','receipt_id':'3'*64,'digest':'4'*64},
        manifest_digest='f'*64,history_watermark=1,source_sequence=1,
        managed_facts={key:False for key in FIELDS},global_facts={key:None for key in FIELDS},
        external_knowledge=None,external_user_reports=None,status='proven',reason='')
    return {**material,'digest':digest(material)}


def managed_payload():
    proof=proof_fixture()
    return dict(transaction_id='managed-sqlite',idempotency_key='managed-sqlite',scope=SCOPE,
        origin='system_detector',detector='eimemory.release_closure_failure.v1',
        **{key:None for key in FIELDS},
        incident=dict(incident_id=proof['incident_id'],incident_digest=proof['incident_digest'],
            incident_class='release.closure_internal_failure',diagnostic_codes=['typed_code']),
        repository=dict(repository_root='/fixture/repository',repository_ref='master',base_commit=COMMIT),
        managed_incident_proof=proof,source_evidence={SOURCE_KEY:proof},payload={SOURCE_KEY:proof})


def test_real_sqlite_four_projection_terminal_binding_and_no_global_upgrade(physical):
    from eimemory.storage.code_evolution_store import CodeEvolutionStore,CodeEvolutionStoreError
    ledger=CodeEvolutionStore(physical.store);payload=managed_payload();row=ledger.create_transaction(payload)
    assert row['schema_version']==TRANSACTION_SCHEMA
    assert transaction_snapshot(row)==proof_fixture()
    assert all(row[key] is None for key in FIELDS)
    raw=physical.store.sqlite.conn.execute('SELECT known_before_detection,prior_user_reported,manual_bootstrap FROM code_evolution_transactions').fetchone()
    assert tuple(raw)==(1,1,1)
    terminal=ledger.add_terminal_receipt(row['transaction_id'],dict(outcome='aborted_no_external_effect',evidence_digest='c'*64),terminal_state='ABORTED_NO_EXTERNAL_EFFECT')
    assert json.loads(terminal['payload_json'])[SOURCE_KEY]==proof_fixture()
    assert ledger.get_terminal_receipt(row['transaction_id'])['payload'][SOURCE_KEY]==proof_fixture()
    # Even a recomputed ordinary outer digest cannot remove the root snapshot.
    # Validate the actual read projection independently of immutable SQL guards.
    changed=deepcopy(row);changed['source_evidence'].pop(SOURCE_KEY)
    with pytest.raises(ValueError,match='proof_mismatch'):transaction_snapshot(changed)


@pytest.mark.parametrize('path',[
    'eimemory/contracts/managed_code_incident_proof.py','eimemory/core/managed_incident_proof_authority.py',
    'eimemory/governance/evolution/managed_code_incident_proof.py','eimemory/ops/prepare_managed_incident_authority.py',
    'eimemory/ops/runtime_identity_drift.py','eimemory/ops/release_closure_failure.py',
    'eimemory/governance/l5/l5_product_completion.py','eimemory/governance/l5_product_completion.py',
    'eimemory/governance/l5/native_assessment.py'])
def test_issuer_and_original_source_authority_are_deny_self(path):
    from eimemory.governance.evolution.code_evolution_path_policy import path_denied_by_self
    assert path_denied_by_self(path)


def test_source_inventory_excludes_unsearched_venv_but_detects_package_injection(tmp_path):
    (tmp_path/'package').mkdir();(tmp_path/'package'/'__init__.py').write_bytes(b'')
    site=tmp_path/'.venv'/'lib'/'python3'/'site-packages';site.mkdir(parents=True)
    (site/'unrelated.py').write_text('raise RuntimeError("not imported")')
    if os.name=='posix':os.symlink('lib',tmp_path/'.venv'/'lib64',target_is_directory=True)
    cache=tmp_path/'package'/'__pycache__';cache.mkdir();(cache/'unsearched.py').write_text('unsearched')
    assert authority.python_source_inventory(tmp_path)=={'package/__init__.py'}
    (tmp_path/'package'/'injected.py').write_text('unsafe = True')
    assert authority.python_source_inventory(tmp_path)=={'package/__init__.py','package/injected.py'}


def test_actual_interpreter_existing_pyc_cannot_override_pinned_source_with_empty_prefix(tmp_path):
    source=tmp_path/'source';source.mkdir();path=source/'victim.py'
    original=b'VALUE = "source"\n';forged=b'VALUE = "forged"\n';assert len(original)==len(forged)
    path.write_bytes(original);metadata=path.stat();path.write_bytes(forged)
    os.utime(path,ns=(metadata.st_atime_ns,metadata.st_mtime_ns))
    py_compile.compile(str(path),doraise=True)
    path.write_bytes(original);os.utime(path,ns=(metadata.st_atime_ns,metadata.st_mtime_ns))
    snippet='import sys;sys.path.insert(0,sys.argv[1]);import victim;print(victim.VALUE)'
    base=[sys.executable,'-I','-S','-B']
    # -B alone does read a matching already-existing timestamp .pyc.
    assert subprocess.check_output([*base,'-c',snippet,str(source)]).strip()==b'forged'
    prefix=tmp_path/'empty-root-owned-cache';prefix.mkdir()
    assert subprocess.check_output([*base,'-X','pycache_prefix='+str(prefix),'-c',snippet,str(source)]).strip()==b'source'
    assert list(prefix.iterdir())==[] and path.read_bytes()==original


@pytest.mark.skipif(os.name!='posix',reason='real POSIX descriptor/no-follow/identity boundary')
def test_posix_empty_source_and_changed_then_restored_identity_are_strict(tmp_path,monkeypatch):
    path=tmp_path/'__init__.py';path.write_bytes(b'')
    owners=(0,os.geteuid())
    assert authority._read(path,owners=owners,allow_empty=True)==b''
    with pytest.raises(ValueError,match='file_untrusted'):authority._read(path,owners=owners)
    path.write_bytes(b'value = 1\n')
    manifest=dict(source_directory=str(tmp_path),scopes=[SCOPE],schema_digest='a'*64,
        producer_files={'__init__.py':authority.sha256(path.read_bytes()).hexdigest()})
    registry=dict(runtime_user='darrow',runtime_uid=os.geteuid(),runtime_gid=os.getegid())
    before=authority._producer_guard(manifest,registry)
    path.write_bytes(b'value = 2\n');path.write_bytes(b'value = 1\n')
    after=authority._producer_guard(manifest,registry)
    assert before!=after
    monkeypatch.setattr(authority,'_empty_pycache',lambda:str(tmp_path/'empty'))
    def attack(*_args,**_kwargs):
        saved=path.read_bytes();path.write_bytes(b'bad = True\n');path.write_bytes(saved)
        return SimpleNamespace(returncode=0,stdout=b'{}',stderr=b'')
    monkeypatch.setattr(authority.subprocess,'run',attack)
    with pytest.raises(ValueError,match='producer_identity_changed'):
        authority._worker(manifest,registry,'','',0)


def test_default_second_precision_source_without_first_binding_is_unproven():
    from eimemory.models.records import RecordEnvelope
    envelope=RecordEnvelope.create(kind='incident',title='default precision original',scope=ScopeRef.from_dict(SCOPE))
    facts=original_facts();facts['source_created_at']=envelope.time.created_at
    facts.pop('first_source_evidence')
    value=activation(facts);value['observed_at']=envelope.time.created_at.replace('+00:00','.123456+00:00')
    assert authority._activation_facts(facts,value,{'generation':'c'*64,'runtime_uid':1001},{'uid':1001})==(None,'original_first_physical_source_unproven')


def test_no_incident_checkpoint_accumulates_real_receive_rows_without_root_receipt(physical):
    conn=physical.store.sqlite.conn
    schema={row['name']:row['sql'] for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'trg_code_incident_coverage_%' OR name LIKE 'idx_incident_coverage_%' ORDER BY name")}
    facts=collect_physical_facts(physical.store.sqlite.path,manifest=dict(scopes=[SCOPE],schema_digest=digest(schema)),
        source_record_id='',source_digest='')
    assert facts['incident'] is None and facts['scope'] is None and facts['complete'] is True
    root_index=state();authority._ingest(root_index,facts,dict(scopes=[SCOPE]))
    assert root_index.execute("SELECT value FROM authority_meta WHERE key='high_water'").fetchone()[0]==str(facts['high_water'])
    assert root_index.execute('SELECT count(*) FROM managed_knowledge').fetchone()[0]==0


@pytest.mark.skipif(os.name!='posix' or not hasattr(os,'geteuid') or os.geteuid()!=0,
                    reason='actual root issuer and unprivileged UID worker require native root fixture')
def test_native_root_private_key_actual_uid_worker_and_receipt_readback(tmp_path,physical,monkeypatch):
    """Full isolated FS/UID/SQLite/HMAC path. Timer receipt is a labeled fixture.

    Only the worker's fixed database literal is relocated in a private root-owned
    script, and AST-equivalence checks bind all remaining code. No host authority,
    service, main repository, runtime DB or production incident is touched.
    """
    import ast
    import pwd
    import time
    from eimemory.governance.evolution import managed_code_incident_proof as managed
    account=pwd.getpwnam('darrow')
    # Traversal for the real UID worker, confined to this disposable pytest tree.
    for path in (tmp_path,*tmp_path.parents):
        if path.stat().st_mode&0o111==0o111:break
        path.chmod((path.stat().st_mode&0o777)|0o111)
    fixture_source=tmp_path/'fixture-source';fixture_source.mkdir(mode=0o755)
    repository=Path(__file__).resolve().parents[1]
    for relative in authority.python_source_inventory(repository):
        if not relative.startswith('eimemory/'):continue
        destination=fixture_source/relative;destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes((repository/relative).read_bytes());destination.chmod(0o644)
    site=fixture_source/'.venv'/'lib'/'site-packages';site.mkdir(parents=True)
    (site/'unrelated.py').write_text('raise AssertionError("must not execute candidate venv")')
    os.symlink('lib',fixture_source/'.venv'/'lib64',target_is_directory=True)
    control=tmp_path/'root-authority';control.mkdir(mode=0o700)
    receipts=control/'receipts';receipts.mkdir(mode=0o700)
    cache=receipts/'empty-pycache';cache.mkdir(mode=0o700)
    secret=control/'key';secret.write_bytes(b'isolated-root-private-signing-key-'*4);secret.chmod(0o600)
    monkeypatch.setattr(authority,'DATABASE_PATH',str(physical.store.sqlite.path))
    monkeypatch.setattr(authority,'MANIFEST_PATH',str(control/'manifest.json'))
    monkeypatch.setattr(authority,'KEY_PATH',str(secret));monkeypatch.setattr(authority,'RECEIPT_DIRECTORY',str(receipts))
    monkeypatch.setattr(authority,'PYCACHE_DIRECTORY',str(cache))
    native_source=Path(authority.__file__).read_text(encoding='utf8')
    relocated=native_source.replace("DATABASE_PATH = '/var/lib/eimemory/state/eimemory.sqlite'",
        'DATABASE_PATH = '+repr(str(physical.store.sqlite.path)),1)
    assert native_source!=relocated
    def without_db(source):
        tree=ast.parse(source)
        return ast.dump(ast.Module(body=[item for item in tree.body if not (
            isinstance(item,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='DATABASE_PATH' for target in item.targets))],type_ignores=[]),include_attributes=False)
    assert without_db(native_source)==without_db(relocated)
    worker=tmp_path/'root-reviewed-worker.py';worker.write_text(relocated,encoding='utf8');worker.chmod(0o644)
    monkeypatch.setattr(authority,'__file__',str(worker))
    activation_reference={'schema':'eimemory.runtime_activation_receipt_ref.v1','receipt_id':'1'*64,'digest':'2'*64}
    issued_at=datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(managed,'native_activation_reference',lambda:deepcopy(activation_reference))
    actual=record_release_closure_failure(physical,scope=ScopeRef.from_dict(SCOPE),
        closure_report=dict(report_type='l5_release_closure',ok=False,closure_complete=False,data_accumulating=False,
            blocked_stage='release_lineage',blocked_reason='release_lineage_not_compatible',
            deployment=dict(commit=COMMIT,version='1.14.156'),release_lineage=dict(ok=True,compatible=False,unknown_production_paths=[])),
        detected_at=datetime.now(timezone.utc).isoformat())
    record=physical.store.get_by_id(actual['incident_record_id'],scope=ScopeRef.from_dict(SCOPE))
    # The isolated source is genuinely persisted with native precise first INSERT.
    from eimemory.ops.prepare_managed_incident_authority import prepare_manifest
    manifest=prepare_manifest(database_path=physical.store.sqlite.path,source_directory=fixture_source,
        scopes=[SCOPE],release_commit=COMMIT)
    manifest_path=Path(authority.MANIFEST_PATH);manifest_path.write_bytes(authority.canonical(manifest));manifest_path.chmod(0o640)
    registry=dict(generation='c'*64,runtime_user='darrow',runtime_uid=account.pw_uid,runtime_gid=account.pw_gid,runtime_commit=COMMIT,
        managed_incident_authority=dict(enabled=True,database_path=str(physical.store.sqlite.path),manifest_path=str(manifest_path),
            manifest_sha256=authority.sha256(manifest_path.read_bytes()).hexdigest(),receipt_directory=str(receipts),key_path=str(secret)))
    for path in [physical.store.root,*physical.store.root.rglob('*')]:
        if path.is_symlink():raise AssertionError('fixture runtime has a symlink')
        os.chown(path,account.pw_uid,account.pw_gid)
    preflight=authority.validate_configuration(registry=registry)
    assert preflight['key_material_returned'] is False
    denied=subprocess.run(['/usr/sbin/runuser','-u','darrow','--','/usr/bin/python3','-I','-S','-B','-c',
        'import pathlib,sys;pathlib.Path(sys.argv[1]).read_bytes()',str(secret)],capture_output=True)
    assert denied.returncode!=0
    activation_receipt=dict(schema='eimemory.runtime_activation_attestation.v1',signature_verified=True,
        reference=activation_reference,registry_generation=registry['generation'],scope=SCOPE,
        peer_pid=os.getpid(),peer_uid=account.pw_uid,manager='system',unit='eimemory-learn-watch.service',
        observed_at=issued_at,trigger_kind='scheduled_timer',job_state='running',job_id=42)
    reference=authority.issue(record.record_id,digest(record.to_dict()),
        verified_activation_receipt=lambda *_:deepcopy(activation_receipt),registry=registry,peer={'uid':account.pw_uid,'pid':123})
    original=(receipts/(reference['receipt_id']+'.json')).read_bytes()
    state_before=(receipts/'knowledge.sqlite').read_bytes()
    proof=authority.verify(reference['receipt_id'],reference['digest'],registry=registry)['snapshot']
    assert proof['status']=='proven' and proof['global_facts']=={key:None for key in FIELDS}
    assert proof['managed_facts']=={key:False for key in FIELDS}
    assert (receipts/'knowledge.sqlite').read_bytes()==state_before
    assert (receipts/(reference['receipt_id']+'.json')).read_bytes()==original
    assert list(cache.iterdir())==[]


@pytest.fixture
def physical(tmp_path,monkeypatch):
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY','managed-fixture-only-'+'f'*64)
    store=RuntimeStore(tmp_path/'runtime')
    try:yield SimpleNamespace(store=store)
    finally:store.close()


def test_real_native_source_and_sqlite_receive_prefix_are_read_only(physical):
    report=dict(report_type='l5_release_closure',ok=False,closure_complete=False,data_accumulating=False,
        blocked_stage='release_lineage',blocked_reason='release_lineage_not_compatible',
        deployment=dict(commit=COMMIT,version='1.14.156'),
        release_lineage=dict(ok=True,compatible=False,unknown_production_paths=[]))
    actual=record_release_closure_failure(physical,scope=ScopeRef.from_dict(SCOPE),closure_report=report,
        detected_at=datetime.now(timezone.utc).isoformat())
    record=physical.store.get_by_id(actual['incident_record_id'],scope=ScopeRef.from_dict(SCOPE))
    conn=physical.store.sqlite.conn
    schema={row['name']:row['sql'] for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'trg_code_incident_coverage_%' OR name LIKE 'idx_incident_coverage_%' ORDER BY name")}
    before=conn.total_changes
    facts=collect_physical_facts(physical.store.sqlite.path,manifest=dict(scopes=[SCOPE],schema_digest=digest(schema)),
        source_record_id=record.record_id,source_digest=digest(record.to_dict()))
    assert facts['complete'] is True and facts['global_facts']=={key:None for key in FIELDS}
    assert facts['source_digest']==digest(record.to_dict()) and conn.total_changes==before
    registry=state();authority._ingest(registry,facts,dict(scopes=[SCOPE]))
    known,reported,sequence,reason=authority._known(registry,facts)
    assert (known,reported,reason)==(False,False,'') and sequence>0
