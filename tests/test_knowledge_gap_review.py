"""Native source/registry/approval/review persistence; no live model calls."""
from dataclasses import replace
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.capability import knowledge_gap_review as review
from eimemory.governance.capability.hypothesis_producer import (
    produce_capability_hypotheses, revoke_produced_hypothesis, revoked_hypothesis_ids,
)
from eimemory.governance.capability.capability_hypotheses import list_capability_hypotheses, hypothesis_behavior_gate
from eimemory.governance.evolution.dynamic_capability_evolution import build_dynamic_capability_evolution_plan
from eimemory.intake.papers.artifacts import materialize_paper_artifacts
from eimemory.intake.review import mark_candidate_paper_promoted, review_candidate
from eimemory.knowledge.capabilities import list_registered_knowledge_links
from eimemory.knowledge.source_trust import resolve_source_trust
from eimemory.models.paper_sources import PaperSource
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage import knowledge_review_inventory as inventory
from eimemory.storage.jsonl import canonical_payload_json, payload_digest
from test_dynamic_capability_evals import SCOPE, _definition, _revision, _binding, _profile, _catalog
from test_paper_pdf_pipeline import _minimal_pdf


TEXT = "Canonical operational evidence shows that bounded decisions preserve traceable source references."


@pytest.fixture
def registered(tmp_path):
    pytest.importorskip("pypdf")
    runtime = Runtime.create(root=tmp_path / "runtime")
    definition = _definition()
    revision = _revision(definition)
    runtime.capabilities.register_definition(definition, runtime_scope=SCOPE)
    runtime.capabilities.register_revision(revision, runtime_scope=SCOPE)
    runtime.capabilities.bind(_binding(definition, revision), runtime_scope=SCOPE)
    runtime.capabilities.register_profile(_profile(definition), runtime_scope=SCOPE)
    yield runtime, _profile(definition), _catalog(), tmp_path
    runtime.close()


def _evidence(registered, *, suffix="1", approved=True, scope=None, context_revision=None):
    runtime, profile, catalog, path = registered
    scope = ScopeRef.from_dict(scope or SCOPE)
    uri = f"https://paper.example.test/{suffix}"
    source = runtime.sources.add_source({"source_id": f"source-{suffix}", "source_kind": "paper",
                                        "uri": uri, "enabled": True,
                                        "metadata": {"connector_id": "test.native", "trust": 0.85}})
    candidate = RecordEnvelope.create(kind="knowledge_candidate", title=f"Paper {suffix}",
        summary=TEXT, content={"source_id": source.source_id, "source_kind": "paper", "source_uri": uri},
        scope=scope, source="test.native", status="candidate")
    decision = resolve_source_trust(candidate.to_dict(), registry=runtime.sources, connector_id="test.native")
    assert decision.reasons == ("registry_verified",)
    candidate.meta["source_trust_decision"] = decision.to_dict()
    runtime.store.append(candidate)
    if approved:
        candidate = review_candidate(runtime, candidate.record_id, "approve", "native-reviewer", scope=scope)
    pdf = path / f"paper-{suffix}.pdf"
    pdf.write_bytes(_minimal_pdf(TEXT))
    artifacts = materialize_paper_artifacts(runtime.store.root, {"pdf_file": str(pdf)})
    paper = PaperSource(paper_source_id=f"paper-{suffix}", source_kind="pdf", title=f"Paper {suffix}",
        pdf_blob_ref=artifacts["pdf_blob_ref"], normalized_text_ref=artifacts["normalized_text_ref"],
        metadata={"artifact": artifacts["artifact"]},
        provenance={"input_kind": "paper_intake_pipeline", "record_id": candidate.record_id}).to_record(scope=scope)
    runtime.store.append(paper)
    candidate = mark_candidate_paper_promoted(runtime, candidate, {"paper_source_id": paper.record_id, "record_ids": [paper.record_id]})
    claim = RecordEnvelope.create(kind="claim_card", title="Canonical claim", summary=TEXT,
        content={"paper_source_id": paper.record_id, "claim_text": TEXT}, scope=scope, source="test.native")
    if context_revision is not None:
        claim.content["metadata"] = {"capability_context": {"capability_id": "dynamic.catalog",
            "capability_revision_id": context_revision, "capability_scope": "global", "relation_type": "supports"}}
    runtime.store.append(claim)
    return claim, paper, candidate


def _model(_system, prompt):
    value = json.loads(prompt)
    return json.dumps({"input_digest": value["input_digest"],
        "capability_id": value["target"]["capability_id"],
        "capability_revision_id": value["target"]["capability_revision_id"],
        "applicability": "applicable", "relation_type": "informs_eval", "quotes": [TEXT]})


def _run(registered, executor=_model):
    runtime, _, _, _ = registered
    return review.review_gap_knowledge(runtime, runtime_scope=SCOPE, capability_scope="global",
        capability_id="dynamic.catalog", revision_id="dynamic.catalog:v1",
        binding_ids=["binding.dynamic.catalog:v1"], executor=executor)


