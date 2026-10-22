"""The final native child verifies facts independently of worker status flags."""
from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='native user release validation')


@pytest.fixture
def finalization(tmp_path, monkeypatch):
    from eimemory.core import runtime_manager as manager
    from eimemory.governance.release import evidence_contract as evidence
    from eimemory.governance.release import release_binding_refresh as bindings
    from deploy import refresh_release_scope_bindings as process
    from deploy import clean_release_bytecode as source
    monkeypatch.delenv('EIMEMORY_CONTROL_PLANE', raising=False)
    path = Path(__file__).parents[1] / 'deploy/run_code_deployment_validation.py'
    spec = importlib.util.spec_from_file_location('worker_native_finalization_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    commit = 'a' * 40
    releases = tmp_path / 'releases'
    target = releases / commit
    (target / 'eimemory').mkdir(parents=True)
    current = tmp_path / 'current'
    current.symlink_to(target)
    config = tmp_path / 'config'
    config.mkdir()
    settings = config / 'settings.json'
    settings.write_text(json.dumps({'trusted_repository_root': str(tmp_path / 'repo')}))
    settings.chmod(0o600)
    binding = config / 'release-scope-bindings.json'
    scope = dict(tenant_id='default', agent_id='hongrui', workspace_id='embodied', user_id='darrow')
    record = SimpleNamespace(to_dict=lambda: {'record_id': 'receipt-current', 'commit': commit})
    digest = sha256(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    entries = [dict(scope=scope, receipt_id='receipt-current', receipt_sha256=digest)]
    binding.write_text(json.dumps(entries))
    binding.chmod(0o600)
    env = dict(EIMEMORY_CONFIG_DIR=str(config), EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE=str(binding),
        EIMEMORY_RUNTIME_COMMIT=commit)
    identity = SimpleNamespace(commit=commit, receipt_id='receipt-current')
    monkeypatch.setattr(module, '_WORKER_CURRENT_LINK', current)
    monkeypatch.setattr(module, '_WORKER_RELEASES_ROOT', releases)
    monkeypatch.setattr(module, '_WORKER_CONFIG_DIR', config)
    monkeypatch.setattr(module.os, 'statvfs', lambda _: SimpleNamespace(f_flag=os.ST_RDONLY))
    monkeypatch.setattr(manager, 'read_registry', lambda: {'repository_root': str(tmp_path / 'repo')})
    monkeypatch.setattr(evidence, 'current_deployment_receipt_for_owner', lambda *a, **kw: identity)
    monkeypatch.setattr(evidence, 'deployment_receipt_for_scope', lambda *a, **kw: record)
    monkeypatch.setattr(evidence, 'verified_deployment_receipt_identity', lambda _: identity)
    monkeypatch.setattr(evidence, 'same_release_authority', lambda a, b: a == b)
    monkeypatch.setattr(bindings, 'refresh_bindings', lambda *a: len(entries))
    monkeypatch.setattr(process, 'rpc_process_environment', lambda _: dict(env))
    monkeypatch.setattr(source, 'validate_release_source', lambda **kw: None)
    body = dict(manual_release=True, target_commit=commit, candidate_commit=commit, scope=scope)
    report = dict(ok=True, business_qualified=True, l5_certification='not_certified',
        record_ids={'deployment_receipt': 'receipt-current'})
    return SimpleNamespace(module=module, body=body, report=report, entries=entries,
        binding=binding, settings=settings, env=env, evidence=evidence, source=source,
        process=process, current=current, target=target)


def run(value):
    return value.module.verify_worker_finalization(object(), body=value.body, report=value.report)


def test_native_finalization_verifies_current_source_settings_and_physical_pins(finalization):
    result = run(finalization)
    assert result['schema'] == 'managed_worker_finalization.v1'
    assert result['ok'] is result['source_verified'] is result['source_readonly'] is result['bytecode_clean'] is True
    assert result['binding_count'] == 1 and result['receipt_id'] == 'receipt-current'
    assert result['l5_credit'] is False
    assert result['binding_digest'] == sha256(finalization.binding.read_bytes()).hexdigest()
    assert result['settings_digest'] == sha256(finalization.settings.read_bytes()).hexdigest()


@pytest.mark.parametrize('fault', ['receipt', 'binding_receipt', 'binding_digest', 'settings', 'missing_binding', 'source', 'source_rw'])
def test_native_finalization_rejects_unverified_worker_claims(finalization, monkeypatch, fault):
    value = finalization
    if fault == 'receipt':
        monkeypatch.setattr(value.evidence, 'current_deployment_receipt_for_owner', lambda *a, **kw: None)
    elif fault == 'binding_receipt':
        value.entries[0]['receipt_id'] = 'other-receipt'
        value.binding.write_text(json.dumps(value.entries))
    elif fault == 'binding_digest':
        value.entries[0]['receipt_sha256'] = '0' * 64
        value.binding.write_text(json.dumps(value.entries))
    elif fault == 'settings':
        value.settings.write_text(json.dumps({'trusted_repository_root': 'other'}))
    elif fault == 'missing_binding':
        value.env.pop('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE')
    elif fault == 'source':
        def refuse(**kw):
            raise ValueError('changed_source_or_bytecode')
        monkeypatch.setattr(value.source, 'validate_release_source', refuse)
    else:
        monkeypatch.setattr(value.module.os, 'statvfs', lambda _: SimpleNamespace(f_flag=0))
    with pytest.raises(ValueError):
        run(value)


def test_native_finalization_detects_process_configuration_change(finalization, monkeypatch):
    calls = 0
    def observed(_):
        nonlocal calls
        calls += 1
        return {**finalization.env, 'EIMEMORY_RUNTIME_COMMIT': 'b' * 40} if calls > 1 else dict(finalization.env)
    monkeypatch.setattr(finalization.process, 'rpc_process_environment', observed)
    with pytest.raises(ValueError, match='changed_during_verification'):
        run(finalization)


def test_native_finalization_cannot_create_autonomous_credit(finalization):
    finalization.body['manual_release'] = False
    with pytest.raises(ValueError, match='manual_business_required'):
        run(finalization)
