"""Real service/host replay across original evidence validity boundaries.

Only the applicability/delivery wall clock is controlled. Retrieval, canonical
storage, RPC metadata issuance, sidecar byte binding and host consumption are
the production implementations. No production store or network is accessed.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from eimemory.raw import applicability
from eimemory.raw.citation_gate import CitationUnavailable
from eimemory.recall import raw_citation
from test_runtime_raw_citation_delivery import (
    CANONICAL, add_raw, configure_proactive, delivery_expectation, final_context,
    owner, persona, persist_mutation, proactive, runtime,
)


QUERY = "Where are Summit deployment manifests stored?"
MARKER = "short-lived repository"
START = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
EXPIRES = START + timedelta(minutes=1)
TEXT = ("Valid from: 2026-11-17T11:00:00Z\n"
        "Valid until: 2026-11-17T12:01:00Z\n"
        "Summit deployment manifests are stored in the short-lived repository.")


@pytest.fixture
def wall_clock(monkeypatch):
    class Clock(datetime):
        current = START

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(applicability, "datetime", Clock)
    # The consumer owns the final time-of-delivery check, independently of
    # service collection. Allow the pre-fix module to have no clock yet.
    monkeypatch.setattr(raw_citation, "datetime", Clock, raising=False)
    return Clock


def cached_payload(runtime, query=QUERY):
    persona(runtime, CANONICAL,
        text="Prefer concise Summit deployment manifests reports with primary evidence.")
    record = add_raw(runtime, TEXT, scope=owner(CANONICAL))
    configure_proactive(runtime)
    result = proactive(runtime, query=query, base=CANONICAL)
    assert result["bundle"]["raw_citation"]["evidence"]
    assert MARKER in result["raw_citation_context"]
    return record, result


@pytest.mark.parametrize("consume_before_expiry", [False, True], ids=["first_delivery", "repeat_delivery"])
def test_old_payload_with_fresh_policy_metadata_cannot_deliver_expired_source(runtime, wall_clock,
                                                                            consume_before_expiry):
    record, payload = cached_payload(runtime)
    if consume_before_expiry:
        assert MARKER in final_context(runtime, payload, query=QUERY, base=CANONICAL)
    wall_clock.current = EXPIRES + timedelta(seconds=1)
    current = applicability.assess_applicability(record, query=QUERY)
    assert (current.status, current.reason) == ("not_applicable", "validity_expired")
    # This is a real fresh metadata RPC, not a stale/copied service expectation.
    expected = delivery_expectation(runtime, query=QUERY, base=CANONICAL)
    delivered = final_context(runtime, payload, query=QUERY, base=CANONICAL,
                              expected_authority=expected)
    assert MARKER not in delivered
    assert "Prefer concise Summit" in delivered
    # Service retry already re-collects RAW; protect that working path too.
    refreshed = proactive(runtime, query=QUERY, base=CANONICAL)
    assert MARKER not in final_context(runtime, refreshed, query=QUERY, base=CANONICAL)


def test_expiry_field_is_covered_by_existing_sidecar_binding(runtime, wall_clock):
    _, payload = cached_payload(runtime)
    assert MARKER in final_context(runtime, payload, query=QUERY, base=CANONICAL)
    tampered = deepcopy(payload)
    proof = tampered["bundle"]["raw_citation"]["evidence"][0]["applicability"]
    assert proof["valid_until"]
    proof["valid_until"] = "2099-01-01T00:00:00+00:00"
    expected = delivery_expectation(runtime, query=QUERY, base=CANONICAL)
    delivered = final_context(runtime, tampered, query=QUERY, base=CANONICAL,
                              expected_authority=expected)
    assert MARKER not in delivered
    assert "Prefer concise Summit" in delivered


def test_historical_as_of_source_does_not_expire_against_delivery_wall_clock(runtime, wall_clock):
    query = "As of 2026-11-17T12:00:00Z, where are Summit deployment manifests stored?"
    record, payload = cached_payload(runtime, query)
    wall_clock.current = EXPIRES + timedelta(days=1)
    assert applicability.assess_applicability(record, query=query).status == "applicable"
    expected = delivery_expectation(runtime, query=query, base=CANONICAL)
    delivered = final_context(runtime, payload, query=query, base=CANONICAL,
                              expected_authority=expected)
    assert MARKER in delivered
    assert "Prefer concise Summit" in delivered


@pytest.mark.parametrize("change", ["withdrawal", "source_text"], ids=["withdrawn_source", "changed_source"])
def test_cached_payload_cannot_replay_after_canonical_source_changes(runtime, wall_clock, change):
    record, payload = cached_payload(runtime)
    assert MARKER in final_context(runtime, payload, query=QUERY, base=CANONICAL)
    replacement_marker = "replacement repository"
    if change == "withdrawal":
        record.status = "withdrawn"
    else:
        replacement = TEXT.replace(MARKER, replacement_marker)
        record.content["raw_text"] = record.content["text"] = replacement
        record.summary = record.detail = replacement
    persist_mutation(runtime, record)
    expected = delivery_expectation(runtime, query=QUERY, base=CANONICAL)
    delivered = final_context(runtime, payload, query=QUERY, base=CANONICAL,
                              expected_authority=expected)
    assert MARKER not in delivered
    assert "Prefer concise Summit" in delivered
    refreshed = proactive(runtime, query=QUERY, base=CANONICAL)
    fresh = final_context(runtime, refreshed, query=QUERY, base=CANONICAL)
    assert MARKER not in fresh
    if change == "source_text":
        assert replacement_marker in fresh


def test_unavailable_warning_without_evidence_does_not_require_a_snapshot(runtime, wall_clock, monkeypatch):
    persona(runtime, CANONICAL,
        text="Prefer concise Summit deployment manifests reports with primary evidence.")
    add_raw(runtime, TEXT, scope=owner(CANONICAL))
    configure_proactive(runtime)

    def incomplete(**kwargs):
        raise CitationUnavailable("collection_incomplete")

    monkeypatch.setattr(runtime.store, "read_raw_citation_records", incomplete)
    payload = proactive(runtime, query=QUERY, base=CANONICAL)
    lane = payload["bundle"]["raw_citation"]
    assert lane["admission"]["status"] == "unavailable" and not lane["evidence"]
    expected = delivery_expectation(runtime, query=QUERY, base=CANONICAL)
    delivered = final_context(runtime, payload, query=QUERY, base=CANONICAL,
                              expected_authority=expected)
    assert MARKER not in delivered
    assert "citation check unavailable" in delivered
    assert "Prefer concise Summit" in delivered
