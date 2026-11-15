"""First citation-admission candidate: synthetic contracts, no provider calls.

The parent executes these against real RuntimeStore in its isolated sandbox.
Pure cases specify conservative lexical behavior, not semantic answer quality.
"""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter

import pytest

from eimemory.contracts.recall_boundary import exact_ref, scope_tuple
from eimemory.governance.release.release_impact import _domains_for_change
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.raw import citation_gate as gate
from eimemory.raw import retrieval
from eimemory.raw.boundary import RawRecallUnavailable
from eimemory.storage.runtime_store import RuntimeStore


OWNER = ScopeRef(tenant_id="citation-fixture", agent_id="agent", workspace_id="room", user_id="owner")
QUERY = "Where are Halcyon deployment manifests stored?"
GENERIC = "The deployment workbook describes release procedures and migration checklists."
EXPLICIT = "Halcyon deployment manifests are stored in the infrastructure repository."


@pytest.fixture
def store(tmp_path):
    instance = RuntimeStore(tmp_path / "store")
    try:
        yield instance
    finally:
        instance.close()


def add(store, text=GENERIC, *, scope=OWNER, source_id="public", **kwargs):
    record = RecordEnvelope.create(
        kind="raw_chunk", title="notebook", scope=scope, source="synthetic",
        source_id=source_id, summary=text, detail=text,
        content={"raw_text": text, "text": text, "session_id": "dialog", "turn_id": "turn:1"},
        **kwargs,
    )
    return store.append(record)


def recall(store, *, query=QUERY, intent="fact_citation", sources=None, context=None, limit=5):
    task = {"rerank_with_llm": False, "enable_llm_rerank": False, **(context or {})}
    if intent is not None:
        task["evidence_intent"] = intent
    return retrieval.search_raw_chunks(store, query=query, scope=OWNER, source_ids=sources,
                                       task_context=task, limit=limit)


def snapshot(*texts, aliases=()):
    docs = tuple(gate.CitationDocument(
        (str(index), *scope_tuple(OWNER), "public"), "canonical", sha256(text.encode()).hexdigest(),
        text, "", tuple(aliases),
    ) for index, text in enumerate(texts))
    return gate.CitationSnapshot("scope", "snapshot", docs)


@pytest.mark.parametrize("intent", [None, "exploration", "FACT_CITATION"])
def test_default_dispatch_never_collects_statistics_and_preserves_raw(store, monkeypatch, intent):
    add(store)
    def forbidden(**_kwargs):
        pytest.fail("ordinary RAW called the citation statistics API")
    monkeypatch.setattr(store, "read_raw_citation_records", forbidden)
    task = {"rerank_with_llm": False, "enable_llm_rerank": False}
    if intent is not None:
        task["evidence_intent"] = intent
    baseline = retrieval.search_raw_chunks.__wrapped__(store, query=QUERY, scope=OWNER,
                                                       task_context=task, limit=5)
    actual = recall(store, intent=intent)
    assert baseline and type(actual) is list
    assert json.dumps(actual, sort_keys=True) == json.dumps(baseline, sort_keys=True)


def test_absent_named_subject_rejects_once_after_recovery_and_neighbors(store, monkeypatch):
    add(store)
    events = []
    def failed_index(*_args, **_kwargs):
        events.append("index")
        raise RawRecallUnavailable("synthetic index failure")
    monkeypatch.setattr(retrieval, "_raw_api_search", failed_index)
    original_scan = retrieval._direct_raw_scan_candidates
    original_expand = retrieval._expand_ranked_turn_context
    original_assess = gate.assess_citation
    def scan(*args, **kwargs):
        events.append("scan")
        assert kwargs["require_complete"] is True
        return original_scan(*args, **kwargs)
    def expand(*args, **kwargs):
        events.append("neighbors")
        return original_expand(*args, **kwargs)
    def assess(*args, **kwargs):
        events.append("gate")
        return original_assess(*args, **kwargs)
    monkeypatch.setattr(retrieval, "_direct_raw_scan_candidates", scan)
    monkeypatch.setattr(retrieval, "_expand_ranked_turn_context", expand)
    monkeypatch.setattr(gate, "assess_citation", assess)
    rows = recall(store, context={"task_type": "locomo", "granularity": "turn"})
    assert rows == [] and rows.decision.reason == "missing_named_literal"
    assert rows.decision.status == "reject" and rows.decision.complete
    assert events == ["index", "scan", "neighbors", "gate"]
    assert rows.decision.candidate_source_digests  # Nonempty hits were suppressed.