def _store_legacy_payload(runtime, record):
    payload = record.to_dict()
    for key in ("source_id", "aliases", "aliases_version"):
        payload.pop(key)
    digest = payload_digest(payload)
    with runtime.store.locked() as sql:
        sql.execute("UPDATE records SET payload_json=?,payload_digest=? WHERE record_id=?",
                    (canonical_payload_json(payload), digest, record.record_id))
        sql.commit()
    hydrated = runtime.store.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id)
    assert hydrated is not None and hydrated.scope == record.scope and hydrated.source_id == record.source_id
    assert payload_digest(hydrated.to_dict()) != digest
    return digest


@pytest.mark.parametrize("legacy", ["claim", "paper", "candidate", "all"])
def test_legacy_storage_digest_survives_hydration_and_certifies_inventory(registered, legacy):
    runtime, _, _, _ = registered
    records = dict(zip(("claim", "paper", "candidate"), _evidence(registered)))
    for name, record in records.items():
        if legacy in {name, "all"}:
            _store_legacy_payload(runtime, record)
    # Same ID in a foreign scope must not affect exact-ref selection.
    runtime.store.append(replace(records["claim"], scope=ScopeRef.from_dict({**SCOPE, "user_id": "foreign"})))
    scope = ScopeRef.from_dict(SCOPE)
    row = inventory.inventory_page(runtime.store, scope)["rows"][0]
    high = inventory.current_watermark(runtime.store, scope)
    assert inventory.get_input_row(runtime.store, scope, row["storage_key"]) == row
    assert inventory.changes_page(runtime.store, scope, after_version=high, through_version=high)["changes"] == []
    report = _run(registered)
    assert report["inventory"]["complete"], json.dumps(report, sort_keys=True)
    assert len(report["links"]) == 1 and report["inventory"]["certificate_ref"]
    entry = inventory.get_manifest_entry(runtime.store, scope, report["inventory"]["scan_key"], row["storage_key"])
    assert entry["payload_digest"] == row["payload_digest"] and not entry["dirty"]
    assert review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE,
                                               review_receipt_id=report["review_receipt_ids"][0])
    again = _run(registered, lambda *_: pytest.fail("unchanged legacy input must reuse review"))
    assert again["inventory"]["complete"] and again["links"] == report["links"]
    assert inventory.current_watermark(runtime.store, scope) == high


@pytest.mark.parametrize("legacy", ["claim", "paper", "candidate", "all"])
@pytest.mark.parametrize("time_shape", ["missing", "null", "partial", "invalid"])
def test_legacy_unknown_times_do_not_change_review_inputs(registered, monkeypatch, legacy, time_shape):
    runtime, _, _, _ = registered
    records = dict(zip(("claim", "paper", "candidate"), _evidence(registered)))
    ticks = iter(range(100000))
    monkeypatch.setattr("eimemory.models.records.now_iso", lambda: f"2026-10-14T00:00:00.{next(ticks):06d}Z")
    for name, record in records.items():
        if legacy not in {name, "all"}:
            continue
        payload = record.to_dict()
        if time_shape == "missing":
            payload.pop("time")
        elif time_shape == "null":
            payload["time"] = None
        elif time_shape == "invalid":
            payload["time"] = ["not a timestamp mapping"]
        else:
            payload["time"] = {"created_at": record.time.created_at, "updated_at": None}
        with runtime.store.locked() as sql:
            sql.execute("UPDATE records SET payload_json=?,payload_digest=? WHERE record_id=?",
                        (canonical_payload_json(payload), payload_digest(payload), record.record_id))
            sql.commit()
        first = runtime.store.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id)
        second = runtime.store.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id)
        assert first.time != second.time  # The general reader still defaults unknown time to now.
    report = _run(registered)
    assert report["inventory"]["complete"] and len(report["links"]) == 1, report
    assert review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE,
                                               review_receipt_id=report["review_receipt_ids"][0])
    again = _run(registered, lambda *_: pytest.fail("unchanged missing timestamps must reuse review"))
    assert again["links"] == report["links"] and again["inventory"]["complete"]
    from eimemory.governance.capability.hypothesis_producer import _applicable_link_rows
    links = _applicable_link_rows(runtime, scope=ScopeRef.from_dict(SCOPE), capability_scope="global",
                                 capability_id="dynamic.catalog", revision_id="dynamic.catalog:v1")
    assert len(links) == 1 and links[0]["payload"]["link_id"] == report["links"][0]["link_id"]


def test_corrupt_segment_does_not_abort_inventory_progress_or_starve_retry(registered):
    runtime, _, _, _ = registered
    bad = _raw_inventory(registered, 1, prefix="aaa")[0]
    _raw_inventory(registered, 201, prefix="bbb")
    claim, _, _ = _evidence(registered)
    with runtime.store.locked() as sql:
        sql.execute("UPDATE records SET payload_pointer_json=? WHERE record_id=?", ("{}", bad.record_id))
        sql.commit()
    calls = []
    def model(system, prompt):
        calls.append(json.loads(prompt)["source"]["knowledge_record_id"])
        return _model(system, prompt)
    first = _run(registered, model)
    assert first["inventory"]["page_record_count"] == 200 and not first["inventory"]["inventory_complete"]
    second = _run(registered, model)
    assert second["inventory"]["inventory_complete"] and second["inventory"]["manifest_record_count"] == 203
    assert calls == [claim.record_id] and second["links"] == [] and not second["inventory"]["complete"]
    bad_entry = next(row for row in inventory.manifest_page(runtime.store, SCOPE, second["inventory"]["scan_key"], dirty_only=True)["rows"]
                     if row["record_id"] == bad.record_id)
    assert bad_entry["dirty"] and bad_entry["attempt_count"] >= 1
    with runtime.store.locked() as sql:
        sql.execute("UPDATE records SET payload_pointer_json='' WHERE record_id=?", (bad.record_id,))
        sql.commit()
    recovered = _run(registered, lambda *_: pytest.fail("only broken hydration needs retry"))
    assert recovered["inventory"]["complete"] and len(recovered["links"]) == 1


