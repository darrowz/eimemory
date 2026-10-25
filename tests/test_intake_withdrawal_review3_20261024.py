"""A stored paper's withdrawal governs its derived, user-visible evidence."""
from dataclasses import asdict
from datetime import datetime
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.core import clock
from eimemory.intake.closure import build_research_closure_review
from eimemory.intake.papers.artifacts import PaperArtifactError
from eimemory.models.paper_sources import PaperSource
from eimemory.models.knowledge_pages import KnowledgePage
from eimemory.models.records import RecordEnvelope, ScopeRef
from test_paper_pdf_pipeline import _minimal_pdf


SCOPE = ScopeRef('review3', 'withdrawal', 'local', 'owner')
TEXT = 'Verified operational memory recall must preserve exact source provenance for runtime decisions'
DATE = '2026-10-24'
REVIEW = json.dumps({'verdict': 'approve', 'rationale': 'The source supports the proposal',
                     'required_followup': 'Add a local replay', 'risk': 'low'})


@pytest.fixture
def paper_clock(monkeypatch):
    """Keep fixed-date evidence fixtures independent of the day pytest runs."""
    current = datetime.fromisoformat(f'{DATE}T12:00:00+00:00')

    class FixtureDatetime:
        @staticmethod
        def now(tz):
            return current.astimezone(tz)

    def set_instant(value):
        nonlocal current
        current = datetime.fromisoformat(value.replace('Z', '+00:00'))

    # Patch the shared clock's dependency, including callers that imported
    # now_iso before this fixture, and restore it after this test only.
    monkeypatch.setattr(clock, 'datetime', FixtureDatetime)
    return set_instant


def _extract(runtime, source_id):
    return runtime.extract_paper_memory(
        {'paper_source_id': source_id, 'title': 'Operational memory research', 'body': TEXT,
         'provenance': {'published_at': DATE}}, scope=asdict(SCOPE))


def _chain(runtime):
    source = runtime.ingest_paper_source({'arxiv_id': '2610.12346', 'title': 'Operational memory research',
                                         'published_at': DATE}, scope=asdict(SCOPE))
    extraction = _extract(runtime, source.record_id)
    claim = runtime.store.get_by_exact_ref(extraction.claims[0].claim_card_id, scope=SCOPE, source_id='default')
    claim.content['confidence'] = claim.meta['confidence'] = 0.92
    runtime.store.rewrite(claim)
    compilation = runtime.compile_paper_knowledge(extraction=extraction, scope=asdict(SCOPE))
    page = runtime.store.get_by_exact_ref(compilation.pages[0].knowledge_page_id, scope=SCOPE, source_id='default')
    page.content['confidence'] = page.meta['confidence'] = 0.92
    runtime.store.rewrite(page)
    return source, extraction, claim, page


def _withdraw(runtime, source, status='deprecated'):
    current = runtime.store.get_by_exact_ref(source.record_id, scope=source.scope, source_id=source.source_id)
    current.status = status
    return runtime.store.rewrite(current)


def _recall(runtime, kind):
    return runtime.memory.recall(query=TEXT, scope=asdict(SCOPE), limit=8, task_context={
        'task_type': 'research' if kind != 'memory' else '', 'kinds': [kind], 'exact_scope_only': True,
        'include_operational_recall': True, 'include_digest_pages': True,
        'allowed_recall_lanes': ['external_knowledge', 'durable_fact'],
    })


@pytest.mark.parametrize('status', ['deprecated', 'rejected', 'quarantined'])
def test_withdrawal_removes_derived_digest_answer_context_and_daily_brief(tmp_path, status, paper_clock):
    with Runtime.create(root=tmp_path) as runtime:
        source, _, claim, page = _chain(runtime)
        assert runtime.build_research_digest(scope=asdict(SCOPE), digest_date=DATE)['claim_count'] == 1
        assert len(runtime.filter_answer_evidence([claim, page], task_type='research')['records']) == 2
        assert runtime.build_daily_brief(scope=asdict(SCOPE), date=DATE)['research_digest']['count'] >= 1
        _withdraw(runtime, source, status)

        digest = runtime.build_research_digest(scope=asdict(SCOPE), digest_date=DATE)
        assert digest['claim_count'] == 0
        assert digest['knowledge_page_count'] == 0
        assert source.record_id not in digest['source_ids']
        assert TEXT not in digest['summary']
        answer = runtime.filter_answer_evidence([claim, page], task_type='research')
        assert answer['records'] == []
        assert {row['reason'] for row in answer['evidence_gate']['excluded']} == {'paper_source_inactive'}
        assert runtime.build_daily_brief(scope=asdict(SCOPE), date=DATE)['research_digest']['count'] == 0