def test_normal_refill_precedes_the_only_gate_and_cannot_resurrect_rejection(store, monkeypatch):
    record = add(store)
    monkeypatch.setattr(retrieval, "_raw_api_search",
                        lambda *_args, **_kwargs: [{"record": record, "base_score": 1.0}])
    events = []
    original_scan, original_assess = retrieval._direct_raw_scan_candidates, gate.assess_citation
    def scan(*args, **kwargs):
        events.append("refill")
        return original_scan(*args, **kwargs)
    def assess(*args, **kwargs):
        events.append("gate")
        return original_assess(*args, **kwargs)
    monkeypatch.setattr(retrieval, "_direct_raw_scan_candidates", scan)
    monkeypatch.setattr(gate, "assess_citation", assess)
    result = recall(store)
    assert result == [] and result.decision.reason == "missing_named_literal"
    assert events == ["refill", "gate"]


def test_positive_preserves_original_source_order_scores_and_digest(store):
    primary = add(store, EXPLICIT)
    add(store, "Halcyon deployment has an archive and a review checklist.")
    baseline = recall(store, intent=None)
    admitted = recall(store)
    assert baseline and list(admitted) == baseline
    assert admitted.decision.status == "related"  # No entailment assertion.
    assert admitted.decision.complete and admitted.decision.corpus_records == 2
    assert admitted.decision.snapshot_digest and admitted.decision.scope_digest
    assert admitted[0]["record"]["text"] == EXPLICIT
    digests = dict(admitted.decision.candidate_source_digests)
    assert digests[exact_ref(primary)] == sha256(EXPLICIT.encode()).hexdigest()
    assert admitted.decision.policy_version and admitted.decision.tokenizer_version
    json.dumps(admitted.decision.to_dict())


@pytest.mark.parametrize("field", ["tenant_id", "agent_id", "workspace_id", "user_id"])
def test_foreign_owner_cannot_supply_candidates_or_term_existence(store, field):
    add(store)
    foreign_scope = ScopeRef(**{**asdict(OWNER), field: "foreign"})
    foreign = add(store, EXPLICIT, scope=foreign_scope)
    result = recall(store)
    assert result == [] and result.decision.reason == "missing_named_literal"
    assert result.decision.corpus_records == 1
    assert "halcyon" in result.decision.missing_terms
    assert all(ref != exact_ref(foreign) for ref, _digest in result.decision.candidate_source_digests)


def test_source_partition_is_applied_before_statistics_and_hydration(store):
    authorized = add(store)
    excluded = add(store, EXPLICIT, source_id="excluded")
    result = recall(store, sources=["public"])
    assert result == [] and result.decision.reason == "missing_named_literal"
    assert result.decision.source_ids == ("public",) and result.decision.corpus_records == 1
    # Test the new statistics API directly: the legacy RAW collector retains
    # its own stricter error semantics and is not altered by this candidate.
    with store.locked() as sqlite:
        sqlite.execute("UPDATE records SET payload_json='broken' WHERE record_id=? AND source_id=?",
                       (excluded.record_id, "excluded"))
        sqlite.commit()
    records = store.read_raw_citation_records(owner_scopes=(scope_tuple(OWNER),),
        source_ids=("public",), kinds=("raw_chunk", "memory"), deadline=perf_counter() + 3.0,
        check_budget=lambda: None)
    assert [exact_ref(record) for record in records] == [exact_ref(authorized)]