@pytest.mark.parametrize("dependency", ["paper", "candidate"])
def test_corrupt_dependency_remains_unknown_and_recovers_without_new_review(registered, dependency):
    runtime, _, _, _ = registered
    records = dict(zip(("claim", "paper", "candidate"), _evidence(registered)))
    with runtime.store.locked() as sql:
        sql.execute("UPDATE records SET payload_pointer_json=? WHERE record_id=?", ("{}", records[dependency].record_id))
        sql.commit()
    broken = _run(registered, lambda *_: pytest.fail("corrupt dependency must never reach model"))
    assert not broken["inventory"]["complete"] and not broken["links"]
    assert broken["reason_counts"]["knowledge_evidence_unavailable"] == 1
    with runtime.store.locked() as sql:
        sql.execute("UPDATE records SET payload_pointer_json='' WHERE record_id=?", (records[dependency].record_id,))
        sql.commit()
    repaired = _run(registered)
    assert repaired["inventory"]["complete"] and len(repaired["links"]) == 1


@pytest.mark.parametrize("tamper", ["body", "digest", "scope", "source", "kind", "status"])
def test_legacy_inventory_still_rejects_corrupt_payload_or_projection(registered, tamper):
    runtime, _, _, _ = registered
    claim, _, _ = _evidence(registered)
    _store_legacy_payload(runtime, claim)
    with runtime.store.locked() as sql:
        row = sql.execute("SELECT payload_json,payload_digest FROM records WHERE record_id=?", (claim.record_id,)).fetchone()
        payload = json.loads(row["payload_json"])
        if tamper == "digest":
            digest = "0" * 64
        else:
            if tamper == "body":
                payload["content"]["claim_text"] += " tampered"
            elif tamper == "scope":
                payload["scope"]["user_id"] = "foreign"
            elif tamper == "source":
                payload["source_id"] = "foreign"
            elif tamper == "kind":
                payload["kind"] = "knowledge_page"
            else:
                payload["status"] = "archived"
            digest = payload_digest(payload) if tamper != "body" else row["payload_digest"]
        sql.execute("UPDATE records SET payload_json=?,payload_digest=? WHERE record_id=?",
                    (canonical_payload_json(payload), digest, claim.record_id))
        sql.commit()
    report = _run(registered, lambda *_: pytest.fail("invalid payload must never reach model"))
    assert report["links"] == [] and not report["inventory"]["complete"]
    assert report["reason_counts"]["knowledge_inventory_record_changed"] == 1


def test_legacy_page_source_projection_is_verified_before_inventory_certification(registered):
    from eimemory.models.knowledge_pages import KnowledgePage
    runtime, _, _, _ = registered
    claim, paper, _ = _evidence(registered)
    page = KnowledgePage(knowledge_page_id=claim.record_id, page_type="topic", title="Legacy page",
                         summary=TEXT, sections=({"text": TEXT},), source_ids=(paper.record_id,)).to_record(scope=claim.scope)
    runtime.store.rewrite(page, previous_scope=claim.scope)
    # Reproduce the legacy partition projection without changing the raw body's source_id.
    with runtime.store.locked() as sql:
        sql.execute("UPDATE records SET source_id=? WHERE record_id=?", (paper.record_id, page.record_id))
        sql.commit()
    page.source_id = paper.record_id
    _store_legacy_payload(runtime, page)
    report = _run(registered)
    assert report["inventory"]["complete"] and len(report["links"]) == 1, json.dumps(report, sort_keys=True)


