"""Independent second-review counterexamples through the public ranking API."""
import pytest
from eimemory.raw.retrieval import rerank_raw_results


def row(name, text, score, day):
    return {'record': {'record_id': name, 'kind': 'raw_chunk', 'text': text,
                       'occurred_at': day + 'T00:00:00Z'}, 'base_score': score}


@pytest.mark.parametrize('new_text', [
    'Alice archive backend was SQLite. Correction: Bob archive backend changed to MySQL.',
    'Alice archive backend was SQLite.\nBob currently uses MySQL.',
    'Alice archive backend was SQLite. Bob currently uses MySQL.',
])
def test_unrelated_statement_cannot_lend_correction_or_current_marker(new_text):
    out = rerank_raw_results(query='Alice archive backend', results=[
        row('right', 'Alice archive backend is PostgreSQL.', .9, '2026-01-01'),
        row('wrong', new_text, .1, '2026-02-01')])
    assert out[0]['record']['record_id'] == 'right'


def test_chinese_sentence_boundary_does_not_merge_subjects():
    out = rerank_raw_results(query='阿辰 archive backend', results=[
        row('right', '阿辰 archive backend 是 PostgreSQL。', .9, '2026-01-01'),
        row('wrong', '阿辰 archive backend 原为 SQLite。更正：阿熙 archive backend 改为 MySQL。', .1, '2026-02-01')])
    assert out[0]['record']['record_id'] == 'right'


def test_calendar_shaped_numeric_identifier_is_not_a_historical_request():
    out = rerank_raw_results(query='What is ticket 2025 status?', results=[
        row('old', 'Ticket 2025 status is open.', .9, '2026-01-01'),
        row('new', 'Correction: ticket 2025 status changed to resolved.', .1, '2026-02-01')])
    assert out[0]['record']['record_id'] == 'new'
