"""Numeric usage telemetry is not permission to store credential-like fields."""
import pytest
from eimemory.experience.sanitize import OutcomeSanitizationError, sanitize_outcome_payload


@pytest.mark.parametrize('key', ['clientSecret', 'api_key_value', 'refresh_token_value',
                                 'authorizationHeader', 'password_hash', 'accessToken'])
def test_secret_key_variants_remain_rejected(key):
    with pytest.raises(OutcomeSanitizationError):
        sanitize_outcome_payload({key: 'synthetic-fixture-value'})


def test_numeric_token_count_is_retained():
    assert sanitize_outcome_payload({'usage': {'token_count': 23}}) == {'usage': {'token_count': 23}}


@pytest.mark.parametrize('value', ['synthetic-fixture-value', True, -1, {'value': 23}])
def test_token_count_exception_is_narrowly_typed(value):
    with pytest.raises(OutcomeSanitizationError):
        sanitize_outcome_payload({'token_count': value})
