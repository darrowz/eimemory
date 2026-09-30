from pathlib import Path

from eimemory.governance.release_impact import _domains_for_change


def test_nightly_result_contract_has_governance_and_evolution_owners():
    # Since 1.14.x the nightly result contract is also a recall release
    # boundary, so it re-gates every domain (stricter, never looser).
    from eimemory.governance.release.release_impact import DOMAINS
    domains = _domains_for_change(Path('.'), path='eimemory/scheduler/result_contract.py',
                                  ancestor='a' * 40, current='b' * 40)
    assert {'memory.governance', 'code.evolution'} <= domains
    assert domains == set(DOMAINS)


def test_host_snapshot_seams_are_delivery_and_deployment_not_storage():
    for path in ('deploy/ensure_hermes_sync_snapshot.py',
                 'integrations/hermes/host/memory_sync_snapshot.py'):
        assert _domains_for_change(Path('.'), path=path, ancestor='a' * 40,
                                   current='b' * 40) == {'channel.delivery', 'deployment.runtime'}
