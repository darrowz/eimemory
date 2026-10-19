"""Controlled results must pass their real verifier before release reuse."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from eimemory.contracts.release_identity import release_identity_payload
from eimemory.governance.release import controlled_release_lineage as module
from eimemory.governance.release import release_lineage
from eimemory.governance.release.evidence_contract import ReleaseIdentity
from test_controlled_production_acceptance import setup, run, cpa, OWNER, RELEASE
from test_controlled_production_release_closure import pipeline, complete_report


def release():
    return ReleaseIdentity(commit=RELEASE['release_commit'], version=RELEASE['release_version'],
        receipt_id=RELEASE['deployment_receipt_id'], session_id=RELEASE['release_session_id'])


def test_real_signed_physical_controlled_result_is_a_distinct_recall_gate(setup):
    result = run(setup)
    record = setup.runtime.store.get_by_id(result['record_id'], scope=OWNER)
    assert release_lineage._recall_gate_contract_error(
        setup.runtime, scope=OWNER, current_release=release(), references=[record.record_id],
        records={record.record_id: record}, domain_changed=True) == ''
    assert len(setup.calls) == 12  # Verification never replays user queries.
    assert result['certifies_natural_queries'] is False


@pytest.mark.parametrize('damage', ['hmac', 'incomplete', 'version', 'new_failed_attempt', 'scope', 'release', 'extra_ref'])
def test_native_controlled_authority_cannot_be_lent_or_summarized(setup, damage):
    result = run(setup)
    owner, current, refs = OWNER, release(), [result['record_id']]
    if damage in {'hmac', 'incomplete'}:
        packet = setup.runtime.store.get_by_id(result['record_id'], scope=OWNER)
        if damage == 'hmac':
            packet.content['signature'] = '0' * 64
        else:
            packet.content['agents'][0]['samples'][0]['complete'] = False
            packet.content = cpa.seal({k: v for k, v in packet.content.items()
                                      if k not in {'key_id', 'signature'}}, cpa.SCHEMA)
            packet.meta['report_digest'] = cpa.digest(packet.content)
        setup.runtime.store.append(packet)
    if damage == 'version':
        item = setup.memories['hongtai'][0]
        item.content['text'] = 'A later actual source version'
        setup.runtime.store.append(item)
    if damage == 'new_failed_attempt':
        setup.monkeypatch.setattr(setup.runtime.memory, 'recall',
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError('retrieval failed')))
        assert not run(setup)['accepted']
    if damage == 'scope': owner = type(OWNER)(**{**asdict(OWNER), 'user_id': 'other'})
    if damage == 'release': current = ReleaseIdentity('b' * 40, current.version, current.receipt_id, current.session_id)
    if damage == 'extra_ref': refs.append('another-reference')
    assert module.controlled_recall_gate_error(setup.runtime, scope=owner,
        release=current, references=refs)


def test_controlled_closure_stops_when_lineage_revalidation_fails(pipeline, monkeypatch):
    monkeypatch.setattr(module, 'finalize_controlled_release_lineage', lambda *args, **kwargs:
        dict(ok=True, validated=True, compatible=False, error='current_domain_proof_missing'))
    report = pipeline.run()
    assert report['ok'] is False and report['closure_complete'] is False
    assert report['blocked_stage'] == 'release_lineage'
    assert report['release_lineage_required'] is True


def test_current_release_cannot_remove_required_lineage_marker():
    from eimemory.governance.release.closure_contracts import controlled_production_closure_report_ok
    report = complete_report()
    for node in (report['deployment'], report['deployment_receipt']): node['version'] = '1.14.156'
    assert not controlled_production_closure_report_ok(report)


def test_native_git_sqlite_lineage_rechecks_signed_recall_high_water(setup, tmp_path):
    from test_release_lineage import _repo, _commit, _receipt

    repo = _repo(tmp_path)
    prior_commit = _commit(repo, 'eimemory/retrieval/engine.py', 'prior\n', 'prior')
    current_commit = _commit(repo, 'eimemory/retrieval/engine.py', 'current\n', 'current')
    _receipt(setup.runtime, OWNER, prior_commit, '1.14.155')
    current = _receipt(setup.runtime, OWNER, current_commit, '1.14.156', prior_commit=prior_commit)
    setup.runtime._test_runtime_commit = current_commit
    setup.monkeypatch.setattr(cpa, '_release', lambda *args, **kwargs:
        (release_identity_payload(current), {'record_ref': current.receipt_id, 'record_digest': 'c' * 64}))
    accepted = run(setup)
    assert accepted['accepted']
    recorded = release_lineage.record_release_lineage(setup.runtime, scope=OWNER,
        repo_root=repo, current_release=current,
        gate_evidence={'memory.recall': [accepted['record_id']]}, legacy_compatibility=True)
    assert recorded['ok'] and recorded['validated'] and recorded['compatible'], recorded
    assert recorded['domains']['memory.recall']['mode'] == 'current'
    resolved = release_lineage.current_release_lineage(setup.runtime, scope=OWNER,
        repo_root=repo, current_release=current, legacy_compatibility=True)
    assert resolved['record_id'] == recorded['record_id'] and resolved['compatible']
    # A new genuine signed attempt supersedes the old gate. No old successful
    # snapshot is silently retained as current, even if the new attempt passes.
    later = run(setup)
    assert later['accepted'] and later['record_id'] != accepted['record_id']
    resolved = release_lineage.current_release_lineage(setup.runtime, scope=OWNER,
        repo_root=repo, current_release=current, legacy_compatibility=True)
    assert not resolved.get('compatible')


def test_native_lineage_refresh_requires_established_closure_and_current_attempt(setup, monkeypatch):
    from test_release_lineage import _receipt
    current = _receipt(setup.runtime, OWNER, RELEASE['release_commit'], RELEASE['release_version'])
    monkeypatch.setattr(cpa, '_release', lambda *args, **kwargs:
        (release_identity_payload(current), {'record_ref': current.receipt_id, 'record_digest': 'c' * 64}))
    accepted = run(setup)
    assert accepted['accepted']
    result = module.refresh_current_controlled_release_lineage(setup.runtime, scope=OWNER, release=current)
    assert result['ok'] and result['status'] == 'awaiting_initial_closure'
    setup.monkeypatch.setattr(setup.runtime.memory, 'recall',
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError('retrieval failed')))
    assert not run(setup)['accepted']
    result = module.refresh_current_controlled_release_lineage(setup.runtime, scope=OWNER, release=current)
    assert not result['ok'] and result['reason'] == 'current_controlled_recall_evidence_invalid'


@pytest.mark.parametrize('failed_replay', [False, True])
def test_refresh_rebinds_only_actual_latest_acceptance_without_replaying_queries(setup, monkeypatch, tmp_path, failed_replay):
    from eimemory.models.records import RecordEnvelope
    from eimemory.config import trusted
    from eimemory.governance.l5 import l5_readiness
    from test_release_lineage import _receipt

    current = _receipt(setup.runtime, OWNER, RELEASE['release_commit'], RELEASE['release_version'])
    monkeypatch.setattr(cpa, '_release', lambda *args, **kwargs:
        (release_identity_payload(current), {'record_ref': current.receipt_id, 'record_digest': 'c' * 64}))
    earlier, latest = run(setup), run(setup)
    gates = {'memory.recall': [earlier['record_id']], 'memory.governance': ['old-manifest'],
        'storage.integrity': ['live'], 'channel.delivery': ['channel'],
        'code.evolution': [current.receipt_id], 'deployment.runtime': [current.receipt_id]}
    seed = setup.runtime.store.append(RecordEnvelope.create(kind='l5_self_continuity',
        title='Existing independent closure', scope=OWNER, source=release_lineage.SOURCE,
        content={'lineage': {'schema_version': release_lineage.SCHEMA_VERSION,
            'scope': asdict(OWNER), 'current_release': release_lineage._identity_payload(current),
            'gate_evidence': deepcopy(gates)}},
        meta={'report_type': 'release_lineage', 'current_commit': current.commit}))
    original = deepcopy(seed.to_dict())
    monkeypatch.setattr(release_lineage, '_manifest_profile_replay_contract',
        lambda *args, **kwargs: ({'fixture-capability'}, {}, ''))
    monkeypatch.setattr(release_lineage, '_resolve_lineage_catalog', lambda *args, **kwargs: object())
    monkeypatch.setattr(l5_readiness, '_verified_replay_summary', lambda *args, **kwargs:
        dict(manifest_record_id_cohort=['new-manifest'], fail_count=1 if failed_replay else 0,
            not_run_count=0))
    monkeypatch.setattr(trusted, 'trusted_repository_root', lambda: tmp_path)
    calls = []
    def record(runtime, **kwargs):
        calls.append(kwargs)
        # Keep the actual new signed acceptance verifier in this orchestration
        # test; the pre-existing native lineage suite verifies other domains.
        assert not module.controlled_recall_gate_error(runtime, scope=OWNER,
            release=current, references=kwargs['gate_evidence']['memory.recall'])
        return dict(ok=True, validated=True, compatible=True, record_id='new-lineage')
    monkeypatch.setattr(release_lineage, 'record_release_lineage', record)
    monkeypatch.setattr(release_lineage, 'current_release_lineage', lambda *args, **kwargs:
        dict(ok=True, validated=True, compatible=True, record_id='new-lineage',
             gate_evidence=deepcopy(calls[-1]['gate_evidence'])))
    result = module.refresh_current_controlled_release_lineage(setup.runtime, scope=OWNER, release=current)
    if failed_replay:
        assert not result['ok'] and not calls
    else:
        assert result['ok'] and result['status'] == 'revalidated'
        expected = {**gates, 'memory.recall': [latest['record_id']], 'memory.governance': ['new-manifest']}
        assert calls[0]['gate_evidence'] == expected
        assert result['controlled_reference'] == latest['record_id']
        assert result['certifies_natural_queries'] is False and result['l5_certified'] is False
    assert len(setup.calls) == 24
    assert setup.runtime.store.get_by_id(seed.record_id, scope=OWNER).to_dict() == original


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'record_id', 'not_compatible'])
def test_finalizer_requires_exact_native_gate_set_and_readback(monkeypatch, damage):
    from release_report_fixtures import live
    calls = []
    def record(*args, **kwargs):
        calls.append(kwargs)
        return dict(ok=True, validated=True, compatible=True, record_id='lineage',
                    gate_evidence=deepcopy(kwargs['gate_evidence']))
    def read(*args, **kwargs):
        result = dict(ok=True, validated=True, compatible=True, record_id='lineage',
                      gate_evidence=deepcopy(calls[-1]['gate_evidence']))
        if damage == 'record_id': result['record_id'] = 'other-lineage'
        if damage == 'not_compatible': result['compatible'] = False
        return result
    monkeypatch.setattr(release_lineage, 'record_release_lineage', record)
    monkeypatch.setattr(release_lineage, 'current_release_lineage', read)
    replay = dict(capability_replay=dict(manifest_record_id='actual-manifest'))
    if damage == 'missing': replay = {}
    if damage == 'duplicate': replay['weak_capability_replay'] = dict(manifest_record_id='other-manifest')
    result = module.finalize_controlled_release_lineage(object(), scope=asdict(OWNER),
        repo_root='/isolated', release=release(), controlled_reference='actual-controlled',
        replay=replay, live=live(), channel_reference='actual-channel')
    assert not result.get('compatible')
    if damage in {'missing', 'duplicate'}: assert not calls
