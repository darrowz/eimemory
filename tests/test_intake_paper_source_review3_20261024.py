"""Canonical source retries preserve verified evidence and durable withdrawal."""
from dataclasses import asdict

import pytest

from eimemory.api.runtime import Runtime
from eimemory.intake.papers import sources
from eimemory.intake.papers.artifacts import PaperArtifactError
from eimemory.models.records import ScopeRef
from test_paper_pdf_pipeline import _minimal_pdf


SCOPE = ScopeRef('review3', 'paper-source', 'local', 'owner')
TEXT = 'A reliable memory runtime must preserve source provenance for operational decisions.'


def _source(runtime, path):
    path.write_bytes(_minimal_pdf(TEXT))
    return runtime.ingest_paper_source({'arxiv_id': '2610.12345', 'title': 'Canonical research evidence',
                                       'abstract': TEXT, 'pdf_file': str(path)}, scope=asdict(SCOPE))


def test_metadata_replay_keeps_verified_canonical_source_usable(tmp_path):
    with Runtime.create(root=tmp_path / 'runtime') as runtime:
        original = _source(runtime, tmp_path / 'paper.pdf')
        runtime.extract_paper_source_memory(paper_source_id=original.record_id, scope=asdict(SCOPE))
        repeated = runtime.ingest_paper_source({'arxiv_id': '2610.12345'}, scope=asdict(SCOPE))
        assert repeated.to_dict() == original.to_dict()
        extraction = runtime.extract_paper_source_memory(paper_source_id=original.record_id, scope=asdict(SCOPE))
        assert extraction.claims


def test_reingest_cannot_reactivate_a_withdrawn_paper_source(tmp_path):
    with Runtime.create(root=tmp_path / 'runtime') as runtime:
        original = _source(runtime, tmp_path / 'paper.pdf')
        original.status = 'deprecated'
        runtime.store.rewrite(original)
        before = original.to_dict()
        with pytest.raises(PaperArtifactError, match='paper_source_not_eligible'):
            runtime.ingest_paper_source({'arxiv_id': '2610.12345'}, scope=asdict(SCOPE))
        current = runtime.store.get_by_exact_ref(original.record_id, scope=SCOPE, source_id=original.source_id)
        assert current.to_dict() == before


def test_concurrent_source_withdrawal_wins_over_artifact_retry(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    with Runtime.create(root=root) as runtime, Runtime.create(root=root) as reviewer:
        original = _source(runtime, tmp_path / 'paper.pdf')
        materialize = sources.materialize_paper_artifacts
        withdrawn = []

        def withdraw_during_preparation(root, payload):
            result = materialize(root, payload)
            record = reviewer.store.get_by_exact_ref(original.record_id, scope=SCOPE, source_id=original.source_id)
            record.status = 'deprecated'
            record.meta['review_reason'] = 'Withdrawal during canonical preparation'
            withdrawn.append(reviewer.store.rewrite(record).to_dict())
            return result

        monkeypatch.setattr(sources, 'materialize_paper_artifacts', withdraw_during_preparation)
        with pytest.raises(ValueError, match='paper_source_changed_retry_required'):
            runtime.ingest_paper_source({'arxiv_id': '2610.12345'}, scope=asdict(SCOPE))
        current = runtime.store.get_by_exact_ref(original.record_id, scope=SCOPE, source_id=original.source_id)
        assert current.to_dict() == withdrawn[-1]


def test_failed_explicit_update_does_not_discard_verified_artifacts(tmp_path):
    with Runtime.create(root=tmp_path / 'runtime') as runtime:
        original = _source(runtime, tmp_path / 'paper.pdf')
        invalid = tmp_path / 'invalid.pdf'
        invalid.write_bytes(b'invalid local PDF fixture')
        with pytest.raises(PaperArtifactError, match='paper_source_update_blocked'):
            runtime.ingest_paper_source({'arxiv_id': '2610.12345', 'pdf_file': str(invalid)}, scope=asdict(SCOPE))
        current = runtime.store.get_by_exact_ref(original.record_id, scope=SCOPE, source_id=original.source_id)
        assert current.to_dict() == original.to_dict()
        assert runtime.extract_paper_source_memory(paper_source_id=original.record_id, scope=asdict(SCOPE)).claims


def test_blocked_source_can_recover_after_a_successful_explicit_retry(tmp_path):
    path = tmp_path / 'paper.pdf'
    path.write_bytes(b'invalid local PDF fixture')
    with Runtime.create(root=tmp_path / 'runtime') as runtime:
        blocked = runtime.ingest_paper_source({'arxiv_id': '2610.12345', 'pdf_file': str(path)}, scope=asdict(SCOPE))
        assert blocked.status == 'blocked'
        path.write_bytes(_minimal_pdf(TEXT))
        recovered = runtime.ingest_paper_source({'arxiv_id': '2610.12345', 'pdf_file': str(path)}, scope=asdict(SCOPE))
        assert recovered.record_id == blocked.record_id
        assert recovered.status == 'active'
        assert recovered.content['metadata']['artifact']['status'] == 'ready'
        assert 'artifact_error_code' not in recovered.meta
        assert 'artifact_error_code' not in recovered.provenance
        assert runtime.extract_paper_source_memory(paper_source_id=recovered.record_id, scope=asdict(SCOPE)).claims
