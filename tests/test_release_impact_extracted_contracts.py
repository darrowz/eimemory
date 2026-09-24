from pathlib import Path

from eimemory.governance.release_impact import _domains_for_change


def test_nightly_result_contract_has_governance_and_evolution_owners():
    assert _domains_for_change(Path('.'), path='eimemory/scheduler/result_contract.py',
                               ancestor='a' * 40, current='b' * 40) == {
        'memory.governance', 'code.evolution'}


def test_host_snapshot_seams_are_delivery_and_deployment_not_storage():
    for path in ('deploy/ensure_hermes_sync_snapshot.py',
                 'integrations/hermes/host/memory_sync_snapshot.py'):
        assert _domains_for_change(Path('.'), path=path, ancestor='a' * 40,
                                   current='b' * 40) == {'channel.delivery', 'deployment.runtime'}
