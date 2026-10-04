"""Offline regressions for pre-existing arXiv identity and raw URL leakage."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
import pytest
from eimemory.intake.connectors import build_arxiv_api_url, collect_from_source_entry, parse_arxiv_xml


@pytest.mark.parametrize('uri, expected', [
    ('https://arxiv.org/abs/hep-th/9901001','hep-th/9901001'),
    ('https://arxiv.org/pdf/hep-th/9901001.pdf','hep-th/9901001'),
    ('https://arxiv.org/abs/math.GT/0309136v2','math.GT/0309136v2'),
    ('https://arxiv.org/pdf/math.GT/0309136v2.pdf','math.GT/0309136v2'),
    ('https://arxiv.org/abs/2401.12345v2','2401.12345v2'),
    ('https://arxiv.org/pdf/2401.12345v2.pdf','2401.12345v2'),
    ('2401.12345v2','2401.12345v2'),
    ('hep-th/9901001','hep-th/9901001'),
    ('hep-th/9901001v2','hep-th/9901001v2'),
    ('math.GT/0309136v2','math.GT/0309136v2'),
    ('arXiv:hep-th/9901001v2','hep-th/9901001v2'),
    ('https://arxiv.org/abs/2401.12345v2?context=cs#section','2401.12345v2'),
    ('https://arxiv.org/pdf/hep-th/9901001v2.pdf?download=1#page=2','hep-th/9901001v2'),
])
def test_arxiv_identity_is_complete_in_api_request(uri,expected):
    assert parse_qs(urlparse(build_arxiv_api_url(uri)).query)['id_list']==[expected]


@pytest.mark.parametrize('uri, expected', [
    ('https://arxiv.org/abs/hep-th/9901001','hep-th/9901001'),
    ('https://arxiv.org/abs/math.GT/0309136v2','math.GT/0309136v2'),
    ('https://arxiv.org/abs/2401.12345v2','2401.12345v2'),
])
def test_arxiv_identity_is_complete_in_parsed_metadata(uri,expected):
    feed=f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>{uri}</id><title>Offline title</title><summary>Offline abstract.</summary></entry></feed>'
    r=parse_arxiv_xml(feed)
    assert r.ok and r.items[0].metadata['arxiv_id']==expected


@pytest.mark.parametrize('primary_failure',['timeout','invalid_json','invalid_response'])
def test_chatpaper_final_failure_never_retains_credential_query_values(primary_failure):
    marker='SYNTHETIC_PRIVATE_QUERY_SENTINEL'
    source=SimpleNamespace(source_kind='url',uri=f'https://www.chatpaper.ai/api/papers/arxiv?category=cs.RO&api_key={marker}&access_token={marker}',metadata={})
    def fetch(url):
        if urlparse(url).hostname=='export.arxiv.org': raise TimeoutError(marker)
        if primary_failure=='timeout': raise TimeoutError(marker)
        if primary_failure=='invalid_json': return 'not JSON'
        return '{}'
    r=collect_from_source_entry(source,fetch_text=fetch)
    assert not r.ok and r.error=='fetch failed'
    assert marker not in repr(r)
    assert r.metadata['url']=='[redacted]'
    assert r.metadata['category_errors']
    assert all('http' not in str(error) for error in r.metadata['category_errors'])