def test_shared_owner_follows_existing_raw_scope_and_exact_opt_out(store):
    add(store)
    shared = ScopeRef(**{**asdict(OWNER), "user_id": ""})
    add(store, EXPLICIT, scope=shared)
    broad = recall(store)
    exact = recall(store, context={"exact_scope_only": True})
    assert broad and broad.decision.corpus_records == 2
    assert exact == [] and exact.decision.reason == "missing_named_literal"
    assert exact.decision.owner_scopes == (scope_tuple(OWNER),)
    assert broad.decision.scope_digest != exact.decision.scope_digest


def test_inactive_and_quality_rejected_rows_do_not_supply_statistics(store):
    add(store)
    add(store, EXPLICIT, status="withdrawn")
    add(store, EXPLICIT, meta={"quality": {"capture_decision": "reject"}})
    result = recall(store)
    assert result == [] and result.decision.corpus_records == 1
    assert "halcyon" in result.decision.missing_terms


def test_authorized_alias_preserves_uncertainty_and_never_rewrites_source(store):
    record = add(store, GENERIC, aliases=["Halcyon"])
    result = recall(store)
    assert result and result.decision.status == "related"
    assert result[0]["record"]["text"] == GENERIC
    assert dict(result.decision.candidate_source_digests)[exact_ref(record)] == sha256(GENERIC.encode()).hexdigest()


def test_candidate_pool_absence_does_not_prove_complete_corpus_absence(store, monkeypatch):
    generic = add(store)
    add(store, EXPLICIT)
    def restricted_pool(bound_store, *, scope_ref, allowed_source_ids, **_kwargs):
        candidates = retrieval._authoritative_raw_candidates(bound_store,
            candidates=[{"record": generic, "base_score": 1.0}], scope=scope_ref,
            source_ids=allowed_source_ids)
        return retrieval.rerank_raw_results(query=QUERY, results=candidates)
    monkeypatch.setattr(retrieval, "_search_raw_candidates", restricted_pool)
    result = recall(store)
    assert len(result) == 1 and result[0]["record"]["text"] == GENERIC
    assert result.decision.status == "related" and result.decision.corpus_records == 2
    assert "halcyon" in result.decision.matched_terms


@pytest.mark.parametrize("query,texts,status,reason", [
    (QUERY, (GENERIC,), "reject", "missing_named_literal"),
    (QUERY, (EXPLICIT,), "related", "related_only"),
    ('Where is "copper harbor" deployed?', ("copper launch", "harbor deployed"), "related", "related_only"),
    ('Where is "copper harbor" deployed?', ("copper deployment",), "reject", "missing_quoted_literal"),
    ("Where is Orion-Delta deployed?", ("Orion-Delta-Blue deployed in a lab.",), "reject", "missing_named_literal"),
    ("Where is Orion-Delta deployed?", ("Orion-Delta is deployed in a lab.",), "related", "related_only"),
    ("Where is O'Neil deployed?", ("O'Neil is deployed in a lab.",), "related", "related_only"),
    ("Where is Service.v2 deployed?", ("Service.v2 is deployed in a lab.",), "related", "related_only"),
    ("Where are Ｈａｌｃｙｏｎ manifests stored?", (EXPLICIT,), "related", "related_only"),
    ('“星湾”项目部署在哪里？', ("“星湾”项目部署在本地。",), "related", "related_only"),
    ('“星湾”项目部署在哪里？', ("“雪岭”项目部署在本地。",), "reject", "missing_quoted_literal"),
    ("星际遥测记录", ("花园种植手册。",), "related", "related_only"),
    ("星际 lunar telemetry", ("garden soils",), "related", "related_only"),
    ("lunar telemetry", ("garden soils",), "reject", "no_content_coverage"),
    ("lunar telemetry deployment", ("deployment reference",), "related", "related_only"),
    ("telemetry", ("garden soils",), "related", "related_only"),
    ("Which ID owns deployment?", ("deployment reference",), "related", "related_only"),
    ("Where are shipments stored?", ("shipment stored locally",), "related", "related_only"),
])
def test_pure_presence_rules_are_conservative_about_names_quotes_and_languages(query, texts, status, reason):
    decision = gate.assess_citation(query, snapshot(*texts))
    assert (decision.status, decision.reason) == (status, reason)
    assert decision.complete