def test_approved_canonical_review_links_native_current_gap_to_one_candidate(registered, monkeypatch):
    runtime, profile, catalog, _ = registered
    _evidence(registered)
    # Exercise the real bounded command/tool-free envelope validation with a
    # local synthetic provider. Discovery alone is replaced; completion,
    # subprocess transport, structured output and durable evidence are native.
    script = registered[3] / "synthetic_tool_free_provider.py"
    script.write_text("import json, sys\n"
        "request = json.loads(sys.stdin.buffer.read())\n"
        "value = json.loads(request['user'])\n"
        "output = dict(input_digest=value['input_digest'], capability_id=value['target']['capability_id'], "
        "capability_revision_id=value['target']['capability_revision_id'], applicability='applicable', "
        "relation_type='informs_eval', quotes=[value['source']['canonical_window']])\n"
        "sys.stdout.write(json.dumps(dict(text=json.dumps(output), tools=[])))\n", encoding="utf-8")
    monkeypatch.setattr("eimemory.llm.hermes_tool_free.runtime_command", lambda _env: [sys.executable, "-I", "-B", str(script)])
    report = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert len(report["created"]) == 1
    assert report["knowledge_link_reviews"][0]["applicable_count"] == 1
    link_id = report["created"][0]["link_id"]
    receipts = report["knowledge_link_reviews"][0]["review_receipt_ids"]
    stored = runtime.store.get_by_id(receipts[0], scope=ScopeRef.from_dict(SCOPE))
    assert stored.content["code_changes_authorized"] is False
    assert stored.content["quote_refs"][0]["length"] == len(TEXT)
    assert TEXT not in json.dumps(stored.to_dict())
    hypotheses = list_capability_hypotheses(runtime, runtime_scope=SCOPE)
    assert len(hypotheses) == 1 and hypotheses[0].status == "candidate"
    assert hypotheses[0].content["expected_metric"]["gap_evidence"]["knowledge_review_receipt_id"] == receipts[0]
    assert hypotheses[0].content["link_id"] == link_id
    assert hypothesis_behavior_gate(runtime, runtime_scope=SCOPE, hypothesis_id=hypotheses[0].record_id)["allowed"] is False
    assert hypotheses[0].content["candidate_bounds"]["max_changes"] == 0


@pytest.mark.parametrize("condition", ["unreviewed", "expired", "contradicted", "foreign", "stale_revision", "trust_disabled"])
def test_ineligible_knowledge_never_calls_model_or_creates_link(registered, condition):
    runtime, _, _, _ = registered
    claim, paper, candidate = _evidence(registered,
        approved=condition != "unreviewed", scope={**SCOPE, "user_id": "foreign"} if condition == "foreign" else None,
        context_revision="dynamic.catalog:old" if condition == "stale_revision" else None)
    if condition == "expired":
        claim.content["temporal_validity"] = {"expires_at": "2020-01-01T00:00:00Z"}
        runtime.store.rewrite(claim, previous_scope=claim.scope)
    elif condition == "contradicted":
        claim.content["contradiction_ids"] = ["contradiction-real"]
        runtime.store.rewrite(claim, previous_scope=claim.scope)
    elif condition == "trust_disabled":
        current = next(item for item in runtime.sources.list_sources() if item.source_id == "source-1")
        runtime.sources.add_source({**current.to_dict(), "enabled": False})
    calls = []
    report = _run(registered, lambda *args: calls.append(args))
    assert calls == [] and report["links"] == []
    assert list_registered_knowledge_links(runtime, runtime_scope=SCOPE, capability_scope="global") == []


def test_multiple_applicable_sources_are_not_chosen_by_order_or_volume(registered):
    runtime, _, _, _ = registered
    _evidence(registered, suffix="1")
    _evidence(registered, suffix="2")
    report = _run(registered)
    assert report["applicable_count"] == 2 and report["links"] == []
    assert report["reason_counts"]["ambiguous_reviewed_knowledge_for_gap_revision"] == 1
    assert list_registered_knowledge_links(runtime, runtime_scope=SCOPE, capability_scope="global") == []


def test_retry_is_idempotent_and_review_revocation_cannot_be_automatically_revived(registered):
    runtime, _, _, _ = registered
    _evidence(registered)
    first = _run(registered)
    again = _run(registered, lambda *_: pytest.fail("cache should avoid model call"))
    assert first["links"] == again["links"] and again["reviewed_count"] == 0
    receipt = first["review_receipt_ids"][0]
    review.revoke_knowledge_gap_review(runtime, runtime_scope=SCOPE, review_receipt_id=receipt, reason="not applicable")
    assert review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt) is False
    revoked = _run(registered, lambda *_: pytest.fail("revoked inputs must not be rereviewed"))
    assert not revoked["links"] and revoked["reason_counts"]["applicability_review_revoked"] == 1