@pytest.mark.parametrize('status', ['deprecated', 'rejected', 'quarantined'])
def test_withdrawal_across_utc_midnight_gates_both_daily_brief_windows(tmp_path, status, paper_clock):
    next_day = '2026-10-25'
    before_midnight = f'{DATE}T23:59:59Z'
    after_midnight = f'{next_day}T00:00:01Z'
    paper_clock(before_midnight)
    with Runtime.create(root=tmp_path) as runtime:
        source, _, claim, page = _chain(runtime)
        assert all(row.time.created_at == before_midnight for row in (source, claim, page))
        assert runtime.build_daily_brief(scope=asdict(SCOPE), date=DATE)['research_digest']['count'] >= 1

        paper_clock(after_midnight)
        for day in (DATE, next_day):
            assert runtime.build_research_digest(scope=asdict(SCOPE), digest_date=day)['claim_count'] == 1
            brief = runtime.build_daily_brief(scope=asdict(SCOPE), date=day)
            assert brief['generated_at'] == after_midnight
            assert page.record_id in {row['record_id'] for row in brief['research_digest']['items']}
        assert len(runtime.filter_answer_evidence([claim, page], task_type='research')['records']) == 2

        _withdraw(runtime, source, status)

        for day in (DATE, next_day):
            digest = runtime.build_research_digest(scope=asdict(SCOPE), digest_date=day)
            assert digest['claim_count'] == 0
            assert digest['knowledge_page_count'] == 0
            assert source.record_id not in digest['source_ids']
            assert TEXT not in digest['summary']
            brief = runtime.build_daily_brief(scope=asdict(SCOPE), date=day)
            assert brief['research_digest']['count'] == 0
            excluded = {row['record_id']: row for row in brief['source_health']['evidence_gate']['excluded']}
            assert excluded[page.record_id]['reason'] == 'paper_source_inactive'
        answer = runtime.filter_answer_evidence([claim, page], task_type='research')
        assert answer['records'] == []
        assert {row['reason'] for row in answer['evidence_gate']['excluded']} == {'paper_source_inactive'}


@pytest.mark.parametrize('kind', ['claim_card', 'knowledge_page', 'memory'])
def test_explicit_recall_cannot_waive_parent_withdrawal(tmp_path, kind):
    with Runtime.create(root=tmp_path) as runtime:
        source, _, claim, page = _chain(runtime)
        if kind == 'memory':
            assert runtime.project_operational_knowledge(scope=asdict(SCOPE))['projected_count'] >= 1
        before = _recall(runtime, kind)
        assert before.items and all(row.kind == kind for row in before.items)
        _withdraw(runtime, source)

        after = _recall(runtime, kind)
        assert not after.items
        assert TEXT not in json.dumps(after.explanation)


