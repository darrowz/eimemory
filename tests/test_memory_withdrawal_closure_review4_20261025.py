"""Real authority withdrawals govern every daily brief body and its delivery copy."""
from dataclasses import asdict, replace
from datetime import datetime
import json

import pytest

from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.core import clock
from eimemory.knowledge.evidence_contracts import versioned_record_ref
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = ScopeRef("review4", "hongtu", "daily-withdrawal", "owner")
PRIVATE = "WITHDRAWN_MEMORY_WITNESS"
CURRENT = "CURRENT_MEMORY_WITNESS"
PAPER = "WITHDRAWN_PAPER_WITNESS"


@pytest.fixture(autouse=True)
def brief_clock(monkeypatch):
    class FixtureDatetime:
        @staticmethod
        def now(tz):
            return datetime.fromisoformat("2026-10-25T12:00:00+00:00").astimezone(tz)

    monkeypatch.setattr(clock, "datetime", FixtureDatetime)


def _text(marker):
    return f"Decision: verify {marker} for operational memory recall; Follow up: review its exact evidence."


def _memory(runtime, marker):
    return runtime.memory.ingest(
        text=_text(marker), memory_type="conversation", title="Daily evidence decision",
        scope=asdict(SCOPE), source="review4.memory", force_capture=True,
    )


def _current(runtime, record):
    return runtime.store.get_by_exact_ref(record.record_id, scope=record.scope,
                                          source_id=record.source_id)


def _brief(runtime, scope, day, *, persist=False):
    return runtime.build_daily_brief(scope=asdict(scope), date=day, persist=persist)


def _assert_sections(brief, marker, *, present):
    for section in ("conversation_summary", "new_memories", "decisions", "followups"):
        assert (marker in json.dumps(brief[section])) is present, section
        delivery_section = brief["delivery"]["outbox"]["body"][section]
        assert (marker in json.dumps(delivery_section)) is present, f"delivery.{section}"
    if not present:
        # Diagnostics may retain identity/counts, never a second copy of a
        # rejected title or body that bypasses the user-visible sections.
        assert marker not in json.dumps(brief)