def test_current_source_update_invalidates_old_receipt_and_planner_candidate(registered, monkeypatch):
    runtime, profile, catalog, _ = registered
    claim, _, _ = _evidence(registered)
    monkeypatch.setattr("eimemory.llm.hermes_tool_free.complete", _model)
    first = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    hypothesis_id = first["created"][0]["hypothesis_id"]
    receipt = first["knowledge_link_reviews"][0]["review_receipt_ids"][0]
    claim.content["contradiction_ids"] = ["new-contradiction"]
    runtime.store.rewrite(claim, previous_scope=claim.scope)
    assert review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt) is False
    assert hypothesis_id in revoked_hypothesis_ids(runtime, runtime_scope=SCOPE)
    plan = build_dynamic_capability_evolution_plan(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert plan["work_items"][0]["detail"]["candidate_hypothesis_count"] == 0
    next_report = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert next_report["created"] == []


def test_quote_and_revision_binding_are_verified_not_model_assertions(registered):
    _evidence(registered)
    def forged(system, prompt):
        result = json.loads(_model(system, prompt))
        result["quotes"] = ["Invented source text has never existed in this canonical paper."]
        return json.dumps(result)
    report = _run(registered, forged)
    assert not report["links"] and report["reason_counts"]["applicability_review_quote_invalid"] == 1


def test_source_change_during_review_does_not_certify_the_old_snapshot(registered):
    runtime, _, _, _ = registered
    claim, _, _ = _evidence(registered)
    def racing(system, prompt):
        claim.status = "stale"
        runtime.store.rewrite(claim, previous_scope=claim.scope)
        return _model(system, prompt)
    report = _run(registered, racing)
    assert not report["links"] and report["review_receipt_ids"] == []


def test_transport_failure_is_visible_and_does_not_print_private_error(registered):
    _evidence(registered)
    def unavailable(*_):
        raise RuntimeError("private model credential and business source")
    report = _run(registered, unavailable)
    assert report["reason_counts"]["applicability_review_transport_unavailable"] == 1
    assert "private" not in json.dumps(report) and report["links"] == []


def test_stale_plan_does_not_create_duplicate_or_revive_revoked_hypothesis(registered, monkeypatch):
    runtime, profile, catalog, _ = registered
    _evidence(registered)
    monkeypatch.setattr("eimemory.llm.hermes_tool_free.complete", _model)
    stale_plan = build_dynamic_capability_evolution_plan(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    first = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog, plan=stale_plan)
    stale_plan["projection_digest"] = "new-projection"
    second = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog, plan=stale_plan)
    assert len(first["created"]) == 1 and second["created"] == [] and len(second["reused"]) == 1
    hypothesis_id = first["created"][0]["hypothesis_id"]
    revoke_produced_hypothesis(runtime, runtime_scope=SCOPE, hypothesis_id=hypothesis_id, reason="do not retry")
    third = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog, plan=stale_plan)
    assert not third["created"] and not third["reused"]
    assert third["skipped"][0]["reason"] == "produced_hypothesis_revoked"
    assert len(list_capability_hypotheses(runtime, runtime_scope=SCOPE)) == 1


@pytest.mark.parametrize("declaration", ["claim_meta_expired", "source_meta_expired", "claim_future",
                                       "source_malformed", "conflicting_environment"])
def test_every_source_and_claim_validity_declaration_is_respected(registered, declaration):
    runtime, _, _, _ = registered
    claim, paper, _ = _evidence(registered)
    if declaration == "claim_meta_expired":
        claim.meta["valid_until"] = "2020-01-01T00:00:00Z"
    elif declaration == "source_meta_expired":
        paper.meta["expires_at"] = "2020-01-01T00:00:00Z"
    elif declaration == "claim_future":
        claim.provenance["valid_from"] = "2099-01-01T00:00:00Z"
    elif declaration == "source_malformed":
        paper.content["metadata"]["temporal_validity"] = {"valid_until": "invalid-private-date"}
    elif declaration == "conflicting_environment":
        paper.meta["environment_constraints"] = {"required": {"runtime": "other"}}
        claim.meta["environment_constraints"] = {"required": {"runtime": "test"}}
    runtime.store.rewrite(claim, previous_scope=claim.scope)
    runtime.store.rewrite(paper, previous_scope=paper.scope)
    calls = []
    report = _run(registered, lambda *args: calls.append(args))
    assert not calls and not report["links"] and report["eligible_count"] == 0
    assert "invalid-private-date" not in json.dumps(report)


def test_more_than_four_inputs_progress_across_runs_without_claiming_partial_uniqueness(registered):
    runtime, _, _, _ = registered
    claims = [_evidence(registered, suffix=str(index))[0] for index in range(5)]
    positive_id = min(claim.record_id for claim in claims)
    calls = []
    def discriminate(system, prompt):
        value = json.loads(prompt)
        calls.append(value["source"]["knowledge_record_id"])
        result = json.loads(_model(system, prompt))
        if value["source"]["knowledge_record_id"] != positive_id:
            result["applicability"] = "not_applicable"
            result["quotes"] = []
        return json.dumps(result)
    first = _run(registered, discriminate)
    assert first["eligible_count"] == 5 and first["reviewed_count"] == 4
    assert first["completed_count"] == 4 and first["deferred_count"] == 1
    assert first["applicable_count"] == 1 and first["links"] == []
    assert len(calls) == 4
    second = _run(registered, discriminate)
    assert second["reviewed_count"] == 1 and second["completed_count"] == 5 and second["deferred_count"] == 0
    assert second["applicable_count"] == 1 and len(second["links"]) == 1
    assert len(calls) == 5 and len(set(calls)) == 5
    third = _run(registered, lambda *_: pytest.fail("all terminal inputs should be cached"))
    assert third["links"] == second["links"] and third["reviewed_count"] == 0


def test_strict_revocation_inventory_is_required_before_forward_authority(registered, monkeypatch):
    runtime, _, _, _ = registered
    def unavailable(**_):
        raise RuntimeError("private database credentials")
    monkeypatch.setattr(runtime.store, "list_records_by_meta_value", unavailable)
    with pytest.raises(RuntimeError, match="^hypothesis_revocation_inventory_unavailable$"):
        revoked_hypothesis_ids(runtime, runtime_scope=SCOPE, strict=True)


