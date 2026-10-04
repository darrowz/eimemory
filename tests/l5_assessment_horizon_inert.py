"""Static caller inspection, scalar clock expression and inert loop call spy only."""
import ast,json,hashlib
from pathlib import Path
from collections.abc import Mapping
HERE=Path(__file__).parent
BASE=Path('/workspace/shared/eimemory-validated-phase29-memory-readers-d555-20261002/eimemory/governance/l5/l5_assessment_v3.py')
CAND=HERE/'overlay/eimemory/governance/l5/l5_assessment_v3.py'
RESULTS=[]
def record(name,actual,expected):
 assert actual==expected,(name,actual,expected)
 RESULTS.append({'name':name,'actual':actual,'expected':expected,'passed':True})
def fn(path,name): return next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.FunctionDef) and n.name==name)
def callee(call):
 return call.func.id if isinstance(call.func,ast.Name) else call.func.attr if isinstance(call.func,ast.Attribute) else ''
def kw(call,key): return next((ast.unparse(k.value) for k in call.keywords if k.arg==key),None)
old_build=fn(BASE,'build_l5_assessment_v3');new_build=fn(CAND,'build_l5_assessment_v3')
for name in ['project','_adapter_readiness','_loop_maturity']:
 calls=[n for n in ast.walk(new_build) if isinstance(n,ast.Call) and callee(n)==name]
 record(name+'_same_horizon', [kw(n,'at_time') for n in calls], ['assessment_at_time'])
old_loop=next(n for n in ast.walk(old_build) if isinstance(n,ast.Call) and callee(n)=='_loop_maturity')
record('original_defect_missing_loop_horizon',kw(old_loop,'at_time'),None)
assignment=next(n for n in ast.walk(new_build) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='assessment_at_time' for t in n.targets))
record('one_clock_assignment',ast.unparse(assignment.value),'at_time or now_iso()')
for value,expected_count in [('2026-09-01T00:00:00+00:00',0),('',1)]:
 ticks=[]
 def clock(): ticks.append('tick');return '2026-10-03T00:00:00+00:00'
 expression=ast.fix_missing_locations(ast.Expression(body=assignment.value))
 actual=eval(compile(expression,'pure-horizon-expression','eval'),{'__builtins__':{},'at_time':value,'now_iso':clock})
 record('explicit' if value else 'default_horizon',actual,value or '2026-10-03T00:00:00+00:00')
 record('explicit_clock_count' if value else 'default_clock_count',len(ticks),expected_count)
class Strip(ast.NodeTransformer):
 def visit_Import(self,n): return None
 def visit_ImportFrom(self,n): return None
def wrapper(path):
 node=Strip().visit(fn(path,'_loop_maturity'))
 mod=ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),node],type_ignores=[]))
 calls=[]
 def spy(*args,**kwargs): calls.append(kwargs);return 'sentinel'
 ns={'loop_maturity':spy}
 exec(compile(mod,str(path),'exec'),ns)
 return ns['_loop_maturity'],calls
old,old_calls=wrapper(BASE);new,new_calls=wrapper(CAND)
record('old_wrapper_inert_result',old(object(),object(),'global',{}),'sentinel')
record('old_wrapper_omits_time',old_calls,[{}])
record('new_wrapper_inert_result',new(object(),object(),'global',{},at_time='2026-09-01T00:00:00+00:00'),'sentinel')
record('new_wrapper_passes_time',new_calls,[{'at_time':'2026-09-01T00:00:00+00:00'}])
# Static proof the callee filters are existing code, not changed by this overlay.
loop=Path('/workspace/shared/eimemory-validated-phase29-memory-readers-d555-20261002/eimemory/governance/l5/l5_loop_evidence.py').read_text()
for text in ['finished > horizon','created > horizon','computed <= horizon','old[1] and passed','_current_link(store, scope, row, payload)']:
 record('preserved_filter_'+text,text in loop,True)
# Explicit hand model of the unchanged cutoff predicates, not execution of loop_maturity.
from datetime import datetime
horizon=datetime.fromisoformat('2026-09-01T00:00:00+00:00')
before=datetime.fromisoformat('2026-08-31T23:59:59+00:00');after=datetime.fromisoformat('2026-09-15T00:00:00+00:00')
record('historical_evaluation_cutoff_model',[t<=horizon for t in [before,horizon,after]],[True,True,False])
record('historical_link_cutoff_model',[t<=horizon for t in [before,horizon,after]],[True,True,False])
record('historical_snapshot_cutoff_model',[t<=horizon for t in [before,horizon,after]],[True,True,False])
# Deployment axis has intentionally not been given historical authority.
old_deploy=fn(BASE,'_deployment_assurance');new_deploy=fn(CAND,'_deployment_assurance')
record('deployment_authority_unchanged',ast.dump(old_deploy)==ast.dump(new_deploy),True)
report={'scope':'Static AST contracts, pure scalar expression, inert wrapper spy, explicitly labeled hand cutoff models; no real assessment, loop, Runtime, projection, provider, evaluator, deployment or source verification execution','count':len(RESULTS),'all_passed':True,'results':RESULTS,'baseline_sha256':hashlib.sha256(BASE.read_bytes()).hexdigest(),'candidate_sha256':hashlib.sha256(CAND.read_bytes()).hexdigest()}
(HERE/'test-results.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'batch':'B','count':len(RESULTS),'all_passed':True}))
