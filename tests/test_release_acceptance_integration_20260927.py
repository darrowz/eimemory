"""Full Runtime integration checks. Not exercised by the excerpt audit harness."""
from __future__ import annotations

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.capability.capability_acceptance import LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS
from eimemory.governance.release.closure_contracts import (
    LEGACY_RELEASE_CASE_IDS, acceptance_report_ok, legacy_release_replay_ok,
)
from eimemory.governance.release.release_impact import DOMAIN_PATHS


@pytest.mark.parametrize('persist',[False,True])
def test_real_twelve_case_acceptance_report_contract(tmp_path,persist):
    runtime=Runtime.create(root=tmp_path)
    try:
        report=runtime.run_capability_acceptance(
            scope={'tenant_id':'audit','agent_id':'audit','workspace_id':'closure','user_id':'auditor'},
            case_ids=list(LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS),
            persist=persist,legacy_compatibility=True,
        )
        assert set(LEGACY_RELEASE_CASE_IDS)==set(LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS)
        assert acceptance_report_ok(report,expected_count=12,expected_case_ids=LEGACY_RELEASE_CASE_IDS,
                                    require_persisted=persist), report
    finally:
        runtime.close()


def test_real_weak_bootstrap_produces_persisted_bound_replay(tmp_path):
    runtime=Runtime.create(root=tmp_path)
    try:
        report=runtime.run_weak_capability_replay_gate(
            scope={'tenant_id':'audit','agent_id':'audit','workspace_id':'closure','user_id':'auditor'},
            persist=True,loop_id='audit-regression',
        )
        assert legacy_release_replay_ok(report),report
        assert report.get('closure_complete') is not True
    finally:
        runtime.close()


@pytest.mark.parametrize('path',[
    'eimemory/governance/promotion/promotion_git_ops.py',
    'eimemory/governance/l5/closure_rehearsal.py',
    'eimemory/governance/release/release_closure.py',
    'eimemory/governance/release/release_closure_pending.py',
    'eimemory/core/python_invocation.py',
    'eimemory/governance/release/closure_contracts.py',
])
def test_release_execution_paths_invalidate_both_execution_domains(path):
    for domain in ('memory.governance','code.evolution','deployment.runtime'):
        assert any(path == rule or path.startswith(rule.rstrip("/") + "/")
                   for rule in DOMAIN_PATHS[domain]), (path, domain)