def test_strict_fallback_rejects_truncated_raw_inventory_before_filtering(registered, monkeypatch):
    runtime, _profile, _catalog, _path = registered
    for index in range(501):
        runtime.store.append(RecordEnvelope.create(kind="reflection", title=f"Ordinary event {index}",
            content={"report_type": "ordinary_event"}, scope=ScopeRef.from_dict(SCOPE), source="ordinary.events"))
    monkeypatch.setattr(runtime.store, "list_records_by_meta_value", None)
    # A generic reflection window cannot establish that older revocations are
    # absent merely because the visible rows have another report type.
    with pytest.raises(RuntimeError, match="^hypothesis_revocation_inventory_truncated$"):
        revoked_hypothesis_ids(runtime, runtime_scope=SCOPE, strict=True)


def _raw_inventory(registered, count=205, *, scope=None, prefix="a_raw"):
    runtime = registered[0]
    scope = ScopeRef.from_dict(scope or SCOPE)
    records = []
    for index in range(count):
        record = RecordEnvelope.create(kind="claim_card", title="Unattributed fixture",
            summary="Raw fixture has no reviewed canonical source.", scope=scope,
            content={"claim_text": "Raw fixture has no reviewed canonical source."}, source="test.raw")
        record.record_id = f"{prefix}_{index:04}"
        records.append(record)
    # Native mutation owner, one transaction; every inserted row still passes
    # its records triggers. The fixtures contain no production memory body.
    def append(sqlite):
        for record in records:
            sqlite.upsert(record, commit=False)
        return None, records, []
    runtime.store.mutate_records_atomically(append)
    return records


def test_more_than_200_inputs_complete_native_scan_before_producing_hypothesis(registered, monkeypatch):
    runtime, profile, original, _ = registered
    _raw_inventory(registered, 205)
    _evidence(registered)
    # A real failing catalog diagnostic keeps the real gap open during the
    # first page. Merely passing a registered case could legitimately close it.
    from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog
    catalog = CapabilityEvaluationCatalog()
    catalog.register_executor(executor_id="eimemory.eval.dynamic-catalog", revision="v1",
        handler=lambda *_: {"decision": "wrong", "evidence_count": 0})
    catalog.register_case(original.list_cases()[0])
    calls = []
    def model(system, prompt):
        calls.append(json.loads(prompt)["source"]["knowledge_record_id"])
        return _model(system, prompt)
    monkeypatch.setattr("eimemory.llm.hermes_tool_free.complete", model)
    first = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    scan = first["knowledge_link_reviews"][0]["inventory"]
    assert first["created"] == [] and scan["page_record_count"] == 200
    assert scan["complete"] is False and scan["inventory_complete"] is False
    assert calls == []
    second = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    scan = second["knowledge_link_reviews"][0]["inventory"]
    assert scan["complete"] is True and scan["manifest_record_count"] == 206
    assert scan["page_record_count"] == 6 and scan["pending_count"] == 0
    assert scan["input_watermark"] == scan["current_watermark"]
    assert len(second["created"]) == len(calls) == 1
    hypothesis = list_capability_hypotheses(runtime, runtime_scope=SCOPE)[0]
    assert hypothesis_behavior_gate(runtime, runtime_scope=SCOPE, hypothesis_id=hypothesis.record_id)["allowed"] is False


def test_low_key_new_applicable_input_is_reconciled_before_uniqueness(registered):
    runtime, _, _, _ = registered
    _raw_inventory(registered, 205)
    _, paper, _ = _evidence(registered)
    first = _run(registered)
    assert first["inventory"]["complete"] is False
    sibling = RecordEnvelope.create(kind="claim_card", title="Another canonical claim", summary=TEXT,
        content={"paper_source_id": paper.record_id, "claim_text": TEXT}, scope=ScopeRef.from_dict(SCOPE), source="test.native")
    sibling.record_id = "a_inserted_behind_cursor"
    runtime.store.append(sibling)
    second = _run(registered)
    assert second["inventory"]["complete"] is True
    assert second["inventory"]["manifest_record_count"] == 207
    assert second["applicable_count"] == 2 and second["links"] == []
    assert second["reason_counts"]["ambiguous_reviewed_knowledge_for_gap_revision"] == 1


def test_completed_certificate_rejects_sibling_insert_even_when_chosen_unchanged(registered):
    runtime, _, _, _ = registered
    _, paper, _ = _evidence(registered)
    first = _run(registered)
    receipt = first["links"][0]["review_receipt_id"]
    assert review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt)
    sibling = runtime.store.append(RecordEnvelope.create(kind="claim_card", title="Sibling claim", summary=TEXT,
        content={"paper_source_id": paper.record_id, "claim_text": TEXT}, scope=ScopeRef.from_dict(SCOPE), source="test.native"))
    assert not review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt)
    again = _run(registered)
    assert again["applicable_count"] == 2 and not again["links"]
    # Deletion is a source change too; it does not revive the old certificate
    # until a current full-membership certificate is persisted again.
    runtime.store.run_locked(lambda sqlite: sqlite.execute("DELETE FROM records WHERE record_id=?", (sibling.record_id,)))
    runtime.store.run_locked(lambda sqlite: sqlite.commit())
    assert not review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt)
    recovered = _run(registered)
    assert len(recovered["links"]) == 1


