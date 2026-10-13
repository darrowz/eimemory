"""Audit F05/F08/F09/F10: real persistence and configured recall paths."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.production_query_auto_review import derive_query_features
from eimemory.evaluation.production_recall import run_production_recall_eval
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.scheduler.result_contract import nightly_result_diagnostics
from eimemory.storage.runtime_store import RuntimeStore
from test_production_query_auto_review import (
    _keys, _seed, _collect, BASE_SCOPE, CHANNEL, resolve_channel_scope,
    auto_review_pending_production_queries,
)


def _memory(runtime, scope):
    record = RecordEnvelope.create(
        kind="memory", title="Certificate rotation schedule",
        summary="Certificate rotation schedule is sixty days.",
        content={"memory_type": "fact", "text": "Certificate rotation schedule is sixty days."},
        source="fixture.memory", scope=scope, meta={"force_capture": True})
    runtime.store.append(record)
    return record


@pytest.mark.parametrize("contract", [None, "legacy.v1", "future-unknown.v2", "known_item_smoke.v1"])
def test_diagnostic_success_never_becomes_formal_acceptance_after_reopen(tmp_path, contract):
    scope = ScopeRef(agent_id="audit", workspace_id="quality", user_id="operator")
    with Runtime.create(root=tmp_path) as rt:
        target = _memory(rt, scope)
        dataset = {"scope": asdict(scope), "cases": [
            {"case_id": f"diagnostic-{i}", "query": target.title, "expected_record_ids": [target.record_id]}
            for i in range(10)]}
        if contract is not None:
            dataset["evaluation_contract"] = contract
        report = run_production_recall_eval(rt, dataset, seed=False, persist_report=True)
        assert report["pass_count"] == 10
        assert report["accepted"] is False and report["dataset_kind"] == "diagnostic"
        receipt = report["persisted_record_id"]
    with Runtime.create(root=tmp_path) as rt:
        saved = rt.store.get_by_id(receipt).content["report"]
        for quality in (report, saved):
            result = nightly_result_diagnostics({"recall_quality": quality,
                                                "recall_quality_gate": quality["quality_gate"]}, [])
            assert result["recall_quality_accepted"] is False
            assert result["recall_quality_evidence"]["release_authority_verified"] is False
            assert result["recall_quality_evidence"]["formal_business_evidence"] is False


def test_unsupported_formal_claim_does_not_replace_a_verified_report():
    from eimemory.evaluation.real_query_schema import PRODUCTION_REAL_QUERY_POLICY, PRODUCTION_REAL_QUERY_REPORT_SCHEMA
    gate = {"ok": True, "vacuous": False, "recall_quality_evidence": {
        "evaluation_contract": PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
        "formal_business_evidence": True, "label_trust_accepted": True,
        "release_authority_verified": True, "authority_record_id": "prg_a"}}
    quality = {"schema": PRODUCTION_REAL_QUERY_REPORT_SCHEMA, "policy_schema": PRODUCTION_REAL_QUERY_POLICY,
               "dataset_kind": "production", "accepted": True, "gate_status": "accepted",
               "persisted": True, "persisted_record_id": "prg_a", "quality_gate": gate}
    assert nightly_result_diagnostics({"recall_quality": quality, "recall_quality_gate": gate}, [])["recall_quality_accepted"]
    for field, value in (("schema", "unknown"), ("policy_schema", "old"), ("accepted", False),
                         ("persisted", False), ("persisted_record_id", "other")):
        bad = {**quality, field: value}
        assert not nightly_result_diagnostics({"recall_quality": bad, "recall_quality_gate": gate}, [])["recall_quality_accepted"]
    for field in ("formal_business_evidence", "label_trust_accepted", "release_authority_verified"):
        bad_gate = deepcopy(gate)
        bad_gate["recall_quality_evidence"][field] = False
        assert not nightly_result_diagnostics({"recall_quality": {**quality, "quality_gate": bad_gate},
                                              "recall_quality_gate": bad_gate}, [])["recall_quality_accepted"]


@pytest.mark.parametrize("query", ["轮换证书需要遵循哪些具体步骤", "我想知道上周部署服务器的时候到底发生了什么问题",
                                   "归档文件需要执行哪些步骤", "如何取消每小时监督任务"])
def test_chinese_questions_have_bounded_non_sentence_features(query):
    features, reason = derive_query_features(query)
    assert reason == ""
    assert 2 <= len(features["terms"]) <= 8
    assert all(2 <= len(term) <= 3 for term in features["terms"])
    assert query not in json.dumps(features, ensure_ascii=False)
    assert "".join(features["terms"]) != query


@pytest.mark.parametrize("query", ["请告诉我呢", "记忆召回", "证书", "memory recall"])
def test_language_adaptation_does_not_manufacture_low_signal_labels(query):
    assert derive_query_features(query)[1] == "query_features_low_signal"


@pytest.mark.parametrize("query", [
    "张三先生需要轮换证书具体步骤", "姓名：张三，如何轮换证书", "证书联系 alice@example.test 如何轮换",
    "证书轮换 password=synthetic-secret", "轮换证书 ghp_SYNTHETIC000000000000",
    "Dr Smith certificate rotation instructions",
    "请联系alice@example.test确认轮换证书",
    "令牌ghp_syntheticcredentialtoken用于轮换证书",
    "令牌sk-syntheticcredentialtoken用于轮换证书",
    "请联系ａｌｉｃｅ＠ｅｘａｍｐｌｅ．ｔｅｓｔ确认轮换证书",
    "令牌ｇｈｐ＿ｓｙｎｔｈｅｔｉｃｃｒｅｄｅｎｔｉａｌｔｏｋｅｎ用于轮换证书",
    "证书轮换password=synthetic-secret", "证书轮换api_key=synthetic-secret",
])
def test_privacy_markers_are_checked_before_multilingual_tokenization(query):
    assert derive_query_features(query) == ({}, "query_features_not_redacted")


def test_chinese_positive_review_still_requires_independent_delivery_evidence(tmp_path):
    with Runtime.create(root=tmp_path) as rt:
        _seed(rt, 85001, query="轮换证书需要遵循哪些具体步骤")
        _seed(rt, 85002, query="轮换证书需要遵循哪些具体步骤", proof=False, state="not_used")
        _collect(rt)
        report = auto_review_pending_production_queries(rt, scope=BASE_SCOPE)
        assert report["accepted_count"] == 1 and report["closed_without_label_count"] == 1
        assert report["reason_counts"]["not_passed"] == {"independent_signal_agreement_missing": 1}
        accepted = report["accepted_record_ids"][0]
    with Runtime.create(root=tmp_path) as rt:
        again = auto_review_pending_production_queries(rt, scope=BASE_SCOPE)
        assert again["already_accepted_count"] == 1
        assert rt.store.get_by_id(accepted).meta["label_authority"] == "auto_review"


def test_review_cursor_survives_restart_and_new_arrivals(tmp_path):
    with Runtime.create(root=tmp_path) as rt:
        for index in range(3):
            _seed(rt, 85100 + index, with_candidate=False)
        ids = set(_collect(rt))
        first = auto_review_pending_production_queries(rt, scope=BASE_SCOPE, channel=CHANNEL, limit=2)
        assert first["status"] == "partial" and first["open_review_count"] is None
        assert first["coverage"]["cycle_remaining_count"] == 1
        visited = {r["pending_record_id"] for r in first["review_results"]}
    with Runtime.create(root=tmp_path) as rt:
        _seed(rt, 85110, with_candidate=False)
        all_ids = set(_collect(rt))
        second = auto_review_pending_production_queries(rt, scope=BASE_SCOPE, channel=CHANNEL, limit=2)
        visited.update(r["pending_record_id"] for r in second["review_results"])
        assert ids <= visited  # a new arrival cannot move the old snapshot window
        assert second["coverage"]["newer_case_count"] == 1
        assert second["status"] == "partial"
        for _ in range(2):
            report = auto_review_pending_production_queries(rt, scope=BASE_SCOPE, channel=CHANNEL, limit=2)
            visited.update(r["pending_record_id"] for r in report["review_results"])
        assert all_ids <= visited
        assert report["status"] == "completed" and report["open_review_count"] == 0
        summary = nightly_result_diagnostics({"production_recall_auto_review": first}, [])
        assert summary["recall_label_auto_review"]["open_review_count"] is None
        assert summary["recall_label_auto_review"]["coverage"]["cycle_remaining_count"] == 1


def test_default_review_window_reaches_the_501st_case_after_restart(tmp_path):
    with Runtime.create(root=tmp_path) as rt:
        _seed(rt, 86000, with_candidate=False)
        first_id = _collect(rt)[0]
        for index in range(86001, 86501):
            _seed(rt, index, with_candidate=False)
        ids = set(_collect(rt)) | {first_id}
        assert len(ids) == 501
        first = auto_review_pending_production_queries(rt, scope=BASE_SCOPE, channel=CHANNEL)
        assert first["scanned_count"] == 500
        assert first["status"] == "partial" and first["coverage"]["cycle_remaining_count"] == 1
        visited = {row["pending_record_id"] for row in first["review_results"]}
    with Runtime.create(root=tmp_path) as rt:
        last = auto_review_pending_production_queries(rt, scope=BASE_SCOPE, channel=CHANNEL)
        assert last["scanned_count"] == 1
        assert last["status"] == "completed" and last["coverage"]["cycle_complete"]
        visited.update(row["pending_record_id"] for row in last["review_results"])
        assert visited == ids


def test_review_cursor_lost_row_binding_restarts_without_skipping(tmp_path):
    from eimemory.evaluation.review_progress import load_review_batch, complete_review_batch
    scope = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    with Runtime.create(root=tmp_path) as rt:
        for index in range(3):
            _seed(rt, 86600 + index, with_candidate=False)
        _collect(rt)
        batch = load_review_batch(rt, scope=scope, limit=1)
        assert complete_review_batch(rt, scope=scope, batch=batch)
        # A rebuilt index or restored DB can move an implicit row id. The
        # record identity guard invalidates the old scheduling position.
        cursor = rt.store.get_by_exact_ref(batch["cursor_id"], scope=scope, source_id="review_progress")
        cursor.content["after"][0] = 2 ** 62
        rt.store.rewrite(cursor)
    with Runtime.create(root=tmp_path) as rt:
        resumed = load_review_batch(rt, scope=scope, limit=3)
        assert len(resumed["records"]) == 3


def test_review_cursor_dry_run_and_failed_receipt_do_not_skip_work(tmp_path, monkeypatch):
    import eimemory.evaluation.production_query_auto_review as review
    with Runtime.create(root=tmp_path) as rt:
        for index in range(3):
            _seed(rt, 85200 + index, with_candidate=False)
        _collect(rt)
        dry = auto_review_pending_production_queries(rt, scope=BASE_SCOPE, channel=CHANNEL, limit=2, dry_run=True)
        original = review._write_not_evaluable_receipt
        def failure(*args, **kwargs):
            raise OSError("synthetic receipt unavailable")
        monkeypatch.setattr(review, "_write_not_evaluable_receipt", failure)
        with pytest.raises(OSError):
            auto_review_pending_production_queries(rt, scope=BASE_SCOPE, channel=CHANNEL, limit=2)
        monkeypatch.setattr(review, "_write_not_evaluable_receipt", original)
    with Runtime.create(root=tmp_path) as rt:
        real = auto_review_pending_production_queries(rt, scope=BASE_SCOPE, channel=CHANNEL, limit=2)
        assert [r["pending_record_id"] for r in real["review_results"]] == [r["pending_record_id"] for r in dry["review_results"]]


def test_review_cursor_compare_and_swap_cannot_move_progress_backwards(tmp_path):
    from eimemory.evaluation.review_progress import load_review_batch, complete_review_batch
    scope = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    with Runtime.create(root=tmp_path) as rt:
        for index in range(3):
            _seed(rt, 85300 + index, with_candidate=False)
        _collect(rt)
        slow = load_review_batch(rt, scope=scope, limit=1)
        fast = load_review_batch(rt, scope=scope, limit=2)
        assert complete_review_batch(rt, scope=scope, batch=fast) is True
        assert complete_review_batch(rt, scope=scope, batch=slow) is False
        rest = load_review_batch(rt, scope=scope, limit=2)
        assert len(rest["records"]) == 1


@pytest.mark.parametrize("scope", [
    ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="operator"),
    ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE)),
])
def test_review_cursor_native_identity_survives_repair_and_resume(tmp_path, scope):
    from eimemory.evaluation.review_progress import load_review_batch, complete_review_batch
    from eimemory.identity import is_hongtu_scope, needs_hongtu_identity_repair
    from eimemory.identity_ops import repair_hongtu_identity
    from eimemory.metadata import business_metadata

    with Runtime.create(root=tmp_path) as rt:
        for index in range(3):
            pending = RecordEnvelope.create(
                kind="evaluation_packet", title="Pending review", content={},
                source="eimemory.production_recall.pending_case", source_id=str(index), scope=scope,
                provenance={"identity_scope_preserved": True})
            rt.store.append(pending)
        slow = load_review_batch(rt, scope=scope, limit=1)
        fast = load_review_batch(rt, scope=scope, limit=2)
        assert complete_review_batch(rt, scope=scope, batch=fast)
        cursor = rt.store.get_by_exact_ref(fast["cursor_id"], scope=scope, source_id="review_progress")
        assert cursor.provenance["identity_scope_preserved"] is True
        assert not needs_hongtu_identity_repair(cursor)
        if is_hongtu_scope(scope):
            assert business_metadata(cursor.meta)["identity"] == "hongtu"
            assert business_metadata(cursor.meta)["identity_stamped_on_ingest"] is True
        before = cursor.to_dict()
        repair = repair_hongtu_identity(rt, apply=True, scope=scope)
        assert cursor.record_id not in repair["repaired_record_ids"]
        assert rt.store.get_by_exact_ref(cursor.record_id, scope=scope, source_id="review_progress").to_dict() == before
        assert not complete_review_batch(rt, scope=scope, batch=slow)
    with Runtime.create(root=tmp_path) as rt:
        rest = load_review_batch(rt, scope=scope, limit=2)
        assert len(rest["records"]) == 1
        assert rest["next"]["high_water"] == fast["next"]["high_water"]
        assert complete_review_batch(rt, scope=scope, batch=rest)


def test_review_window_is_one_snapshot_across_an_external_commit(tmp_path, monkeypatch):
    from eimemory.evaluation.review_progress import load_review_batch
    from eimemory.storage.sqlite_store import SqliteRecordStore
    scope = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    with Runtime.create(root=tmp_path) as rt, Runtime.create(root=tmp_path) as external:
        for index in range(3):
            _seed(rt, 86700 + index, with_candidate=False)
        ids = _collect(rt)
        original = rt.store.get_by_id(ids[0])
        arrival = RecordEnvelope.create(kind=original.kind, title=original.title, summary=original.summary,
                                       content=original.content, source=original.source,
                                       source_id=original.source_id, scope=original.scope,
                                       status=original.status, meta=original.meta)
        execute = SqliteRecordStore.execute
        inserted = False

        def interleave(db, sql, parameters=()):
            nonlocal inserted
            cursor = execute(db, sql, parameters)
            if not inserted and sql.startswith("SELECT COUNT(*) FROM records WHERE kind='evaluation_packet'"):
                inserted = True
                external.store.append(arrival)
            return cursor

        monkeypatch.setattr(SqliteRecordStore, "execute", interleave)
        batch = load_review_batch(rt, scope=scope, limit=10)
        assert inserted
        assert batch["coverage"]["total_case_count"] == 3
        assert batch["coverage"]["batch_case_count"] == 3
        assert batch["coverage"]["boundary"] == "fixed_high_water_snapshot"
        assert arrival.record_id not in {row.record_id for row in batch["records"]}
        assert load_review_batch(rt, scope=scope, limit=10)["coverage"]["total_case_count"] == 4


def test_complete_dry_run_does_not_claim_persisted_review_closure(tmp_path):
    with Runtime.create(root=tmp_path) as rt:
        _seed(rt, 86800, with_candidate=False)
        _collect(rt)
        report = auto_review_pending_production_queries(rt, scope=BASE_SCOPE, channel=CHANNEL, dry_run=True)
        assert report["status"] == "dry_run"
        assert report["coverage"]["cycle_complete"] is True
        assert report["open_review_count"] is None and report["pending_count"] is None
        assert report["receipt_count"] == 0
        summary = nightly_result_diagnostics({"production_recall_auto_review": report}, [])
        assert summary["recall_label_auto_review"]["open_review_count"] is None


class _ObservedSQLiteSource(SQLiteCandidateSource):
    def __init__(self, store):
        super().__init__(store)
        self.calls = 0
    def search(self, request):
        self.calls += 1
        return super().search(request)


def test_memory_benchmark_uses_configured_native_source_after_restart(tmp_path):
    scope = ScopeRef(agent_id="audit", workspace_id="benchmark", user_id="operator")
    with Runtime.create(root=tmp_path) as rt:
        target = _memory(rt, scope)
    store = RuntimeStore(tmp_path)
    source = _ObservedSQLiteSource(store)
    with Runtime(store, candidate_source=source) as rt:
        report = rt.run_memory_eval_ci({"scope": asdict(scope), "threshold": 1.0, "cases": [
            {"case_id": "positive", "query": target.title, "expect_any_text": ["sixty days"]},
            {"case_id": "negative", "query": "No known lunar shipping policy", "expect_any_text": ["missing truth"]},
        ]})
        assert source.calls > 0
        assert report["pass_count"] == 1 and report["fail_count"] == 1
        assert report["passed_threshold"] is False
        identity = report["retrieval_identity"]
        assert identity["candidate_source_type"] == "_ObservedSQLiteSource"
        assert identity["status"] == "reported" and len(identity["engine_digest"]) == 64
