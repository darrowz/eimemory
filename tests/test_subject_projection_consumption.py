"""A revoked subject grant cannot survive current recall, cache or attestation."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict

import pytest

from test_subject_projection import proposal, synthetic_grant
from eimemory import subject_projection as subject
from eimemory.identity_governance import filter_current_records
from eimemory.evaluation.explicit_recall import _reference
from eimemory.models.records import RecordEnvelope
from eimemory.subject_projection_consumption import projection_is_current, projection_read_fence


@pytest.fixture
def current_projection(proposal, monkeypatch):
    runtime, manifest, originals, *_ = proposal
    grant, authority = synthetic_grant(manifest)
    projected = subject._projection(originals[0], manifest, manifest['items'][0], grant)
    ordinary = RecordEnvelope.create(kind='memory', title='Existing ordinary instruction',
        summary='Preserve the source and its qualifications.', scope=projected.scope,
        source='hermes.l1', source_id='hermes', meta={'memory_type':'instruction'})
    with runtime.store.locked() as db:
        db.upsert(projected)
        db.upsert(ordinary)

    @contextmanager
    def snapshot(*_args, **_kwargs):
        yield authority, None
    monkeypatch.setattr(subject.authority_api, 'snapshot', snapshot)
    return runtime, originals[0], projected, ordinary, authority, grant


def test_current_engine_cache_and_explicit_reference_all_recheck_revocation(current_projection):
    runtime, original, projected, ordinary, authority, grant = current_projection
    scope = asdict(projected.scope)

    def cached():
        return runtime.proactive._revalidate_cached_records((projected, ordinary),
            exact_scope=scope, source_ids=('hermes',))

    assert filter_current_records(runtime.store, [projected, ordinary], scope=scope) == ([projected, ordinary], 0)
    assert cached() == (projected, ordinary)
    assert _reference(runtime, projected, projected.scope)['record_ref'] == projected.record_id
    authority.revoked.add(grant['event_id'])
    assert filter_current_records(runtime.store, [projected, ordinary], scope=scope) == ([ordinary], 1)
    assert cached() == (ordinary,)
    with pytest.raises(ValueError, match='subject projection authority'):
        _reference(runtime, projected, projected.scope)
    assert not projection_is_current(runtime.store, projected)
    assert projection_is_current(runtime.store, ordinary)
    assert runtime.store.get_by_exact_ref(original.record_id, scope=original.scope,
        source_id='hermes').to_dict() == original.to_dict()


def test_source_physical_version_drift_invalidates_current_projection(current_projection):
    runtime, original, projected, ordinary, *_ = current_projection
    with runtime.store.locked() as db:
        db.execute("UPDATE records SET embedding_json='[]' WHERE record_id=?", (original.record_id,))
        db.commit()
    assert filter_current_records(runtime.store, [projected, ordinary], scope=projected.scope) == ([ordinary], 1)
    assert not projection_is_current(runtime.store, projected)


def test_real_recall_honors_current_subject_grant_and_revocation(current_projection, monkeypatch):
    runtime, _, projected, _, authority, grant = current_projection
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_REQUIRED', '0')
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '0')
    context = {'source_ids':['hermes'], 'exact_scope_only':True,
               'include_evidence_only':True, 'kinds':['memory']}
    before = runtime.memory.recall(query=projected.content['text'], scope=asdict(projected.scope),
                                   task_context=context, limit=5)
    assert projected.record_id in {record.record_id for record in before.items}
    authority.revoked.add(grant['event_id'])
    after = runtime.memory.recall(query=projected.content['text'], scope=asdict(projected.scope),
                                  task_context=context, limit=5)
    assert projected.record_id not in {record.record_id for record in after.items}


def test_missing_authority_fails_only_marked_projection(current_projection, monkeypatch):
    runtime, _, projected, ordinary, *_ = current_projection
    def unavailable(*_args, **_kwargs):
        raise OSError('authority unavailable')
    monkeypatch.setattr(subject.authority_api, 'snapshot', unavailable)
    assert filter_current_records(runtime.store, [projected, ordinary], scope=projected.scope) == ([ordinary], 1)
    assert not projection_is_current(runtime.store, projected)
    assert projection_is_current(runtime.store, ordinary)


@pytest.mark.parametrize('lane', ['cascade_evidence', 'raw_evidence', 'recall_view'])
def test_reference_only_delivery_rechecks_revocation_but_ignores_unselected_diagnostics(current_projection, lane):
    runtime, _, projected, ordinary, authority, grant = current_projection
    row = {'record_id':projected.record_id}
    refs = {lane: ([{'record':row}] if lane == 'raw_evidence' else
                   {'items':[row]} if lane == 'recall_view' else [row])}
    assert filter_current_records(runtime.store, [ordinary], scope=ordinary.scope, references=refs) == ([ordinary], 0)
    authority.revoked.add(grant['event_id'])
    assert filter_current_records(runtime.store, [ordinary], scope=ordinary.scope, references=refs) == ([], 1)
    diagnostics = {'engine_diagnostics':{'scored_candidates':[{'record_id':projected.record_id}]}}
    assert filter_current_records(runtime.store, [ordinary], scope=ordinary.scope, references=diagnostics) == ([ordinary], 0)
    quoted = {'raw_evidence':[{'record':{'record_id':ordinary.record_id,
        'content':{'literal_example':{'record_id':projected.record_id}}}}]}
    assert filter_current_records(runtime.store, [ordinary], scope=ordinary.scope, references=quoted) == ([ordinary], 0)


def test_consumption_authority_lock_is_nonblocking(current_projection, monkeypatch):
    runtime, _, projected, _, _, _ = current_projection
    @contextmanager
    def busy(*_args, **kwargs):
        assert kwargs.get('blocking') is False
        raise BlockingIOError('operator lock busy')
        yield
    monkeypatch.setattr(subject.authority_api, 'snapshot', busy)
    assert not projection_is_current(runtime.store, projected)


def test_ordinary_reads_never_open_projection_authority(current_projection, monkeypatch):
    runtime, _, _, ordinary, *_ = current_projection
    monkeypatch.setattr(subject.authority_api, 'snapshot', lambda *_a, **_k: pytest.fail('unneeded authority read'))
    assert projection_is_current(runtime.store, ordinary)
    with projection_read_fence([ordinary]) as valid:
        assert valid(None, ordinary)


def test_forged_projection_is_rejected_at_explicit_and_cached_boundaries(current_projection):
    runtime, _, projected, ordinary, *_ = current_projection
    damaged = deepcopy(projected)
    damaged.content['text'] += ' altered'
    with runtime.store.locked() as db:
        db.upsert(damaged)
    with pytest.raises(ValueError, match='subject projection authority'):
        _reference(runtime, damaged, damaged.scope)
    assert runtime.proactive._revalidate_cached_records((damaged,),
        exact_scope=asdict(damaged.scope), source_ids=('hermes',)) == ()