def test_prior_page_update_is_revalidated_and_foreign_scope_is_not_enumerated(registered):
    runtime, _, _, _ = registered
    raws = _raw_inventory(registered, 205)
    _raw_inventory(registered, 220, scope={**SCOPE, "user_id": "another-user"}, prefix="foreign")
    _, paper, _ = _evidence(registered)
    first = _run(registered)
    assert first["inventory"]["manifest_record_count"] == 200
    old = raws[0]
    old.summary = TEXT
    old.content = {"paper_source_id": paper.record_id, "claim_text": TEXT}
    runtime.store.rewrite(old, previous_scope=old.scope)
    next_report = _run(registered)
    assert next_report["inventory"]["manifest_record_count"] == 206
    assert next_report["applicable_count"] == 2 and not next_report["links"]


def test_regular_new_records_and_scan_timestamps_do_not_restart_progress(registered):
    runtime, _, _, _ = registered
    _raw_inventory(registered, 410)
    _evidence(registered)
    first = _run(registered)
    assert first["inventory"]["manifest_record_count"] == 200
    current = runtime.sources.list_sources()[0]
    runtime.sources.add_source({**current.to_dict(), "last_scanned_at": "2026-10-13T01:00:00Z"})
    _raw_inventory(registered, 1, prefix="a_added_after_first_page")
    second = _run(registered)
    assert second["inventory"]["scan_key"] == first["inventory"]["scan_key"]
    assert second["inventory"]["manifest_record_count"] >= 400
    _raw_inventory(registered, 1, prefix="a_added_after_second_page")
    third = _run(registered)
    assert third["inventory"]["complete"] is True
    assert third["inventory"]["manifest_record_count"] == 413
    assert len(third["links"]) == 1


def test_more_than_500_older_review_receipts_use_exact_index_lookup(registered):
    runtime, _, _, _ = registered
    _evidence(registered)
    for index in range(505):
        review._append(runtime.store, ScopeRef.from_dict(SCOPE), {
            "report_type": review.SCHEMA, "schema": review.SCHEMA, "status": "unavailable",
            "input_digest": f"{index:064x}", "reason": "applicability_review_transport_unavailable",
        }, "unavailable", [])
    report = _run(registered)
    assert len(report["links"]) == 1 and report["inventory"]["complete"]
    assert "review_inventory_truncated" not in report["reason_counts"]


def test_policy_file_change_with_restored_mtime_invalidates_certificate(registered):
    runtime, _, _, _ = registered
    _evidence(registered)
    first = _run(registered)
    receipt = first["links"][0]["review_receipt_id"]
    path = runtime.sources.path
    before = path.stat()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[0]["enabled"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt)
    blocked = _run(registered, lambda *_: pytest.fail("disabled source must not be sent to a model"))
    assert blocked["links"] == [] and blocked["inventory"]["scan_key"] != first["inventory"]["scan_key"]


def test_external_canonical_change_and_restoration_revalidate_without_db_change(registered):
    runtime, _, _, _ = registered
    _, paper, _ = _evidence(registered)
    first = _run(registered)
    receipt = first["links"][0]["review_receipt_id"]
    watermark = inventory.current_watermark(runtime.store, ScopeRef.from_dict(SCOPE))
    path = runtime.store.root / paper.content["normalized_text_ref"]
    original = path.read_bytes()
    path.write_bytes(b"changed source bytes cannot match the canonical artifact digest")
    assert inventory.current_watermark(runtime.store, ScopeRef.from_dict(SCOPE)) == watermark
    assert not review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt)
    blocked = _run(registered)
    assert not blocked["links"] and blocked["inventory"]["complete"] is False
    restored = _run(registered)  # Still unavailable; never certify from cached positivity.
    assert restored["inventory"]["unknown_count"] == 1
    path.write_bytes(original)
    recovered = _run(registered, lambda *_: pytest.fail("unchanged valid input should reuse its independent review"))
    assert len(recovered["links"]) == 1


def test_clock_expiry_invalidates_complete_inventory_without_record_mutation(registered, monkeypatch):
    runtime, _, _, _ = registered
    claim, _, _ = _evidence(registered)
    deadline = datetime.now(timezone.utc) + timedelta(days=2)
    claim.meta["valid_until"] = deadline.isoformat()
    runtime.store.rewrite(claim, previous_scope=claim.scope)
    first = _run(registered)
    receipt = first["links"][0]["review_receipt_id"]
    frozen = deadline + timedelta(seconds=1)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is not None else frozen.replace(tzinfo=None)
    monkeypatch.setattr(review, "datetime", Clock)
    monkeypatch.setattr("eimemory.knowledge.capabilities.datetime", Clock)
    assert not review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt)
    expired = _run(registered, lambda *_: pytest.fail("expired source must not be reevaluated by a model"))
    assert expired["links"] == [] and expired["applicable_count"] == 0


