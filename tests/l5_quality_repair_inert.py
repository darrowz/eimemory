"""AST-selected data helpers and inert list-call spies; no application imports."""
import ast, copy, json, hashlib
from pathlib import Path
from types import SimpleNamespace as NS
from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime, timezone
BASE=Path('/workspace/shared/eimemory-validated-phase29-memory-readers-d555-20261002/eimemory/governance/l5/l5_reader.py')
HERE=Path(__file__).parent
CAND=HERE/'overlay/eimemory/governance/l5/l5_reader.py'
SOURCE='eimemory.l5.quality_gap_intake'
STAMP='2026-10-02T01:00:00+00:00'
LATER='2026-10-02T02:00:00+00:00'
LATEST='2026-10-02T03:00:00+00:00'
@dataclass
class Scope:
 tenant_id:str='t';agent_id:str='a';workspace_id:str='w';user_id:str='owner'
 @classmethod
 def from_dict(cls,value): return cls(**value)
SCOPE=Scope()
class Strip(ast.NodeTransformer):
 def visit_Import(self,node): return None
 def visit_ImportFrom(self,node): return None
def load(path):
 tree=ast.parse(path.read_text());names={'_quality_repair_transaction','_current_quality_resolution','_quality_observation_time','_select_code_evolution_rows'}
 nodes=[Strip().visit(n) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
 mod=ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[]))
 ns={'Any':object,'Mapping':Mapping,'ScopeRef':Scope,'sha256':hashlib.sha256,'QUALITY_GAP_SOURCE':SOURCE,'datetime':datetime,'timezone':timezone}
 exec(compile(mod,str(path),'exec'),ns)
 return ns
OLD,NEW=load(BASE),load(CAND)
def gap(rid='g',parent='',key='quality-gap:test:cap',when=STAMP,scope=None,source_id='observation-source'):
 return NS(record_id=rid,kind='reflection',source=SOURCE,source_id=source_id,status='active',scope=scope or SCOPE,time=NS(occurred_at=when),meta={'schema':'eimemory.quality_gap.v1','report_type':'quality_gap','semantic_key':key,'target_capability':'cap','supersedes_gap_id':parent},content={'schema':'eimemory.quality_gap.v1','semantic_key':key,'target_capability':'cap',**({'supersedes_gap_id':parent} if parent else {})})
def resolution(rid='r',parent='g',key='quality-gap:test:cap',when=LATER,scope=None):
 return NS(record_id=rid,kind='reflection',source=SOURCE,source_id='default',status='resolved',scope=scope or SCOPE,time=NS(occurred_at=when),meta={'schema':'eimemory.quality_gap.v1','report_type':'quality_gap_resolution','semantic_key':key,'target_capability':'cap','resolves_gap_id':parent,'report_digest':'d'*64,'resolved_at':when,'release_commit':'b'*40,'deployment_receipt_id':'receipt'},content={'schema':'eimemory.quality_gap.v1','semantic_key':key,'target_capability':'cap','resolves_gap_id':parent,'resolution':{'status':'passed','report_digest':'d'*64,'observed_at':when}})
class Store:
 def __init__(self,records): self.records=records;self.calls=[]
 def list_records(self,**kw): self.calls.append(kw);return self.records
RESULTS=[]
def run(ns,records):
 store=Store(records);result=ns['_quality_repair_transaction'](NS(store=store),runtime_scope=SCOPE)
 return result,store.calls

def check(name,records,expected,old_expected=None):
 result,calls=run(NEW,records)
 actual=None if result is None else result.get('evidence_verified')
 assert actual==expected,(name,actual,expected)
 assert calls==[{'kinds':['reflection'],'scope':SCOPE,'limit':501}]
 row={'name':name,'candidate':actual,'passed':True}
 if old_expected is not None:
  original,_=run(OLD,records);original_actual=None if original is None else original.get('evidence_verified')
  assert original_actual==old_expected,(name,original_actual,old_expected)
  row['original']=original_actual
 RESULTS.append(row)
 return result

