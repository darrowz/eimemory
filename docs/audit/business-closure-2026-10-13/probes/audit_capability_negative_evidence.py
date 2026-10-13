import sys, json, tempfile
from pathlib import Path
sys.path[:0] = ['/workspace/eimemory','/workspace/eimemory/tests']
import test_dynamic_capability_evals as fixture
from eimemory.api.runtime import Runtime
from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog
from eimemory.capabilities.projector import CapabilityStateProjector
from datetime import datetime, timezone

with tempfile.TemporaryDirectory(prefix='audit-negative-evidence-') as temp:
    runtime = Runtime.create(root=Path(temp))
    try:
        definition = fixture._definition()
        revision = fixture._revision(definition)
        binding = fixture._binding(definition,revision)
        profile = fixture._profile(definition)
        for method, item in [(runtime.capabilities.register_definition,definition),
                (runtime.capabilities.register_revision,revision), (runtime.capabilities.bind,binding),
                (runtime.capabilities.register_profile,profile)]:
            method(item,runtime_scope=fixture.SCOPE)
        state = {'passing':True}
        catalog = CapabilityEvaluationCatalog()
        catalog.register_executor(executor_id='eimemory.eval.dynamic-catalog',revision='v1',
            handler=lambda _input,_fixture,_runtime: {'decision':'traceable' if state['passing'] else 'wrong',
                                                    'evidence_count':1})
        for case in fixture._catalog().list_cases(): catalog.register_case(case)
        reports=[]
        for passing in (True,False):
            state['passing']=passing
            report=runtime.run_capability_acceptance(scope=fixture.SCOPE,persist=True,catalog=catalog,
                profile_key=profile.profile_key,capability_scope='global',runtime_scope=fixture.SCOPE)
            reports.append({'expected_passing':passing,'ok':report['ok'],
                'results':[{key:row.get(key) for key in ('case_id','passed','error','trace_record_id','evaluation_run_id')}
                    for row in report['results']]})
        projection=CapabilityStateProjector(runtime.store).project(profile.profile_key,runtime_scope=fixture.SCOPE,
            capability_scope='global',at_time=datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),persist=False).to_dict()
        print(json.dumps({'reports':reports,'projection_blocked':projection.get('blocked'),
            'snapshots':projection.get('snapshots'), 'actual_external_effects':False},sort_keys=True))
    finally: runtime.close()
