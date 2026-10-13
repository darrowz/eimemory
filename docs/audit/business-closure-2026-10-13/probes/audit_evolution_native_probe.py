import sys, json, tempfile
from pathlib import Path
from dataclasses import asdict
from types import SimpleNamespace
sys.path[:0] = ['/workspace/eimemory','/workspace/eimemory/tests']
from pytest import MonkeyPatch
import test_dynamic_code_patch_context as fixture
from eimemory.governance.evolution import dynamic_capability_evolution as evolution
from eimemory.governance.evolution import autonomous_evolution as autonomous
from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog
from eimemory.governance.capability.capability_acceptance import run_capability_acceptance

with tempfile.TemporaryDirectory(prefix='audit-evolution-') as temp, MonkeyPatch.context() as mp:
    generator = fixture.native_gap.__wrapped__(Path(temp), mp)
    native = next(generator)
    try:
        from eimemory.contracts.release_identity import ReleaseIdentity
        mp.setattr('eimemory.governance.release.evidence_contract.current_release_identity', lambda *a, **kw: ReleaseIdentity(native.base, 'audit-fixture', 'audit-receipt', 'audit-session'))
        proposal, edge = fixture.proposal(native, mp)
        # Exercise the real proposal -> experiment -> candidate -> promotion
        # return shape. Only the actual external-effects call is inert here.
        from eimemory.governance.evolution import code_evolution_effects as effects
        mp.setattr(effects, 'execute_code_evolution_effects',
            lambda *a, **kw: {'ok': True, 'applied': True, 'transaction_id': proposal['transaction_id']})
        patch = {'opportunity_id':'dynamic-capability-' + native.item['work_item_id'],
                 'summary':'Native contract audit', 'code_evolution_proposal':proposal}
        result = autonomous._apply_code_evolution_v2(native.runtime, patch, scope=asdict(fixture.SCOPE), apply=True)
        feedback = evolution._record_dynamic_candidate_feedback(native.runtime, scope=fixture.SCOPE,
                    item=native.item, evolution={'applied_patches':[result], 'blocked_patches':[]})
        blocked_feedback = evolution._record_dynamic_candidate_feedback(native.runtime, scope=fixture.SCOPE, item=native.item, evolution={'applied_patches':[], 'blocked_patches':[{**result, 'applied':False}]})
        print(json.dumps({'blocked_branch_feedback':blocked_feedback,'probe':'strict_v2_feedback_contract',
            'candidate_created':bool(result.get('candidate_id')),
            'applied':result.get('applied'),'isolated_evaluator_field_present':'isolated_evaluator' in result,
            'feedback':feedback, 'actual_external_effects':False}, sort_keys=True))
    finally:
        generator.close()

