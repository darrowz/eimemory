"""Origin-aware circuit contracts; all transports and repositories are local fakes."""
import json
from urllib.error import URLError

import pytest

from eimemory.retrieval.contracts import CandidateBatch, CandidateRequest, ExactScope
from eimemory.retrieval.postgres_vector import (
    IndexState, OpenAICompatibleEmbeddingProvider, PostgresVectorCandidateSource,
    PostgresVectorConfig, PROJECTION_DIGEST_SCHEMA, projection_fingerprint,
)

SECRET = 'private://client:credential@never-contact.invalid'
GOOD = b'{"data":[{"embedding":[0.25]}]}'


def provider(transport=None, **overrides):
    options = dict(base_url='https://never-contact.invalid/v1', api_key='fake', model='fake',
        dimension=1, transport=transport or (lambda **kwargs: GOOD), failure_threshold=1)
    options.update(overrides)
    return OpenAICompatibleEmbeddingProvider(**options)


@pytest.mark.parametrize('case', ['empty', 'too_many', 'request_size', 'unconfigured', 'encoding'])
def test_local_provider_preflight_never_counts_backend_failure(case):
    calls = []
    p = provider(lambda **kwargs: calls.append(kwargs) or GOOD, max_batch=1, max_request_bytes=128)
    texts = {'empty': [], 'too_many': ['a', 'b'], 'request_size': ['x' * 200],
             'unconfigured': ['a'], 'encoding': ['\ud800']}[case]
    if case == 'unconfigured':
        p._api_key = ''
    with pytest.raises(RuntimeError) as caught:
        p.embed(texts)
    assert not calls
    assert p._circuit.failures == 0
    assert p.health()['circuit'] == 'closed'
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_local_provider_failure_preserves_real_failure_history():
    def unavailable(**kwargs):
        raise URLError(SECRET)
    p = provider(unavailable, failure_threshold=3)
    with pytest.raises(RuntimeError):
        p.embed(['valid'])
    assert p._circuit.failures == 1
    for _ in range(2):
        with pytest.raises(RuntimeError, match='embedding_batch_invalid'):
            p.embed([])
    assert p._circuit.failures == 1
    assert p.health()['circuit'] == 'closed'


def test_local_preflight_does_not_claim_or_extend_half_open_probe():
    clock = [0.0]
    calls = []
    def unavailable(**kwargs):
        calls.append(kwargs)
        raise URLError(SECRET)
    p = provider(unavailable, clock=lambda: clock[0], cooldown_seconds=1)
    with pytest.raises(RuntimeError):
        p.embed(['valid'])
    original_opened = p._circuit.opened_at
    clock[0] = 2.0
    with pytest.raises(RuntimeError, match='embedding_batch_invalid'):
        p.embed([])
    assert p._circuit.opened_at == original_opened
    assert p._circuit.failures == 1 and p._circuit.probe_active is False
    p._transport = lambda **kwargs: GOOD
    assert p.embed(['valid']) == [(0.25,)]
    assert len(calls) == 1


@pytest.mark.parametrize('body', [
    b'not json', b'\xff', b'{"data":{}}',
    b'{"data":[{"embedding":["not-a-number"]}]}',
    b'{"data":[{"embedding":[null]}]}',
    b'{"data":[{"embedding":["NaN"]}]}',
    b'{"data":[{"embedding":[1,2]}]}', b'x' * 129,
])
def test_malformed_provider_response_is_backend_even_for_valueerror(body):
    calls = []
    p = provider(lambda **kwargs: calls.append(kwargs) or body, max_response_bytes=128)
    with pytest.raises(RuntimeError) as caught:
        p.embed(['valid'])
    assert len(calls) == 1
    assert p._circuit.failures == 1
    assert p.health()['circuit'] == 'open'
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize('exc', [URLError(SECRET), TimeoutError(SECRET), ValueError(SECRET), TypeError(SECRET)])
def test_unknown_transport_failure_retains_backend_policy(exc):
    def transport(**kwargs): raise exc
    p = provider(transport)
    with pytest.raises(RuntimeError):
        p.embed(['valid'])
    assert p._circuit.failures == 1
    assert p.health()['circuit'] == 'open'
    assert SECRET not in json.dumps(p.health())


def test_short_request_timeout_still_preserves_preexisting_failures():
    def transport(**kwargs): raise TimeoutError(SECRET)
    p = provider(transport, failure_threshold=3, timeout_seconds=10)
    p._circuit.failure()
    with pytest.raises(RuntimeError, match='recall_budget_exhausted'):
        p.embed(['valid'], timeout_seconds=.1)
    assert p._circuit.failures == 1 and p._circuit.probe_active is False


class Local:
    def search(self, request): return CandidateBatch(hits=())


class FixedProvider:
    def fingerprint(self): return 'a' * 64
    def embed(self, texts, **kwargs): return [(0.25,) for _ in texts]
    def health(self): return dict(configured=True, available=True, circuit='closed', dimension=1)


