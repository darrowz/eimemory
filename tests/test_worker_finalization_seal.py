"""Manual completion must include the final child's independently sealed facts."""
from copy import deepcopy

import pytest

from eimemory.contracts import runtime_manager as contract
from test_runtime_manager import validation_fixture


@pytest.mark.parametrize('fault', [None, 'absent', 'parent_status', 'receipt', 'commit',
    'repository', 'bindings_absent', 'binding_count', 'binding_bool', 'binding_digest',
    'settings_digest', 'source', 'readonly', 'bytecode', 'l5_credit'])
def test_manual_completion_rejects_unbound_or_unverified_finalization(monkeypatch, tmp_path, fault):
    module, bridge, registry, request, key, parent, *_ = validation_fixture(monkeypatch, tmp_path)
    parent['manual_release'] = True
    commit = request['target_commit']
    final = dict(schema='managed_worker_finalization.v1', ok=True, commit=commit,
        repository_root=registry['repository_root'], receipt_id='rec_current', binding_configured=True,
        binding_count=2, binding_digest='1' * 64, settings_digest='2' * 64,
        source_verified=True, source_readonly=True, bytecode_clean=True, l5_credit=False)
    report = dict(ok=True, report_type='controlled_production_release_closure', business_qualified=True,
        closure_complete=True, l5_certification='not_certified', observation_started=False,
        record_ids={'deployment_receipt': 'rec_current'}, worker_finalization=final)
    if fault == 'absent': report.pop('worker_finalization')
    elif fault == 'parent_status': report['worker_finalization'] = dict(ok=True, installer_exit_code=0)
    elif fault == 'receipt': final['receipt_id'] = 'rec_prior'
    elif fault == 'commit': final['commit'] = 'c' * 40
    elif fault == 'repository': final['repository_root'] = '/untrusted'
    elif fault == 'bindings_absent': final['binding_configured'] = False
    elif fault == 'binding_count': final['binding_count'] = 0
    elif fault == 'binding_bool': final['binding_count'] = True
    elif fault == 'binding_digest': final['binding_digest'] = 'parent_reported'
    elif fault == 'settings_digest': final['settings_digest'] = '0' * 63
    elif fault == 'source': final['source_verified'] = False
    elif fault == 'readonly': final['source_readonly'] = False
    elif fault == 'bytecode': final['bytecode_clean'] = False
    elif fault == 'l5_credit': final['l5_credit'] = True
    artifact = dict(schema='managed_code_deployment_validation.v2', job_key=key, operation='post_resume',
        parent_invocation='a' * 32, invocation='b' * 32, target_commit=commit,
        controller_commit=request['controller_commit'], scope=deepcopy(request['scope']), source_readonly=True,
        manual_release=True, launch_controller_commit=request['controller_commit'], report=report,
        target_transport=dict(exit_status=0, timed_out=False, stdout_digest='3' * 64, stderr_digest='4' * 64,
            executed=True, physical_receipt_readback=None, closure_capture=dict(capture_saved=True,
                recording_ok=True, exit_code=0, capture=dict(scope=request['scope'], expected_commit=commit))))
    proof = dict(terminal_state=dict(LoadState='loaded', InvocationID='b' * 32, Result='success', ExecMainStatus='0'),
        original_invocation='b' * 32, parent_original_invocation='a' * 32,
        sealed_artifact=True, target_commit=commit)
    monkeypatch.setattr(bridge, 'validation_status', lambda *a: proof)
    monkeypatch.setattr(module, 'read_file', lambda *a, **k: b'{}')
    monkeypatch.setattr(bridge, 'validation_seal', lambda _r, _k, op, _v:
        {'raw_hex': contract.canonical(artifact if op == 'post_resume' else
            {'report': {'promotion_request_id': 'rec_current'}}).hex()})
    if fault:
        with pytest.raises(ValueError, match='worker_finalization'):
            bridge.verify_completed_validation(registry, parent, 'post_resume', commit)
    else:
        bridge.verify_completed_validation(registry, parent, 'post_resume', commit)


def test_manual_policy_retirement_is_outside_autonomous_patch_authority():
    from eimemory.governance.evolution.code_evolution_path_policy import path_denied_by_self
    assert path_denied_by_self('deploy/retire_manual_code_policy.py')
    assert path_denied_by_self('eimemory/evaluation/controlled_acceptance_window.py')