check('exact_owner_positive_and_different_producer_source_ids',[resolution(),gap()],False,True)
check('missing_predecessor_defect_control',[resolution()],None,True)
check('shared_owner_defect_control',[resolution(scope=Scope(user_id='')),gap(scope=Scope(user_id=''))],None,True)
check('neighbor_channel_not_repair_authority',[resolution(scope=Scope(workspace_id='other')),gap(scope=Scope(workspace_id='other'))],None,True)
check('newer_reopen_defect_control',[gap('g2','r',when=LATEST),resolution(),gap()],None,True)
check('equal_time_reopen_defect_control',[gap('g2','r',when=LATER),resolution(),gap()],None,True)
check('reopened_chain_order_independent',[gap(),resolution(),gap('g2','r',when=LATEST)],None,True)
check('resolve_reopened_chain',[resolution('r2','g2',when=LATEST),gap('g2','r',when=LATER),resolution(),gap()],False,True)
check('unrelated_active_gap_is_not_global_veto',[gap('other',key='quality-gap:other:cap',when=LATEST),resolution(),gap()],False,True)
wrong=resolution();wrong.source='different-producer'
check('wrong_source_cannot_supply_resolution',[wrong,gap()],None)
wrong=resolution();wrong.kind='memory'
check('wrong_kind_cannot_supply_resolution',[wrong,gap()],None)
wrong=resolution();wrong.status='archived'
check('archived_resolution_is_not_current_credit',[wrong,gap()],None)
wrong=resolution();wrong.content['resolves_gap_id']='different'
check('conflicting_parent_reference',[wrong,gap()],None,True)
wrong=resolution();wrong.content['resolution']['status']='failed'
check('content_failed_is_not_meta_resolved',[wrong,gap()],None,True)
wrong=resolution();wrong.meta.pop('deployment_receipt_id')
result=check('valid_chain_unbound_diagnostic',[wrong,gap()],False,False)
assert result['evidence_error']=='quality_repair_release_unbound'
wrong=resolution();wrong.meta['schema']='other'
check('wrong_schema',[wrong,gap()],None,True)
wrong=resolution();wrong.content['semantic_key']='different'
check('semantic_contradiction',[wrong,gap()],None,True)
check('cycle_cannot_qualify',[resolution(),gap(parent='r')],None,True)
check('competing_resolved_tips',[resolution('r2'),resolution(),gap()],None,True)
check('backwards_observation_time',[resolution(when=STAMP),gap(when=LATER)],None,True)
wrong=resolution();wrong.meta['resolved_at']='not-a-time'
check('malformed_observation_time',[wrong,gap()],None,True)
wrong=resolution();wrong.meta['resolved_at']='2026-10-02T02:00:00';wrong.content['resolution']['observed_at']=wrong.meta['resolved_at']
check('naive_observation_time',[wrong,gap()],None,True)
wrong=resolution();wrong.content['resolution']['observed_at']=LATEST
check('observed_time_conflict',[wrong,gap()],None,True)
shared=resolution(scope=Scope(user_id=''));shared.meta['release_commit']='c'*40
result=check('shared_same_id_does_not_shadow_exact_owner',[shared,resolution(),gap()],False,True)
assert result['release_commit']=='b'*40
assert run(OLD,[shared,resolution(),gap()])[0]['release_commit']=='c'*40
repeated=resolution();repeated.source_id='other-source'
check('same_owner_same_id_ambiguous_source_partition',[repeated,resolution(),gap()],None,True)
filler=NS(record_id='irrelevant',kind='reflection',source='other',scope=SCOPE)
check('complete_500_row_window',[resolution(),gap()]+[filler]*498,False,True)
check('501_sentinel_refuses_incomplete_window',[resolution(),gap()]+[filler]*499,None,True)
check('missing_record_list_returns_no_evidence',None,None)
class Ledger:
 def get_quarantine_resolution(self,transaction_id): return None
for label,rows,expected in [('active',[{'transaction_id':'active','terminal':False},{'transaction_id':'old','terminal':True,'current_state':'SUCCEEDED_SEDIMENTED'}],({'transaction_id':'active','terminal':False},None)),('quarantine',[{'transaction_id':'q','terminal':True,'current_state':'RECOVERY_QUARANTINED'},{'transaction_id':'old','terminal':True,'current_state':'SUCCEEDED_SEDIMENTED'}],(None,{'transaction_id':'q','terminal':True,'current_state':'RECOVERY_QUARANTINED'}))]:
 assert NEW['_select_code_evolution_rows'](Ledger(),rows)==expected
 RESULTS.append({'name':'unchanged_'+label+'_precedence','passed':True})