def make_source(p=None, *, repository_error=None, local=None):
    p = p or FixedProvider()
    config = PostgresVectorConfig(enabled=True, dsn='unused', vector_dimension=1, failure_threshold=1)
    # Do not call the possibly failing identity implementation in test setup.
    fingerprint = p.fingerprint() if isinstance(p, OpenAICompatibleEmbeddingProvider) else 'a' * 64
    state = IndexState(ready=True, watermark='wm', lag_seconds=0,
        embedding_fingerprint=fingerprint, projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
        projection_fingerprint=projection_fingerprint(config))
    class Repository:
        def read_index_state(self, **kwargs): return state
        def search(self, *args, **kwargs):
            if repository_error: raise repository_error
            return []
    return PostgresVectorCandidateSource(sqlite_source=local or Local(), config=config,
        repository=Repository(), embedding_provider=p)


def query(text='hello'):
    return CandidateRequest(query=text, scope=ExactScope())


@pytest.mark.parametrize('exc', [TypeError(SECRET), ValueError(SECRET), KeyError(SECRET)])
@pytest.mark.parametrize('refresh', [False, True])
def test_known_local_fingerprint_setup_failure_does_not_trip_source(exc, refresh):
    class FailingIdentity(FixedProvider):
        def fingerprint(self): raise exc
    source = make_source(FailingIdentity())
    if refresh:
        assert source.refresh_index_identity(force=True) is False
    else:
        result = source.search(query())
        assert result.diagnostic_dict()['postgres']['state'] == 'bypassed'
        assert SECRET not in json.dumps(result.diagnostic_dict())
    assert source._circuit.failures == 0
    assert source.health()['circuit'] == 'closed'
    assert source.health()['query_valid'] is False
    assert SECRET not in json.dumps(source.health())


def test_known_local_cache_setup_typeerror_does_not_trip_source(monkeypatch):
    source = make_source()
    def invalid_cache(*args, **kwargs): raise TypeError(SECRET)
    monkeypatch.setattr(source, '_cache_get', invalid_cache)
    result = source.search(query())
    assert result.diagnostic_dict()['postgres']['state'] == 'bypassed'
    assert source._circuit.failures == 0


def test_known_local_authority_error_does_not_trip_postgres_backend():
    class UnavailableAuthority(Local):
        def authority_head(self): raise ValueError(SECRET)
    source = make_source(local=UnavailableAuthority())
    source.search(query())
    assert source._circuit.failures == 0
    assert source.health()['query_valid'] is False


def test_provider_local_origin_survives_public_code_sanitization_in_source():
    calls = []
    p = provider(lambda **kwargs: calls.append(kwargs) or GOOD, max_request_bytes=128)
    source = make_source(p)
    result = source.search(query('x' * 200))
    assert result.diagnostic_dict()['postgres']['error_code'] == 'request_too_large'
    assert source._circuit.failures == p._circuit.failures == 0
    assert not calls
    assert source.health()['query_valid'] is False


@pytest.mark.parametrize('body', [b'not json', b'{"data":[{"embedding":["bad"]}]}',
                                  b'{"data":[{"embedding":[1,2]}]}'])
def test_source_preserves_provider_owned_malformed_response_circuit_policy(body):
    p = provider(lambda **kwargs: body)
    source = make_source(p)
    result = source.search(query())
    assert result.diagnostic_dict()['postgres']['state'] == 'bypassed'
    assert p._circuit.failures == 1
    assert source._circuit.failures == 0
    assert source.health()['query_valid'] is False


@pytest.mark.parametrize('exc', [TypeError(SECRET), ValueError(SECRET), KeyError(SECRET)])
def test_unknown_repository_failure_is_not_blanket_exempted(exc):
    source = make_source(repository_error=exc)
    result = source.search(query())
    assert result.diagnostic_dict()['postgres']['state'] == 'bypassed'
    assert source._circuit.failures == 1
    assert SECRET not in json.dumps(result.diagnostic_dict())


def test_local_preflight_cannot_cancel_someone_elses_active_probe():
    clock = [0.0]
    p = provider(clock=lambda: clock[0], cooldown_seconds=1)
    p._circuit.failure()
    clock[0] = 2.0
    assert p._circuit.allow() and p._circuit.probe_active
    with pytest.raises(RuntimeError, match='embedding_batch_invalid'):
        p.embed([])
    assert p._circuit.probe_active is True
    assert p._circuit.failures == 1 and p._circuit.opened_at == 0
    p._circuit.cancel()


def test_source_local_failure_does_not_erase_real_failure_history_or_query_fence(monkeypatch):
    source = make_source()
    assert source.search(query()).diagnostic_dict()['postgres']['state'] == 'available'
    source._circuit.threshold = 3
    source._circuit.failure()
    def local_failure(*args, **kwargs): raise ValueError(SECRET)
    monkeypatch.setattr(source, '_cache_get', local_failure)
    source.search(query('next'))
    assert source._circuit.failures == 1
    assert source.health()['circuit'] == 'closed'
    assert source.health()['query_valid'] is False
    assert source.health()['available'] is False


def test_authority_budget_exception_remains_budget_not_local_backend(monkeypatch):
    from eimemory.storage.recall_deadline import RecallReadDeadlineExceeded
    source = make_source()
    source._circuit.failure()
    source._circuit.opened_at = None
    def expired(*args, **kwargs):
        raise RecallReadDeadlineExceeded('recall_budget_exhausted')
    monkeypatch.setattr(source, '_authority_snapshot', expired)
    result = source.search(query())
    assert result.diagnostic_dict()['postgres']['error_code'] == 'recall_budget_exhausted'
    assert source._circuit.failures == 1
    assert source.health()['query_valid'] is False
