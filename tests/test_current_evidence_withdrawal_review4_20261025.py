"""Current exact authority governs cached memory and persisted brief consumption."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.knowledge.evidence_contracts import versioned_record_ref
from eimemory.knowledge.projectors import stable_projection_id
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.jsonl import canonical_payload_json, payload_digest


SCOPE = ScopeRef('review4', 'current-evidence', 'withdrawal', 'owner')
PRIVATE_TITLE = 'WITHDRAWN_CONTEXT_TITLE'
PRIVATE_BODY = 'WITHDRAWN_CONTEXT_BODY'
CURRENT_BODY = 'CURRENT_CONTEXT_BODY'
_EXACT_PARENT = ('record_id=? AND tenant_id=? AND agent_id=? AND workspace_id=? '
                 'AND user_id=? AND source_id=?')


def _memory(runtime):
    return runtime.memory.ingest(
        text=f'Decision: verify {PRIVATE_BODY} for operational memory recall. '
             f'Follow up: review {PRIVATE_BODY} against the current source.',
        memory_type='conversation', title=PRIVATE_TITLE, scope=asdict(SCOPE),
        source='review4.current-evidence', source_id='journal', force_capture=True,
    )


def _current(runtime, record, **kwargs):
    return runtime.store.get_by_exact_ref(record.record_id, scope=record.scope,
                                          source_id=record.source_id, **kwargs)


def _persisted_brief(runtime, source):
    report = runtime.build_daily_brief(scope=asdict(source.scope),
        date=source.time.occurred_at[:10], persist=True)
    assert report['persisted'] is True
    assert PRIVATE_BODY in json.dumps(report['conversation_summary'])
    return runtime.store.get_by_exact_ref(report['persisted_record_id'],
                                          scope=source.scope, source_id='default')


def _assert_kept(runtime, record):
    result = runtime.filter_answer_evidence([record])
    assert [item.record_id for item in result['records']] == [record.record_id]


def _assert_excluded(runtime, record):
    result = runtime.filter_answer_evidence([record])
    assert result['records'] == []
    assert result['evidence_gate']['excluded_count'] >= 1
    diagnostics = json.dumps(result)
    assert PRIVATE_TITLE not in diagnostics
    assert PRIVATE_BODY not in diagnostics


def _remove(runtime, original):
    current = _current(runtime, original)
    current.status = 'removed'
    current.touch()
    return runtime.store.rewrite(current)


@pytest.mark.parametrize('cached', [False, True], ids=['current-envelope', 'cached-active-envelope'])
def test_removed_memory_cannot_enter_answer_context(tmp_path, cached):
    with Runtime.create(root=tmp_path) as runtime:
        original = _memory(runtime)
        _assert_kept(runtime, original)
        retired = _remove(runtime, original)
        assert original.status == 'active' and retired.status == 'removed'
        _assert_excluded(runtime, original if cached else retired)


def test_persisted_brief_rechecks_removed_source_after_reopen(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        source = _memory(runtime)
        brief = _persisted_brief(runtime, source)
        _assert_kept(runtime, brief)
        _remove(runtime, source)
        _assert_excluded(runtime, brief)
        _assert_excluded(runtime, _current(runtime, brief))
    with Runtime.create(root=tmp_path) as reopened:
        assert _current(reopened, source).status == 'removed'
        assert _current(reopened, brief).status == 'active'
        _assert_excluded(reopened, brief)
        _assert_excluded(reopened, _current(reopened, brief))


def test_active_source_revision_invalidates_cached_memory_and_old_brief(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        source = _memory(runtime)
        brief = _persisted_brief(runtime, source)
        _assert_kept(runtime, source)
        _assert_kept(runtime, brief)
        current = _current(runtime, source)
        current.summary = current.detail = f'Decision: use {CURRENT_BODY} for the updated memory policy.'
        current.content['text'] = current.summary
        current.touch()
        runtime.store.rewrite(current)
        assert current.status == 'active'
        _assert_kept(runtime, _current(runtime, source))
        _assert_excluded(runtime, source)
        _assert_excluded(runtime, brief)


@pytest.mark.parametrize('fault', ['missing', 'unreadable_payload', 'moved_scope', 'source_projection_mismatch'])
def test_persisted_brief_requires_its_exact_readable_parent(tmp_path, fault):
    with Runtime.create(root=tmp_path) as runtime:
        source = _memory(runtime)
        brief = _persisted_brief(runtime, source)
        _assert_kept(runtime, brief)
        if fault == 'moved_scope':
            moved = deepcopy(source)
            moved.scope = replace(source.scope, user_id='another-owner')
            runtime.store.rewrite(moved, previous_scope=source.scope)
            assert _current(runtime, moved).status == 'active'
            assert _current(runtime, source) is None
        else:
            identity = (source.record_id, source.scope.tenant_id, source.scope.agent_id,
                        source.scope.workspace_id, source.scope.user_id, source.source_id)

            def damage(sqlite):
                if fault == 'missing':
                    sqlite.execute(f'DELETE FROM records WHERE {_EXACT_PARENT}', identity)
                elif fault == 'unreadable_payload':
                    sqlite.execute(f'UPDATE records SET payload_json=? WHERE {_EXACT_PARENT}',
                                   ('{"broken":true}', *identity))
                else:
                    # Explicit storage-corruption control, not a supported
                    # same-ID/source mutation. The row's SQL identity stays
                    # unchanged while its valid JSON envelope claims another
                    # source, so strict hydration must reject the mismatch.
                    payload = source.to_dict()
                    payload['source_id'] = 'foreign-source'
                    sqlite.execute(f'UPDATE records SET payload_json=?,payload_digest=? WHERE {_EXACT_PARENT}',
                                   (canonical_payload_json(payload), payload_digest(payload), *identity))
                return None, [], []

            runtime.store.mutate_records_atomically(damage)
            if fault == 'missing':
                assert _current(runtime, source) is None
            else:
                with pytest.raises(RuntimeError, match='exact_record_unavailable'):
                    _current(runtime, source, fail_on_unavailable=True)
        _assert_excluded(runtime, brief)


def test_legacy_native_brief_without_typed_lineage_cannot_certify_its_body(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        source = _memory(runtime)
        brief = _persisted_brief(runtime, source)
        for container in (brief.content, brief.provenance, brief.content['brief'],
                          brief.content['delivery']['outbox']['body']):
            container.pop('source_lineage_schema', None)
            container.pop('source_record_refs', None)
        runtime.store.rewrite(brief)
        _assert_excluded(runtime, _current(runtime, brief))


def test_current_brief_accepts_reviewed_rule_and_other_source_in_same_owner(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        source = _memory(runtime)
        rule = runtime.store.append(RecordEnvelope.create(
            kind='rule', status='accepted', title='Reviewed followup policy',
            summary='Follow up: retain the reviewed operational evidence policy.',
            scope=source.scope, source='review4.rule', source_id='policy',
        ))
        brief = _persisted_brief(runtime, source)
        followups = brief.content['brief']['followups']
        assert {source.record_id, rule.record_id}.issubset({item['record_id'] for item in followups})
        assert source.source_id != rule.source_id != brief.source_id
        _assert_kept(runtime, rule)
        _assert_kept(runtime, brief)


@pytest.mark.parametrize('defect', ['conflicting_content_ref', 'content_only_typed_lineage'])
def test_native_projection_cannot_ignore_partial_or_conflicting_content_lineage(tmp_path, defect):
    with Runtime.create(root=tmp_path) as runtime:
        extraction = runtime.extract_paper_memory({
            'paper_source_id': 'paper-current-lineage', 'title': 'Operational memory policy',
            'body': f'Verified operational memory must preserve {PRIVATE_BODY} exact source provenance for runtime decisions.',
            'provenance': {'published_at': '2026-10-25'},
        }, scope=asdict(SCOPE))
        claim = runtime.store.get_by_exact_ref(extraction.claims[0].claim_card_id,
                                               scope=SCOPE, source_id='default')
        claim.content['confidence'] = claim.meta['confidence'] = 0.95
        runtime.store.rewrite(claim)
        report = runtime.project_operational_knowledge(scope=asdict(SCOPE))
        projection_id = stable_projection_id(claim)
        assert projection_id in report['projected_ids']
        projection = runtime.store.get_by_exact_ref(projection_id, scope=SCOPE, source_id='default')
        assert PRIVATE_BODY in projection.summary
        _assert_kept(runtime, projection)

        projection.content['source_record_ref'] = deepcopy(projection.meta['source_record_ref'])
        if defect == 'conflicting_content_ref':
            projection.content['source_record_ref']['source_id'] = 'foreign-source'
        else:
            for container in (projection.meta, projection.meta.get('business_meta', {}), projection.provenance):
                for key in ('source_record_ref', 'supporting_claim_refs', 'projection_lineage_schema'):
                    container.pop(key, None)
        runtime.store.rewrite(projection)
        assert _current(runtime, claim).status == 'active'
        current = _current(runtime, projection)
        if defect == 'content_only_typed_lineage':
            assert not {'source_record_ref', 'supporting_claim_refs', 'projection_lineage_schema'} & set(
                business_metadata(current.meta))
            assert 'source_record_ref' in current.content
        _assert_excluded(runtime, current)


def test_manual_operational_classification_remains_consumable_until_its_own_withdrawal(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        original = runtime.memory.ingest(
            text=f'Decision: verify {PRIVATE_BODY} for ordinary operational memory.',
            memory_type='conversation', title=PRIVATE_TITLE, scope=asdict(SCOPE),
            source='review4.manual-classification', source_id='journal', force_capture=True,
            meta={'projection_type': 'operational_knowledge'},
        )
        current = _current(runtime, original)
        assert current.source != 'eimemory.knowledge.projectors'
        assert business_metadata(current.meta)['projection_type'] == 'operational_knowledge'
        for container in (business_metadata(current.meta), current.content, current.provenance):
            assert not {'projection_lineage_schema', 'source_record_ref', 'supporting_claim_refs',
                        'paper_source_id', 'source_ids'} & set(container)
        _assert_kept(runtime, current)
        retired = _remove(runtime, original)
        _assert_excluded(runtime, current)
        _assert_excluded(runtime, retired)


def test_legacy_nonstring_occurrence_uses_normal_hydration_for_memory_and_brief(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        original = _memory(runtime)
        payload = original.to_dict()
        original_created_at = payload['time']['created_at']
        original_updated_at = payload['time']['updated_at']
        assert isinstance(original_created_at, str) and isinstance(original_updated_at, str)
        # Older valid JSON envelopes may carry a numeric occurrence. Ordinary
        # reads normalize it to a stable string; this is not an L1 source clock.
        payload['time']['occurred_at'] = 123
        identity = (original.record_id, original.scope.tenant_id, original.scope.agent_id,
                    original.scope.workspace_id, original.scope.user_id, original.source_id)

        def store_legacy_payload(sqlite):
            sqlite.execute(f'UPDATE records SET payload_json=?,payload_digest=? WHERE {_EXACT_PARENT}',
                           (canonical_payload_json(payload), payload_digest(payload), *identity))
            return None, [], []

        runtime.store.mutate_records_atomically(store_legacy_payload)
        current = _current(runtime, original)
        assert current.time.occurred_at == '123'
        assert current.time.created_at == original_created_at
        assert current.time.updated_at == original_updated_at
        assert _current(runtime, original).to_dict() == current.to_dict()
        _assert_kept(runtime, current)

        report = runtime.build_daily_brief(scope=asdict(SCOPE),
            date=original_created_at[:10], persist=True)
        assert report['persisted'] is True
        assert PRIVATE_BODY in json.dumps(report['conversation_summary'])
        brief = runtime.store.get_by_exact_ref(report['persisted_record_id'],
                                               scope=SCOPE, source_id='default')
        assert versioned_record_ref(current) in brief.content['source_record_refs']
        _assert_kept(runtime, brief)

        retired = _remove(runtime, original)
        _assert_excluded(runtime, current)
        _assert_excluded(runtime, retired)
        _assert_excluded(runtime, brief)