def test_missing_inventory_certificate_and_late_conflicting_completion_block_consumption(registered):
    runtime, _, _, _ = registered
    _evidence(registered)
    first = _run(registered)
    receipt_id = first["links"][0]["review_receipt_id"]
    receipt = runtime.store.get_by_id(receipt_id, scope=ScopeRef.from_dict(SCOPE), exact_scope=True)
    conflicting = {**receipt.content, "applicability": "not_applicable", "quote_refs": []}
    # Simulate process B appending its independently completed response and
    # crashing before it updates the manifest. A's former seal cannot survive.
    review._append(runtime.store, ScopeRef.from_dict(SCOPE), conflicting, "not_applicable", receipt.evidence)
    assert not review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt_id)
    blocked = _run(registered)
    assert blocked["links"] == [] and blocked["inventory"]["complete"] is False
    assert blocked["reason_counts"]["conflicting_applicability_reviews"] == 1


def test_deleted_certificate_cannot_be_replaced_by_a_successful_looking_scan_state(registered):
    runtime, _, _, _ = registered
    _evidence(registered)
    report = _run(registered)
    receipt = report["links"][0]["review_receipt_id"]
    certificate = report["inventory"]["certificate_ref"]
    runtime.store.run_locked(lambda sqlite: sqlite.execute("DELETE FROM records WHERE record_id=?", (certificate,)))
    runtime.store.run_locked(lambda sqlite: sqlite.commit())
    assert not review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt)


def test_missing_change_history_restarts_a_fresh_bounded_full_scan(registered):
    runtime, _, _, _ = registered
    raws = _raw_inventory(registered, 205)
    _evidence(registered)
    first = _run(registered)
    assert first["inventory"]["complete"] is False
    raws[0].summary = "Changed unreviewed fixture remains unattributed."
    runtime.store.rewrite(raws[0], previous_scope=raws[0].scope)
    runtime.store.run_locked(lambda sqlite: sqlite.execute(
        "DELETE FROM knowledge_review_inventory_changes WHERE version=(SELECT MIN(version) FROM knowledge_review_inventory_changes)"))
    runtime.store.run_locked(lambda sqlite: sqlite.commit())
    reset = _run(registered)
    assert reset["reason_counts"]["knowledge_inventory_restarted_after_change_gap"] == 1
    assert reset["inventory"]["manifest_record_count"] == 200 and not reset["links"]
    assert reset["inventory"]["complete"] is False
    recovered = _run(registered)
    assert recovered["inventory"]["manifest_record_count"] == 206
    assert recovered["inventory"]["complete"] is True and len(recovered["links"]) == 1


def test_ready_artifact_unavailable_on_first_scan_remains_unknown_until_verified(registered):
    runtime, _, _, _ = registered
    _, paper, _ = _evidence(registered)
    path = runtime.store.root / paper.content["normalized_text_ref"]
    contents = path.read_bytes()
    path.unlink()
    unavailable = _run(registered, lambda *_: pytest.fail("unverified canonical input must not reach a model"))
    assert unavailable["inventory"]["unknown_count"] == 1
    assert unavailable["inventory"]["complete"] is False and unavailable["links"] == []
    path.write_bytes(contents)
    restored = _run(registered)
    assert restored["inventory"]["complete"] is True and len(restored["links"]) == 1


def test_source_approval_change_before_final_page_is_not_hidden_by_prior_cursor(registered):
    runtime, _, _, _ = registered
    _raw_inventory(registered, 205)
    _, _, candidate = _evidence(registered)
    first = _run(registered)
    assert first["inventory"]["complete"] is False
    candidate.status = "rejected"
    runtime.store.rewrite(candidate, previous_scope=candidate.scope)
    second = _run(registered, lambda *_: pytest.fail("revoked intake approval must not reach a model"))
    assert second["inventory"]["complete"] is True
    assert second["applicable_count"] == 0 and second["links"] == []


@pytest.mark.parametrize("target", ["review", "certificate"])
def test_inactive_audit_receipt_cannot_authorize_current_link(registered, target):
    runtime, _, _, _ = registered
    _evidence(registered)
    report = _run(registered)
    receipt_id = report["links"][0]["review_receipt_id"]
    record_id = receipt_id if target == "review" else report["inventory"]["certificate_ref"]
    record = runtime.store.get_by_id(record_id, scope=ScopeRef.from_dict(SCOPE), exact_scope=True)
    assert record.status == "archived"  # The intended authoritative audit status.
    record.status = "deprecated"
    runtime.store.rewrite(record, previous_scope=record.scope)
    assert not review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt_id)


def test_same_input_receipt_overflow_does_not_hide_an_unseen_conflict(registered):
    runtime, _, _, _ = registered
    _evidence(registered)
    report = _run(registered)
    receipt_id = report["links"][0]["review_receipt_id"]
    receipt = runtime.store.get_by_id(receipt_id, scope=ScopeRef.from_dict(SCOPE), exact_scope=True)
    for index in range(3):
        review._append(runtime.store, ScopeRef.from_dict(SCOPE), {
            "report_type": review.SCHEMA, "schema": review.SCHEMA, "status": "unavailable",
            "input_digest": receipt.content["input_digest"], "attempt": index,
            "reason": "applicability_review_transport_unavailable",
        }, "unavailable", [])
    assert not review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt_id)
    blocked = _run(registered)
    assert blocked["links"] == []
    assert blocked["reason_counts"]["inventory_review_lookup_truncated"] == 1
