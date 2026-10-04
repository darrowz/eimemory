import pytest
from eimemory.knowledge.synthesis import build_research_digest
from eimemory.knowledge.evidence_gate import grade_research_evidence
from eimemory.models.records import RecordEnvelope, ScopeRef


def claim(confidence, *, status='active'):
    return RecordEnvelope.create(kind='claim_card', title='fixture', status=status, scope=ScopeRef(),
        content={'confidence': confidence, 'source_url': 'https://example.test/fixture', 'published_at': '2026-10-01', 'claim_text': 'Verified memory evidence improves runtime recall.'})


@pytest.mark.parametrize('value', ['garbage', None, True, float('nan'), float('inf'), -1, 2])
def test_excluded_malformed_claim_cannot_crash_viable_digest(value):
    good, bad = claim(.72), claim(value)
    digest = build_research_digest(paper_sources=[], claim_cards=[bad, good], knowledge_pages=[])
    assert digest['ok']
    assert [item['claim_id'] for item in digest['notable_claims']] == [good.record_id]
    assert digest['claim_count'] == 1


def test_rejected_claim_is_not_republished():
    bad = claim(.9, status='rejected')
    assert not grade_research_evidence(bad)['ok']
    assert not build_research_digest(paper_sources=[], claim_cards=[bad], knowledge_pages=[])['ok']


def test_confidence_zero_cannot_fall_back_to_high_meta():
    bad = claim(0)
    bad.meta['confidence'] = .9
    digest = build_research_digest(paper_sources=[], claim_cards=[bad], knowledge_pages=[])
    assert not digest['ok']