def test_hermes_remove_clears_all_brief_sections_and_delivery_after_reopen(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        service = AgentRuntimeMemoryService(runtime)
        added = service.mutate_memory(
            channel="hermes", scope=asdict(SCOPE), source_id="hermes", target="memory",
            action="add", content=_text(PRIVATE), idempotency_key="brief-original",
            provenance={"write_origin": "hermes.memory_write", "session_id": "brief-session"},
        )
        assert added["ok"] is True
        scope = ScopeRef.from_dict(added["record"]["scope"])
        day = added["record"]["time"]["occurred_at"][:10]
        _assert_sections(_brief(runtime, scope, day), PRIVATE, present=True)
        removed = service.mutate_memory(
            channel="hermes", scope=asdict(scope), source_id="hermes", target="memory",
            action="remove", content="", idempotency_key="brief-remove",
            target_record_id=added["record"]["record_id"], expected_revision=added["content_revision"],
            provenance={"write_origin": "hermes.memory_write", "session_id": "brief-session"},
        )
        assert removed["ok"] is True
        after = _brief(runtime, scope, day)
        _assert_sections(after, PRIVATE, present=False)
        assert after["source_health"]["by_status"]["removed"] == 2
    with Runtime.create(root=tmp_path) as reopened:
        retired = reopened.store.get_by_exact_ref(added["record"]["record_id"], scope=scope, source_id="hermes")
        assert retired.status == "removed" and PRIVATE in retired.content["text"]
        _assert_sections(_brief(reopened, scope, day), PRIVATE, present=False)


@pytest.mark.parametrize("status", ["removed", "superseded", "rejected", "quarantined",
                                    "deprecated", "expired", "refuted", "inactive", "conflicted"])
def test_rewritten_inactive_memory_cannot_populate_any_brief_body(tmp_path, status):
    with Runtime.create(root=tmp_path) as runtime:
        original = _memory(runtime, PRIVATE)
        active = _memory(runtime, CURRENT)
        day = original.time.occurred_at[:10]
        _assert_sections(_brief(runtime, original.scope, day), PRIVATE, present=True)
        original = _current(runtime, original)
        original.status = status
        runtime.store.rewrite(original)
        after = _brief(runtime, original.scope, day)
        _assert_sections(after, PRIVATE, present=False)
        _assert_sections(after, CURRENT, present=True)
        assert after["source_health"]["by_status"][status] == 1
        assert _current(runtime, active).status == "active"


@pytest.mark.parametrize("metadata", [{"mutation_state": "removed"}, {"superseded_by": "mem_successor"}])
def test_active_envelope_with_retired_memory_metadata_is_not_resummarized(tmp_path, metadata):
    with Runtime.create(root=tmp_path) as runtime:
        original = _memory(runtime, PRIVATE)
        day = original.time.occurred_at[:10]
        _assert_sections(_brief(runtime, original.scope, day), PRIVATE, present=True)
        original = _current(runtime, original)
        original.meta.update(metadata)
        runtime.store.rewrite(original)
        assert _current(runtime, original).status == "active"
        _assert_sections(_brief(runtime, original.scope, day), PRIVATE, present=False)


@pytest.mark.parametrize("status", ["deprecated", "rejected", "quarantined"])
def test_paper_withdrawal_clears_projected_memory_and_followup_bypasses(tmp_path, status):
    with Runtime.create(root=tmp_path) as runtime:
        source = runtime.ingest_paper_source(
            {"arxiv_id": "2610.12348", "title": "Operational memory evidence",
             "abstract": _text(PAPER), "published_at": "2026-10-25"}, scope=asdict(SCOPE),
        )
        extraction = runtime.extract_paper_memory(
            {"paper_source_id": source.record_id, "title": "Operational memory evidence",
             "body": _text(PAPER), "provenance": {"published_at": "2026-10-25"}},
            scope=asdict(SCOPE),
        )
        assert extraction.claims
        for claim in extraction.claims:
            current = runtime.store.get_by_exact_ref(claim.claim_card_id, scope=source.scope, source_id=source.source_id)
            current.content["confidence"] = current.meta["confidence"] = 0.92
            runtime.store.rewrite(current)
        compilation = runtime.compile_paper_knowledge(extraction=extraction, scope=asdict(SCOPE))
        assert compilation.pages
        for page in compilation.pages:
            current = runtime.store.get_by_exact_ref(page.knowledge_page_id, scope=source.scope, source_id=source.source_id)
            current.content["confidence"] = current.meta["confidence"] = 0.92
            runtime.store.rewrite(current)
        projected = runtime.project_operational_knowledge(scope=asdict(SCOPE))
        assert projected["projected_count"] >= 1
        day = source.time.occurred_at[:10]
        before = _brief(runtime, source.scope, day)
        _assert_sections(before, PAPER, present=True)
        assert before["research_digest"]["count"] >= 1
        source = _current(runtime, source)
        source.status = status
        runtime.store.rewrite(source)
        after = _brief(runtime, source.scope, day)
        _assert_sections(after, PAPER, present=False)
        assert after["research_digest"]["count"] == 0
    with Runtime.create(root=tmp_path) as reopened:
        assert _current(reopened, source).status == status
        _assert_sections(_brief(reopened, source.scope, day), PAPER, present=False)


def test_brief_retains_legitimate_accepted_rule_followups(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        rule = runtime.store.append(RecordEnvelope.create(
            kind="rule", status="accepted", title="Reviewed evidence policy",
            summary="Follow up: retain the accepted operational review policy.",
            scope=SCOPE, source="review4.rule",
        ))
        report = _brief(runtime, rule.scope, rule.time.occurred_at[:10])
        assert [item["record_id"] for item in report["followups"]] == [rule.record_id]
        assert report["new_memories"] == []
        assert report["source_health"]["by_status"] == {"accepted": 1}


def test_persisted_brief_binds_only_actual_body_inputs_with_exact_versions(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        active = _memory(runtime, CURRENT)
        removed = _memory(runtime, PRIVATE)
        removed.status = "removed"
        runtime.store.rewrite(removed)
        rule = runtime.store.append(RecordEnvelope.create(
            kind="rule", status="accepted", title="Reviewed evidence policy",
            summary="Follow up: retain the accepted operational review policy.",
            scope=active.scope, source="review4.rule", source_id="review-policy",
        ))
        health_only = runtime.store.append(RecordEnvelope.create(
            kind="unknown", status="candidate", title="Pending operator review",
            summary="This pending item has no committed decision or followup.",
            scope=active.scope, source="review4.health",
        ))
        day = active.time.occurred_at[:10]
        report = _brief(runtime, active.scope, day, persist=True)
        assert report["persisted"] is True
        _assert_sections(report, PRIVATE, present=False)
        expected = [versioned_record_ref(_current(runtime, item)) for item in (active, rule)]
        expected.sort(key=lambda ref: (*ref["scope"].values(), ref["source_id"], ref["record_id"]))
        assert report["source_lineage_schema"] == "daily_brief_sources.v1"
        assert report["source_record_refs"] == expected
        assert {ref["record_id"] for ref in expected}.isdisjoint({removed.record_id, health_only.record_id})
        stored_id = report["persisted_record_id"]
        stored = runtime.store.get_by_exact_ref(stored_id, scope=active.scope, source_id="default")
        assert stored.content["source_record_refs"] == stored.content["brief"]["source_record_refs"] == expected
        assert stored.content["delivery"]["outbox"]["body"]["source_record_refs"] == expected
        assert stored.content["source_lineage_schema"] == stored.provenance["source_lineage_schema"] == "daily_brief_sources.v1"
    with Runtime.create(root=tmp_path) as reopened:
        stored = reopened.store.get_by_exact_ref(stored_id, scope=active.scope, source_id="default")
        assert stored.content["source_record_refs"] == expected
        assert [versioned_record_ref(_current(reopened, item)) for item in (active, rule)] == [
            next(ref for ref in expected if ref["record_id"] == item.record_id) for item in (active, rule)]


def test_inherited_global_user_input_is_valid_until_withdrawal_and_after_reopen(tmp_path):
    global_scope = replace(SCOPE, user_id="")
    marker = "SHARED_INPUT_WITNESS"
    with Runtime.create(root=tmp_path) as runtime:
        shared = runtime.memory.ingest(
            text=_text(marker), memory_type="conversation", title="Shared daily decision",
            scope=asdict(global_scope), source="review4.shared", source_id="shared-origin",
            force_capture=True,
        )
        local = _memory(runtime, CURRENT)
        foreign = runtime.store.append(RecordEnvelope.create(
            kind="memory", title="Another user's decision", summary=_text("FOREIGN_USER_WITNESS"),
            scope=replace(SCOPE, user_id="another-user"), source="review4.shared",
        ))
        day = shared.time.occurred_at[:10]
        report = _brief(runtime, SCOPE, day, persist=True)
        _assert_sections(report, marker, present=True)
        assert {row["record_id"] for row in report["new_memories"]} == {shared.record_id, local.record_id}
        assert foreign.record_id not in {ref["record_id"] for ref in report["source_record_refs"]}
        assert versioned_record_ref(_current(runtime, shared)) in report["source_record_refs"]
        saved_id = report["persisted_record_id"]
        saved = runtime.store.get_by_exact_ref(saved_id, scope=SCOPE, source_id="default")
        assert runtime.filter_answer_evidence([saved])["records"] == [saved]
        global_report = _brief(runtime, global_scope, day)
        assert [row["record_id"] for row in global_report["new_memories"]] == [shared.record_id]

        with Runtime.create(root=tmp_path) as reviewer:
            current = _current(reviewer, shared)
            current.status = "removed"
            reviewer.store.rewrite(current)
        # The caller's old active envelope and its persisted derivative must
        # both be rejected after the separate authority writer withdraws it.
        assert shared.status == saved.status == "active"
        assert runtime.filter_answer_evidence([shared])["records"] == []
        answer = runtime.filter_answer_evidence([saved])
        assert answer["records"] == []
        assert answer["evidence_gate"]["excluded"][0]["reason"] == "source_evidence_inactive"
        _assert_sections(_brief(runtime, SCOPE, day), marker, present=False)
        _assert_sections(_brief(runtime, SCOPE, day), CURRENT, present=True)
    with Runtime.create(root=tmp_path) as reopened:
        saved = reopened.store.get_by_exact_ref(saved_id, scope=SCOPE, source_id="default")
        assert saved.status == "active"  # Historical storage is still intact.
        assert reopened.filter_answer_evidence([saved])["records"] == []
        _assert_sections(_brief(reopened, SCOPE, day), marker, present=False)
