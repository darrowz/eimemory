"""Lineage's independent wire-contract check; real delegation lives in dynamic eval tests."""
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from eimemory.models.records import ScopeRef
from eimemory.governance.capability.capability_replay_packs import (
    DELEGATED_SELECTION_CONTRACT_SCHEMA, DELEGATED_MANIFEST_SCHEMA_VERSION,
    SELECTION_CONTRACT_SCHEMA, replay_selection_contract_digest,
)
from eimemory.governance.release.release_lineage import _manifest_profile_replay_contract


OWNER = ScopeRef('tenant', 'agent', 'business-channel', 'owner')


def manifest():
    selection = dict(schema=DELEGATED_SELECTION_CONTRACT_SCHEMA,
        mode='dynamic_profile', runtime_scope=asdict(OWNER), capability_scope='global',
        capabilities=['code.implementation'], profile_key='fixture', profile_id='profile',
        profile_digest='a' * 64, resolution_digest='b' * 64,
        provider_authority_snapshot={'schema': 'unit-contract-fixture'},
        provider_authority_digest='c' * 64, provider_scope={**asdict(OWNER), 'user_id': 'provider'},
        minimums_by_capability={'code.implementation': dict(minimum_executed=3,
            minimum_distinct_evidence=3, minimum_pass_rate=0.8)})
    selection['selection_contract_digest'] = replay_selection_contract_digest(selection)
    mirrors = dict(schema_version=DELEGATED_MANIFEST_SCHEMA_VERSION,
        selection_contract_schema=selection['schema'],
        selection_contract_digest=selection['selection_contract_digest'],
        **{key: deepcopy(selection[key]) for key in ('provider_authority_snapshot',
            'provider_authority_digest', 'provider_scope')})
    return SimpleNamespace(source='eimemory.capability_replay', scope=OWNER,
        content=dict(schema_version=DELEGATED_MANIFEST_SCHEMA_VERSION,
            report_type='capability_replay_manifest', capabilities=['code.implementation'],
            selection_contract=selection), meta=deepcopy(mirrors), provenance=deepcopy(mirrors))


def test_delegated_selection_keeps_owner_and_original_thresholds_in_lineage():
    record = manifest()
    capabilities, minimums, error = _manifest_profile_replay_contract({'actual': record}, ['actual'], scope=OWNER)
    assert not error and capabilities == {'code.implementation'}
    assert minimums['code.implementation']['minimum_executed'] == 3


@pytest.mark.parametrize('damage', ['missing_one', 'strip_all', 'v1_authority',
    'meta_scope', 'provenance_digest', 'manifest_version', 'strip_and_downgrade'])
def test_rehashing_cannot_downgrade_delegated_lineage_contract(damage):
    record = manifest()
    selection = record.content['selection_contract']
    if damage == 'missing_one': del selection['provider_authority_digest']
    if damage in {'strip_all', 'strip_and_downgrade'}:
        for key in ('provider_authority_snapshot', 'provider_authority_digest', 'provider_scope'):
            del selection[key]
    if damage in {'v1_authority', 'strip_and_downgrade'}:
        selection['schema'] = SELECTION_CONTRACT_SCHEMA
        record.meta['selection_contract_schema'] = record.provenance['selection_contract_schema'] = SELECTION_CONTRACT_SCHEMA
    if damage == 'meta_scope': record.meta['provider_scope']['user_id'] = 'other'
    if damage == 'provenance_digest': record.provenance['provider_authority_digest'] = 'f' * 64
    if damage == 'manifest_version': record.content['schema_version'] = 'capability_replay_manifest.v2'
    selection['selection_contract_digest'] = replay_selection_contract_digest(selection)
    record.meta['selection_contract_digest'] = record.provenance['selection_contract_digest'] = selection['selection_contract_digest']
    assert _manifest_profile_replay_contract({'actual': record}, ['actual'], scope=OWNER)[2]