def test_empty_authorized_corpus_has_explicit_complete_rejection(store):
    result = recall(store)
    assert result == [] and result.decision.complete
    assert result.decision.reason == "empty_authorized_corpus" and result.decision.corpus_records == 0


@pytest.mark.parametrize("limit,sources", [(0, None), (5, [])])
def test_explicit_no_delivery_requests_do_not_assert_a_missing_fact(store, limit, sources):
    result = recall(store, limit=limit, sources=sources)
    assert result == [] and result.decision.status == "related" and not result.decision.complete


def test_incomplete_statistics_are_unavailable_not_empty(store, monkeypatch):
    class Incomplete(list):
        degraded = True
    monkeypatch.setattr(store, "read_raw_citation_records", lambda **_kwargs: Incomplete())
    with pytest.raises(gate.CitationUnavailable) as caught:
        recall(store)
    assert caught.value.decision.reason == "collection_incomplete"
    assert not caught.value.decision.complete and caught.value.allow_recovery is False


def test_statistics_with_an_out_of_scope_reference_are_unavailable(store, monkeypatch):
    foreign = RecordEnvelope.create(kind="raw_chunk", title="notebook", content={"raw_text": EXPLICIT},
        scope=ScopeRef(**{**asdict(OWNER), "tenant_id": "foreign"}), source_id="public")
    monkeypatch.setattr(store, "read_raw_citation_records", lambda **_kwargs: [foreign])
    with pytest.raises(gate.CitationUnavailable) as caught:
        recall(store)
    assert caught.value.decision.reason == "snapshot_unavailable"
    assert not caught.value.decision.matched_terms


def test_unsupported_store_never_turns_missing_statistics_into_rejection():
    with pytest.raises(gate.CitationUnavailable) as caught:
        recall(object())
    assert caught.value.decision.reason == "unsupported_store" and not caught.value.decision.complete


def test_unrecoverable_raw_collection_failure_is_not_masked_by_empty_gate(store, monkeypatch):
    add(store)
    def failed(*_args, **_kwargs):
        raise RawRecallUnavailable("synthetic corruption", allow_recovery=False)
    monkeypatch.setattr(retrieval, "_search_raw_candidates", failed)
    with pytest.raises(gate.CitationUnavailable) as caught:
        recall(store)
    assert caught.value.decision.reason == "raw_collection_unavailable"


def test_corpus_mutation_between_snapshots_is_unavailable(store, monkeypatch):
    add(store)
    original = retrieval._search_raw_candidates
    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        add(store, EXPLICIT)
        return result
    monkeypatch.setattr(retrieval, "_search_raw_candidates", changing)
    with pytest.raises(gate.CitationUnavailable) as caught:
        recall(store)
    assert caught.value.decision.reason == "snapshot_changed"