def test_final_recall_rechecks_source_withdrawn_during_selection(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime, Runtime.create(root=tmp_path) as reviewer:
        source, _, claim, _ = _chain(runtime)
        engine = runtime.memory.recall_engine
        select = engine._select_post_fusion_items
        calls = []

        def withdraw_after_selection(items, **kwargs):
            selected, report = select(items, **kwargs)
            assert claim.record_id in {row.record_id for row in selected}
            _withdraw(reviewer, source)
            calls.append(True)
            return selected, report

        monkeypatch.setattr(engine, '_select_post_fusion_items', withdraw_after_selection)
        bundle = _recall(runtime, 'claim_card')
        assert calls
        assert not bundle.items
        assert bundle.explanation['retrieval_status'] == 'unavailable'
        assert TEXT not in json.dumps(bundle.explanation)


def test_manual_extraction_without_a_stored_parent_remains_usable(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        extraction = _extract(runtime, 'paper_manual_text')
        assert extraction.claims
        assert runtime.compile_paper_knowledge(extraction=extraction, scope=asdict(SCOPE)).pages
        assert runtime.build_research_digest(scope=asdict(SCOPE), digest_date=DATE)['claim_count'] == 1


@pytest.mark.parametrize('foreign', ['scope', 'source'])
def test_foreign_parent_does_not_govern_manual_exact_owner(tmp_path, foreign):
    with Runtime.create(root=tmp_path) as runtime:
        source = PaperSource(paper_source_id='paper_manual_text', source_kind='manual').to_record(scope=SCOPE)
        if foreign == 'scope':
            source.scope = ScopeRef(SCOPE.tenant_id, SCOPE.agent_id, SCOPE.workspace_id, 'another-owner')
        else:
            source.source_id = 'another-origin'
        source.status = 'deprecated'
        runtime.store.append(source)
        assert _extract(runtime, source.record_id).claims
        assert runtime.build_research_digest(scope=asdict(SCOPE), digest_date=DATE)['claim_count'] == 1


def test_manual_extraction_rejects_existing_withdrawn_source_before_writes(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        source = runtime.ingest_paper_source({'arxiv_id': '2610.12346'}, scope=asdict(SCOPE))
        _withdraw(runtime, source)
        with pytest.raises(PaperArtifactError, match='paper_source_not_eligible'):
            _extract(runtime, source.record_id)
        assert runtime.store.list_records(kinds=['claim_card', 'paper_extract'], scope=SCOPE) == []


def test_compilation_rejects_withdrawn_source_before_publishing_pages(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        source = runtime.ingest_paper_source({'arxiv_id': '2610.12346'}, scope=asdict(SCOPE))
        extraction = _extract(runtime, source.record_id)
        _withdraw(runtime, source)
        with pytest.raises(PaperArtifactError, match='paper_source_not_eligible'):
            runtime.compile_paper_knowledge(extraction=extraction, scope=asdict(SCOPE))
        assert runtime.store.list_records(kinds=['knowledge_page'], scope=SCOPE) == []


def test_projection_rechecks_source_parent_in_writer_transaction(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime, Runtime.create(root=tmp_path) as reviewer:
        source, _, _, _ = _chain(runtime)
        mutate = runtime.store.mutate_records_atomically

        def withdraw_before_write(mutation):
            _withdraw(reviewer, source)
            return mutate(mutation)

        monkeypatch.setattr(runtime.store, 'mutate_records_atomically', withdraw_before_write)
        report = runtime.project_operational_knowledge(scope=asdict(SCOPE))
        assert report['projected_count'] == 0
        assert 'paper_source_inactive' in {row['reason'] for row in report['skipped']}
        assert runtime.store.list_records(kinds=['memory'], scope=SCOPE) == []


def test_quarantined_canonical_source_cannot_extract_or_refresh(tmp_path):
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(_minimal_pdf(TEXT))
    with Runtime.create(root=tmp_path / 'runtime') as runtime:
        source = runtime.ingest_paper_source({'arxiv_id': '2610.12346', 'title': 'Runtime recall',
                                             'pdf_file': str(pdf)}, scope=asdict(SCOPE))
        extraction = runtime.extract_paper_source_memory(paper_source_id=source.record_id, scope=asdict(SCOPE))
        compilation = runtime.compile_paper_knowledge(extraction=extraction, scope=asdict(SCOPE))
        for page in compilation.to_records(scope=SCOPE):
            page.status = 'needs_refresh'
            runtime.store.rewrite(page)
        _withdraw(runtime, source, 'quarantined')
        with pytest.raises(PaperArtifactError, match='paper_source_not_eligible'):
            runtime.extract_paper_source_memory(paper_source_id=source.record_id, scope=asdict(SCOPE))
        report = runtime.refresh_knowledge_pages(scope=asdict(SCOPE))
        assert report['recompiled_page_count'] == 0
        assert report['blocked_page_count'] >= 1
        assert {row['reason'] for row in report['blocked']} == {'blocked_source_status_quarantined'}


def _closure(runtime, source):
    return build_research_closure_review(runtime, {'title': 'Runtime recall policy', 'content_excerpt': TEXT},
        {'paper_source_id': source.record_id, 'record_ids': []}, scope=SCOPE)['record_id']


def test_pending_closure_does_not_review_withdrawn_source(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        source, _, _, _ = _chain(runtime)
        closure_id = _closure(runtime, source)
        _withdraw(runtime, source)
        calls = []
        report = runtime.review_pending_research_closures(scope=asdict(SCOPE),
            executor=lambda *_: calls.append(True) or REVIEW)
        assert calls == []
        assert report['reviewed'] == 0 and report['unavailable'] == 1
        closure = runtime.store.get_by_exact_ref(closure_id, scope=SCOPE, source_id='default')
        assert closure.meta['review_error'] == 'paper_source_inactive'
        assert closure.content['model_review'] == ''
        assert runtime.filter_answer_evidence([closure], task_type='research')['records'] == []


def test_review_result_cannot_certify_source_withdrawn_during_execution(tmp_path):
    with Runtime.create(root=tmp_path) as runtime, Runtime.create(root=tmp_path) as reviewer:
        source, _, _, _ = _chain(runtime)
        closure_id = _closure(runtime, source)

        def withdraw_during_review(*_):
            _withdraw(reviewer, source)
            return REVIEW

        report = runtime.review_pending_research_closures(scope=asdict(SCOPE), executor=withdraw_during_review)
        assert report['reviewed'] == 0 and report['unavailable'] == 1
        closure = runtime.store.get_by_exact_ref(closure_id, scope=SCOPE, source_id='default')
        assert closure.meta['review_error'] == 'paper_source_inactive'
        assert closure.content['model_review'] == ''
        _withdraw(reviewer, source, 'active')
        assert runtime.retry_unavailable_research_closures(scope=asdict(SCOPE))['requeued'] == 1
        recovered = runtime.review_pending_research_closures(scope=asdict(SCOPE), executor=lambda *_: REVIEW)
        assert recovered['reviewed'] == 1 and recovered['unavailable'] == 0


def test_withdrawn_parent_outside_digest_scan_window_is_still_authoritative(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        source, _, _, _ = _chain(runtime)
        others = []
        for i in range(1000):
            row = RecordEnvelope.create(kind='paper_source', title=f'Unrelated source {i}', scope=SCOPE)
            row.time.created_at = row.time.updated_at = row.time.occurred_at = '2030-01-01T00:00:00Z'
            others.append(row)

        def add_others(sqlite):
            for row in others:
                sqlite.upsert(row, commit=False)
            return None, others, []

        runtime.store.mutate_records_atomically(add_others)
        _withdraw(runtime, source)
        assert source.record_id not in {row.record_id for row in runtime.store.list_records(
            kinds=['paper_source'], scope=SCOPE, limit=1000)}
        digest = runtime.build_research_digest(scope=asdict(SCOPE), digest_date=DATE)
        assert digest['claim_count'] == 0
        assert source.record_id not in digest['source_ids']


def test_secondary_paper_parent_is_checked_for_compiled_page(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        first = runtime.ingest_paper_source({'arxiv_id': '2610.12346'}, scope=asdict(SCOPE))
        second = runtime.ingest_paper_source({'arxiv_id': '2610.12347'}, scope=asdict(SCOPE))
        page = KnowledgePage(knowledge_page_id='review3-combined-page', page_type='topic', title='Combined evidence',
            summary=TEXT, source_ids=(first.record_id, second.record_id)).to_record(scope=SCOPE)
        page.content['confidence'] = 0.9
        runtime.store.append(page)
        assert runtime.filter_answer_evidence([page])['records']
        _withdraw(runtime, second)
        assert runtime.filter_answer_evidence([page])['records'] == []


def test_parent_query_failure_cannot_be_interpreted_as_legacy_absence(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        source, _, _, _ = _chain(runtime)
        assert _recall(runtime, 'claim_card').items
        get = runtime.store.sqlite.get_by_exact_ref

        def fail_parent(record_id, **kwargs):
            if record_id == source.record_id:
                raise OSError('private backend failure details')
            return get(record_id, **kwargs)

        monkeypatch.setattr(runtime.store.sqlite, 'get_by_exact_ref', fail_parent)
        bundle = _recall(runtime, 'claim_card')
        assert bundle.items == []
        assert bundle.explanation['relevance_selector']['dropped_reasons'] == {'paper_source_unavailable': 1}
        assert 'private backend' not in json.dumps(bundle.explanation)


def test_unreadable_existing_parent_is_not_legacy_absence(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        source, _, claim, _ = _chain(runtime)

        def damage_projection(sqlite):
            # Simulate a damaged/restored SQL projection: exact hydration must
            # reject the status disagreement with its canonical payload.
            sqlite.execute('UPDATE records SET status=? WHERE record_id=?', ('deprecated', source.record_id))
            return None, [], []

        runtime.store.mutate_records_atomically(damage_projection)
        assert runtime.store.get_by_exact_ref(source.record_id, scope=SCOPE, source_id='default') is None
        with pytest.raises(RuntimeError, match='exact_record_unavailable'):
            runtime.store.get_by_exact_ref(source.record_id, scope=SCOPE, source_id='default', fail_on_unavailable=True)
        assert runtime.store.get_by_exact_ref('missing-manual-parent', scope=SCOPE, source_id='default',
                                             fail_on_unavailable=True) is None
        answer = runtime.filter_answer_evidence([claim], task_type='research')
        assert answer['records'] == []
        assert answer['evidence_gate']['excluded'][0]['reason'] == 'paper_source_unavailable'


def test_promotion_final_commit_rechecks_an_unchanged_existing_source(tmp_path, monkeypatch):
    from eimemory.intake import pipeline
    from eimemory.intake.promotion_batch import _PaperPromotionBatch
    from test_intake_queue_review2_20261024 import paper_candidate, PAPER_SCOPE, PAPER_KINDS

    with Runtime.create(root=tmp_path) as runtime, Runtime.create(root=tmp_path) as reviewer:
        candidate = paper_candidate(runtime)
        payload = pipeline._defer_collected_remote_pdf_fetch(
            pipeline._paper_input_from_candidate(pipeline._candidate_payload(candidate)))
        source = runtime.ingest_paper_source(payload, scope=PAPER_SCOPE)
        publish = _PaperPromotionBatch.publish

        def withdraw_before_commit(batch):
            assert runtime.store.get_by_exact_ref(source.record_id, scope=source.scope,
                                                  source_id=source.source_id).to_dict() == source.to_dict()
            _withdraw(reviewer, source)
            return publish(batch)

        monkeypatch.setattr(_PaperPromotionBatch, 'publish', withdraw_before_commit)
        with pytest.raises(ValueError, match='paper_promotion_source_not_eligible'):
            runtime.promote_paper_candidate(candidate, scope=PAPER_SCOPE)
        current = runtime.store.get_by_exact_ref(candidate.record_id, scope=candidate.scope, source_id=candidate.source_id)
        assert current.status == 'candidate'
        assert [row.record_id for row in runtime.store.list_records(kinds=PAPER_KINDS, scope=PAPER_SCOPE)] == [source.record_id]


def test_derivative_write_failure_rolls_back_and_can_retry(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        source = runtime.ingest_paper_source({'arxiv_id': '2610.12346'}, scope=asdict(SCOPE))
        upsert = runtime.store.sqlite.upsert
        calls = []

        def fail_second(record, **kwargs):
            calls.append(record.record_id)
            if len(calls) == 2:
                raise OSError('synthetic derivative write failure')
            return upsert(record, **kwargs)

        monkeypatch.setattr(runtime.store.sqlite, 'upsert', fail_second)
        with pytest.raises(OSError, match='synthetic derivative write failure'):
            _extract(runtime, source.record_id)
        assert runtime.store.list_records(kinds=['claim_card', 'paper_extract'], scope=SCOPE) == []
        monkeypatch.setattr(runtime.store.sqlite, 'upsert', upsert)
        assert _extract(runtime, source.record_id).claims


def test_native_digest_with_101_sources_remains_recallable_with_bounded_parent_checks(tmp_path, monkeypatch):
    from eimemory.knowledge import evidence_gate

    with Runtime.create(root=tmp_path) as runtime:
        sources = []
        for i in range(101):
            source = runtime.ingest_paper_source({'arxiv_id': f'2610.{10000 + i}'}, scope=asdict(SCOPE))
            sources.append(source)
            _extract(runtime, source.record_id)
        digest = runtime.build_research_digest(scope=asdict(SCOPE), persist=True, digest_date=DATE)
        assert len(digest['source_ids']) == 101

        def recall():
            return runtime.memory.recall(query=f'Research digest {DATE}', scope=asdict(SCOPE), task_context={
                'task_type': 'research', 'kinds': ['knowledge_page'],
                'include_digest_pages': True, 'exact_scope_only': True,
            })

        assert [row.record_id for row in recall().items] == [digest['persisted_page_id']]
        # A too-small whole-pass budget must fail closed; it cannot silently
        # skip the remaining parents and authorize a partially checked digest.
        with monkeypatch.context() as limited:
            limited.setattr(evidence_gate, 'MAX_PAPER_SOURCE_LOOKUPS', 100)
            exhausted = recall()
            assert not exhausted.items
            assert exhausted.explanation['relevance_selector']['dropped_reasons'] == {'paper_source_unavailable': 1}
        _withdraw(runtime, max(sources, key=lambda record: record.record_id))
        withdrawn = recall()
        assert not withdrawn.items
        assert withdrawn.explanation['relevance_selector']['dropped_reasons'] == {'paper_source_inactive': 1}
