"""Run in the complete patched repository; not executed by the review harness.

These checks import actual repository modules. Existing Runtime/RPC/promotion
behavior suites must also run before a release; this is not their replacement.
"""
from email.message import Message
import math

import pytest

from eimemory.core.strict_json import StrictJSONError, loads
from eimemory.adapters.runtime.http_boundary import bearer_matches, content_length, RequestBoundaryError
from eimemory.governance.promotion_manager import _score_value, _closed_loop_gate, _rollout_gate
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.intake.safe_transport import _normalize_headers
from eimemory.storage.independent_evidence import strict_json, CatalogError


@pytest.mark.parametrize('raw', ['NaN', 'Infinity', '1e309', '{"x":1,"x":2}'])
def test_rpc_json_invalid_numbers_and_duplicates(raw):
    with pytest.raises(StrictJSONError):
        loads(raw)


@pytest.mark.parametrize('raw', ['NaN', 'Infinity', '1e309', '{"x":1,"x":2}'])
def test_catalog_json_invalid_numbers_and_duplicates(raw):
    with pytest.raises(CatalogError):
        strict_json(raw)


@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf, 'NaN', True, 2, -1])
def test_normalized_score_is_finite_and_in_range(value):
    assert _score_value({'safety': value}, 'safety', default=0.0) == 0.0


def test_closed_loop_requires_actual_booleans():
    assert not _closed_loop_gate({'closed_loop': {'doctor': {'ok': 'false'}, 'smoke': {'ok': True}}})
    assert _closed_loop_gate({'closed_loop': {'doctor': {'ok': True}, 'smoke': {'ok': True}}})


def test_duplicate_content_length_rejected():
    headers = Message()
    headers['Content-Length'] = '10'
    headers['Content-Length'] = '10'
    with pytest.raises(RequestBoundaryError):
        content_length(headers, max_bytes=1000)


def test_non_ascii_auth_is_not_an_exception():
    headers = Message()
    headers['Authorization'] = 'Bearer café'
    assert not bearer_matches(headers, 'private-token')


def test_invalid_header_token_rejected():
    with pytest.raises(ValueError):
        _normalize_headers({'X Invalid': 'data'})


@pytest.mark.parametrize("tier", ["L0", "L1", "L2"])
def test_missing_safety_regression_scores_fail_closed(tier):
    """GOV-01: missing safety/regression must not default to a perfect score."""
    candidate = RecordEnvelope.create(
        kind="capability_candidate",
        title="gov01 missing scores",
        scope=ScopeRef(agent_id="hongtu"),
        content={"promotion_target": "pattern"},
        meta={"authority_tier": tier, "promotion_target": "pattern"},
    )
    gate = _rollout_gate(
        {"verdict": "pass", "scores": {"capability": 0.9}},
        {"ok": True},
        tier=tier,
        candidate=candidate,
    )
    assert gate["ok"] is False
    assert "safety_gate" in gate["blocked_reasons"]
    assert "regression_gate" in gate["blocked_reasons"]


def test_bearer_matches_accepts_mapping_headers():
    """RPC unit tests and synthetic callers may pass a plain dict."""
    assert bearer_matches({"Authorization": "Bearer private-token"}, "private-token")
    assert not bearer_matches({"Authorization": "Bearer other"}, "private-token")
    assert not bearer_matches({}, "private-token")


def test_content_length_accepts_mapping_headers():
    assert content_length({"Content-Length": "12"}, max_bytes=1000) == 12