def test_provider_text_mutation_cannot_pass_canonical_snapshot(store, monkeypatch):
    add(store, EXPLICIT)
    original = retrieval._search_raw_candidates
    def forged(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result
        result[0]["record"]["text"] = "forged answer"
        return result
    monkeypatch.setattr(retrieval, "_search_raw_candidates", forged)
    with pytest.raises(gate.CitationUnavailable) as caught:
        recall(store)
    assert caught.value.decision.reason == "candidate_snapshot_mismatch"


def test_existing_delivery_boundary_still_detects_post_gate_candidate_change(store, monkeypatch):
    record = add(store, EXPLICIT)
    original = gate.assess_citation
    def changed_after_snapshot(*args, **kwargs):
        decision = original(*args, **kwargs)
        record.content["raw_text"] = "This source has changed."
        store.append(record)
        return decision
    monkeypatch.setattr(gate, "assess_citation", changed_after_snapshot)
    with pytest.raises(gate.CitationUnavailable) as caught:
        recall(store)
    assert caught.value.decision.reason == "raw_collection_unavailable"


def test_canonical_payload_digest_failure_is_unavailable(store):
    record = add(store, EXPLICIT)
    with store.locked() as sqlite:
        sqlite.execute("UPDATE records SET payload_digest=? WHERE record_id=?", ("0" * 64, record.record_id))
        sqlite.commit()
    with pytest.raises(gate.CitationUnavailable) as caught:
        recall(store)
    assert caught.value.decision.status == "unavailable" and not caught.value.decision.complete


@pytest.mark.parametrize("bound,value,reason", [
    ("MAX_ROWS", 1, "row_limit"),
    ("MAX_RECORD_BYTES", 16, "payload_limit"),
    ("MAX_CORPUS_BYTES", 16, "payload_limit"),
    ("MAX_TEXT_CHARS", 8, "text_limit"),
    ("MAX_GATE_CPU_SECONDS", -1.0, "cpu_limit"),
])
def test_statistics_limits_fail_closed_without_truncation(store, monkeypatch, bound, value, reason):
    add(store)
    add(store, EXPLICIT)
    monkeypatch.setattr(gate, bound, value)
    with pytest.raises(gate.CitationUnavailable) as caught:
        recall(store)
    assert caught.value.decision.reason == reason and caught.value.allow_recovery is False


def test_deadline_is_explicit_and_does_not_start_statistics(store, monkeypatch):
    def forbidden(**_kwargs):
        pytest.fail("expired request reached statistics")
    monkeypatch.setattr(store, "read_raw_citation_records", forbidden)
    with pytest.raises(gate.CitationUnavailable) as caught:
        recall(store, context={"_recall_deadline_monotonic": perf_counter() - 1.0})
    assert caught.value.decision.reason == "deadline"


@pytest.mark.parametrize("query", ["x" * 4097, " ".join("token" + str(i) for i in range(129))])
def test_query_caps_are_unavailable_instead_of_silently_shortening_the_question(query):
    with pytest.raises(gate.CitationUnavailable) as caught:
        gate.assess_citation(query, snapshot(GENERIC))
    assert caught.value.decision.reason == "query_limit"


def test_additional_policy_filters_fail_unavailable_before_statistics(store, monkeypatch):
    def forbidden(**_kwargs):
        pytest.fail("unsupported policy was ignored")
    monkeypatch.setattr(store, "read_raw_citation_records", forbidden)
    with pytest.raises(gate.CitationUnavailable) as caught:
        recall(store, context={"allowed_sources": ["restricted"]})
    assert caught.value.decision.reason == "unsupported_authority_filter"


def test_statistics_do_not_join_or_rollback_callers_transaction(store):
    with store.locked() as sqlite:
        sqlite.execute("BEGIN")
        try:
            with pytest.raises(gate.CitationUnavailable):
                recall(store)
            assert sqlite.in_transaction
        finally:
            sqlite.rollback()


def test_selection_and_hydration_use_one_read_transaction(store, monkeypatch):
    add(store, EXPLICIT)
    # Force the existing writer fallback under the owned lock; observe both reads.
    with store.locked() as sqlite:
        original = sqlite.get_by_exact_ref
        observed = []
        def hydrated(*args, **kwargs):
            if kwargs.get("fail_on_unavailable"):
                observed.append(sqlite.in_transaction)
            return original(*args, **kwargs)
        monkeypatch.setattr(sqlite, "get_by_exact_ref", hydrated)
        assert recall(store)
        assert observed == [True, True]
        assert not sqlite.in_transaction


def test_budget_failure_rolls_back_only_the_statistics_transaction(store):
    add(store)
    checks = []
    def exhausted():
        checks.append(True)
        if len(checks) == 4:
            raise gate.CitationUnavailable("cpu_limit")
    with store.locked() as sqlite:
        with pytest.raises(gate.CitationUnavailable):
            store.read_raw_citation_records(owner_scopes=(scope_tuple(OWNER),),
                source_ids=("public",), kinds=("raw_chunk", "memory"),
                deadline=perf_counter() + 3.0, check_budget=exhausted)
        assert len(checks) == 4 and not sqlite.in_transaction


def test_gate_module_is_registered_on_critical_release_surfaces():
    assert _domains_for_change(repo=Path("/nonexistent"), path="eimemory/raw/citation_gate.py",
        ancestor="0" * 40, current="1" * 40) == {"memory.recall", "channel.delivery", "storage.integrity"}