# Static call contract: no application execution. Repair owner changes only here.
tree=ast.parse(CAND.read_text());owner=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_code_evolution_evidence')
repair_call=next(n for n in ast.walk(owner) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='_quality_repair_transaction')
assert ast.unparse(next(k.value for k in repair_call.keywords if k.arg=='runtime_scope'))=='evidence_scope if evidence_scope is not None else runtime_scope'
RESULTS.append({'name':'repair_call_uses_report_owner_static_contract','passed':True})
# A current chain establishes diagnostics only: its producer has no prior-knowledge authority.
tx,_=run(NEW,[resolution(),gap()])
for field in ('known_before_detection','prior_user_reported','manual_bootstrap','observation_valid'):
 assert tx[field] is None
 RESULTS.append({'name':'unproven_'+field+'_remains_none','passed':True})
assert tx['evidence_verified'] is False
assert tx['evidence_error']=='quality_repair_provenance_unproven'
assert tx['evidence_gaps']==['quality_repair_provenance_unproven']
RESULTS.append({'name':'current_release_shape_does_not_verify_incident_provenance','passed':True})
product_path=BASE.parent/'l5_product_completion.py'
product_tree=ast.parse(product_path.read_text())
names={'_mapping','_bool','_explicit_false','_sha256_digest','_adapter_ready','_deployment_status','build_product_completion'}
nodes=[Strip().visit(n) for n in product_tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
mod=ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[]))
product={'Mapping':Mapping,'QUALIFYING_OUTCOMES':frozenset({'succeeded_sedimented','rolled_back_healthy','quality_repaired'})}
exec(compile(mod,str(product_path),'exec'),product)
assess={'ok':True,'status':'ready','adapter_readiness':{'a':'ready'},'deployment_assurance':{'required':False,'ok':None},'loop_maturity':'evolving'}
provider={'provider_ready':True,'catalog_ready':True,'advertisement_fresh':True}
lineage={'ok':True,'compatible':True}
def classify(transaction): return product['build_product_completion'](assess,provider=provider,transaction=transaction,current_lineage=lineage)
closed=classify(tx)
assert closed['product_l5_complete'] is False
assert set(['transaction_evidence_unverified','incident_prior_knowledge_unproven','incident_not_user_reported_unproven','observation_not_valid']).issubset(closed['gaps'])
assert tx['known_before_detection'] is None and tx['prior_user_reported'] is None
RESULTS.append({'name':'downstream_none_never_becomes_qualifying_false','passed':True,'gaps':closed['gaps']})
# Existing independent ordinary transaction contract remains positively usable as a scalar fixture.
ordinary={'transaction_id':'ordinary','terminal_receipt_digest':'e'*64,'outcome':'succeeded_sedimented','evidence_verified':True,'origin':'system_detector','known_before_detection':False,'prior_user_reported':False,'manual_bootstrap':False,'observation_valid':True}
assert classify(ordinary)['product_l5_complete'] is True
RESULTS.append({'name':'independent_ordinary_transaction_scalar_positive_control','passed':True})
for pchange in [{'provider_ready':False},{'catalog_ready':False},{'advertisement_fresh':False}]:
 closed=product['build_product_completion'](assess,provider={**provider,**pchange},transaction=ordinary,current_lineage=lineage)
 assert closed['product_l5_complete'] is False
 RESULTS.append({'name':'preserved_gate_'+next(iter(pchange)),'passed':True})
for assessment,lineage_value,label in [({**assess,'ok':False},lineage,'control'),(assess,{'ok':True,'compatible':False},'lineage')]:
 assert product['build_product_completion'](assessment,provider=provider,transaction=ordinary,current_lineage=lineage_value)['product_l5_complete'] is False
 RESULTS.append({'name':'preserved_gate_'+label,'passed':True})
reader_source=CAND.read_text()
assert 'transaction.update(quality_tx)' in reader_source
assert 'envelope["transaction_evidence"] = dict(transaction)' in reader_source
RESULTS.append({'name':'reader_update_and_envelope_preserve_none_static_contract','passed':True})

report={'scope':'Finite AST data helpers + inert list/ledger spies; source imports stripped; no real Runtime, L5 engine, provider, evaluator, replay, hooks, storage or real data','count':len(RESULTS),'all_passed':True,'results':RESULTS,'baseline_sha256':hashlib.sha256(BASE.read_bytes()).hexdigest(),'candidate_sha256':hashlib.sha256(CAND.read_bytes()).hexdigest()}
(HERE/'test-results.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'batch':'A','count':len(RESULTS),'all_passed':True}))
