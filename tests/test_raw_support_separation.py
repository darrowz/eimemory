"""Consumer contracts, using stub verdicts; these are not semantic evaluations."""
from copy import deepcopy
import json

import pytest

from eimemory.recall.raw_citation import attach_compact_lane, collect_citation, render_citation
from test_raw_answer_support import Client
from test_raw_answer_support_consumer import configured, fetch
from test_runtime_raw_citation_delivery import (
    CANONICAL, QUERY, TEXT, add_raw, configure_proactive, delivery_expectation,
    final_context, owner, persona, proactive, runtime,
)


@pytest.mark.parametrize("status,reason", [
    ("supported", "direct_support"), ("unsupported", "missing_requested_fact"),
    ("unknown", "partial_support"),
])
def test_semantic_review_never_reorders_or_deletes_related_candidates(runtime, monkeypatch, status, reason):
    add_raw(runtime, scope=owner(CANONICAL))
    add_raw(runtime, "Halcyon deployment manifests have a separate rollback policy.", scope=owner(CANONICAL))
    monkeypatch.setenv("EIMEMORY_RAW_ANSWER_SUPPORT_ENABLED", "0")
    before = fetch(runtime)["bundle"]["raw_citation"]
    client = Client(lambda packet: {"request_digest": packet["request_digest"], "status": status,
        "reason": reason, "selected": [packet["passages"][-1]["id"]] if status == "supported" else []})
    configured(monkeypatch, client)
    result = fetch(runtime)
    after = result["bundle"]["raw_citation"]
    assert after["evidence"] == before["evidence"]
    assert after["answer_support"]["status"] == status
    assert len(after["support_evidence"]) == (1 if status == "supported" else 0)
    if status == "supported":
        assert after["support_evidence"][0]["ref"] == before["evidence"][-1]["ref"]
        assert result["context"].index("Model-reviewed support material:") < result["context"].index("Related material only;")
    else:
        assert '"evidence_role":"support_material"' not in result["context"]


def test_compact_budget_prioritizes_complete_support_then_retains_related_prefix(runtime, monkeypatch):
    add_raw(runtime, TEXT + " Additional deployment detail." * 28, scope=owner(CANONICAL))
    add_raw(runtime, "Halcyon deployment manifests have a short secondary source.", scope=owner(CANONICAL))
    configured(monkeypatch, Client(lambda packet: {"request_digest": packet["request_digest"],
        "status": "supported", "reason": "direct_support", "selected": [packet["passages"][-1]["id"]]}))
    lane = collect_citation(runtime.memory, query=QUERY, scope=owner(CANONICAL),
        context={"runtime_channel": "hermes", "source_ids": ["hermes"]})
    assert lane.answer_support["status"] == "supported"
    full = attach_compact_lane({}, lane, maximum_bytes=16000)["raw_citation"]
    full_bytes = len(json.dumps({"raw_citation": full}, ensure_ascii=False, separators=(",", ":")).encode()) + 192
    compact = attach_compact_lane({}, lane, maximum_bytes=full_bytes - 10)["raw_citation"]
    assert compact["answer_support"]["status"] == "supported"
    # Identity context may be necessary for a cross-document answer. Keep the
    # same complete support set; spend the remaining bytes on related rows.
    assert compact["support_evidence"] == full["support_evidence"]
    assert compact["evidence"] == full["evidence"][:len(compact["evidence"])]
    assert lane.evidence == full["evidence"]  # Transport fitting does not rewrite the original lane.
    assert lane.support_evidence == full["support_evidence"]


@pytest.mark.parametrize("field", ["evidence", "support_evidence"])
def test_expiry_in_either_section_withholds_both_at_render(runtime, monkeypatch, field):
    add_raw(runtime, scope=owner(CANONICAL))
    configured(monkeypatch, Client())
    lane = fetch(runtime)["bundle"]["raw_citation"]
    public = deepcopy(lane)
    public[field][0]["applicability"] = {"valid_until": "2000-01-01T00:00:00+00:00"}
    rendered = render_citation("Independent L1", public, max_chars=16000)
    assert "infrastructure repository" not in rendered
    assert "Independent L1" in rendered
    assert public["answer_support"]["status"] == "unknown"
    assert not public["support_evidence"]
    assert public["presentation"]["evidence_count"] == public["presentation"]["support_evidence_count"] == 0


def test_support_section_is_bound_into_cached_sidecar_bytes(runtime, monkeypatch):
    add_raw(runtime, scope=owner(CANONICAL))
    persona(runtime, CANONICAL)
    configure_proactive(runtime)
    configured(monkeypatch, Client())
    payload = proactive(runtime, base=CANONICAL)
    assert payload["bundle"]["raw_citation"]["support_evidence"]
    assert "selected by a model" in final_context(runtime, payload, base=CANONICAL)
    tampered = deepcopy(payload)
    tampered["bundle"]["raw_citation"]["support_evidence"][0]["text"] += " injected claim"
    expected = delivery_expectation(runtime, base=CANONICAL)
    rendered = final_context(runtime, tampered, base=CANONICAL, expected_authority=expected)
    assert "selected by a model" not in rendered and "infrastructure repository" not in rendered
    assert "Prefer concise" in rendered


def test_supported_heading_requires_every_selected_support_window_even_when_related_survives(runtime, monkeypatch):
    add_raw(runtime, scope=owner(CANONICAL))
    add_raw(runtime, "Halcyon deployment manifests also require the rollback source.", scope=owner(CANONICAL))
    configured(monkeypatch, Client(lambda packet: {"request_digest": packet["request_digest"],
        "status": "supported", "reason": "direct_support", "selected": [p["id"] for p in packet["passages"]]}))
    public = fetch(runtime)["bundle"]["raw_citation"]
    related = deepcopy(public["evidence"])
    public["support_evidence"].pop()
    rendered = render_citation("", public, max_chars=16000)
    assert public["evidence"] == related
    assert public["answer_support"]["status"] == "unknown"
    assert not public["support_evidence"]
    assert "selected by a model" not in rendered
    assert '"evidence_role":"related_material"' in rendered
