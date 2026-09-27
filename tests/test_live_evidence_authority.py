"""Run in the full project: isolate authority guards, not independent receipt verification."""
import os
from types import SimpleNamespace

import pytest


def evidence_case(monkeypatch):
    from eimemory.governance.l5 import live_task_acceptance as live
    from eimemory.models.records import ScopeRef
    scope = ScopeRef(tenant_id='authority-test', agent_id='a', workspace_id='w', user_id='u')
    receipt = SimpleNamespace(scope=scope)
    runtime = SimpleNamespace(store=SimpleNamespace(get_by_id=lambda *_args, **_kwargs: receipt))
    case_id = live.LIVE_ACCEPTANCE_CASE_IDS[0]
    commit, digest = 'a' * 40, 'b' * 64
    trace = f'live-acceptance:{commit}:{case_id}:{digest[:12]}'
    payload = dict(report_type=live.CASE_REPORT_TYPE, schema_version=live.SCHEMA_VERSION,
                   evidence_class=live.EVIDENCE_CLASS, case_id=case_id, task_type=live.live_acceptance_task_type(case_id),
                   deployment_commit=commit, release_path='/release/' + commit, promotion_request_id='receipt',
                   release_session_id='session', passed=True, observation_digest=digest, trace_id=trace)
    record = SimpleNamespace(scope=scope, status='active', kind='learning_eval',
                             source='eimemory.live_task_acceptance', content=payload, meta={})
    monkeypatch.setattr(live, '_valid_deployment_receipt', lambda *_args, **_kwargs: True)
    def check():
        return live.validate_live_acceptance_case(runtime, scope=scope, evidence=record, case_id=case_id,
            task_type=payload['task_type'], trace_id=trace, deployment_commit=commit, passed=True)
    assert check(), 'positive control must pass before mutating the isolated authority boundary'
    return live, scope, receipt, record, check


def test_shared_visible_live_case_cannot_be_reused_as_private_evidence(monkeypatch):
    from dataclasses import replace
    _live, scope, _receipt, record, check = evidence_case(monkeypatch)
    record.scope = replace(scope, user_id='')
    assert not check()


def test_shared_visible_receipt_cannot_authorize_private_live_case(monkeypatch):
    from dataclasses import replace
    _live, scope, receipt, _record, check = evidence_case(monkeypatch)
    receipt.scope = replace(scope, user_id='')
    assert not check()


@pytest.mark.parametrize('status', ['revoked', 'rejected', 'superseded', 'archived'])
def test_inactive_live_case_cannot_be_reused(monkeypatch, status):
    _live, _scope, _receipt, record, check = evidence_case(monkeypatch)
    record.status = status
    assert not check()


def test_live_path_comparison_respects_platform_case_semantics():
    from eimemory.governance.l5.live_task_acceptance import _same_path
    assert _same_path('/release/current', '/release/current/')
    assert not _same_path('', '')
    assert _same_path('/release/A', '/release/a') is (os.name == 'nt')

    assert not _same_path("/release/link/../private", "/release/private")
