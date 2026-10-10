"""Durable review conclusions retain label authority and permit evidence retries."""
import json

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from test_production_query_auto_review import (
    BASE_SCOPE, CHANNEL, SEM_SOURCE, SEM_VERSION, _keys, runtime,
    _seed, _collect, _digest, resolve_channel_scope,
    auto_review_pending_production_queries, build_production_query_dataset,
)


def test_every_scanned_case_has_a_durable_conclusion_and_reruns_are_idempotent(tmp_path):
    root = tmp_path / "durable reviews"
    with Runtime.create(root=root) as rt:
        for index, kwargs in enumerate([{}, {"semantic": None}, {"proof": False, "state": "not_used"},
                                        {"semantic": "unrelated"}, {"with_candidate": False}], start=801):
            _seed(rt, index, **kwargs)
        _collect(rt)
        report = auto_review_pending_production_queries(rt, scope=BASE_SCOPE)
        assert report["scanned_count"] == report["reviewed_count"] == 5
        assert report["passed_count"] == report["accepted_count"] == 1
        assert report["not_passed_count"] == 4
        assert report["pending_count"] == report["open_review_count"] == 0
        assert build_production_query_dataset(rt, scope=BASE_SCOPE)["ready"] is False
        results = report["review_results"]
    with Runtime.create(root=root) as rt:
        for result in results:
            record = rt.store.get_by_id(result["review_receipt_id"])
            assert record.content["review_verdict"] == result["review_verdict"]
            assert record.content["review_complete"] is True
            assert record.content["reasons"] == result["reasons"]
            assert record.content["signature"]
        repeated = auto_review_pending_production_queries(rt, scope=BASE_SCOPE)
        assert repeated["passed_count"] == 1 and repeated["not_passed_count"] == 4
        assert repeated["already_accepted_count"] == 1
        first_failed = {r["review_receipt_id"] for r in results if r["review_verdict"] == "fail"}
        again_failed = {r["review_receipt_id"] for r in repeated["review_results"] if r["review_verdict"] == "fail"}
        assert first_failed == again_failed
        terminals = rt.store.list_records_by_meta_value(
            kinds=["evaluation_packet"], scope=ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE)),
            meta_key="report_type", meta_value="production_recall_evaluation_terminal", status="active", limit=20)
        assert len(terminals) == 3  # contradiction has a rejected receipt instead


def test_case_execution_failure_does_not_stop_other_reviews(runtime, monkeypatch):
    import eimemory.evaluation.production_query_auto_review as review
    _seed(runtime, 811)
    _seed(runtime, 812)
    bad_id = sorted(_collect(runtime))[0]
    original = review.assess_pending_case
    def assess(rt, pending, **kwargs):
        if pending.record_id == bad_id:
            raise RuntimeError("private-provider-token")
        return original(rt, pending, **kwargs)
    monkeypatch.setattr(review, "assess_pending_case", assess)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["ok"] is False and report["blocked_reason"] == "auto_review_execution_failed"
    assert report["passed_count"] == report["not_passed_count"] == 1
    assert report["pending_count"] == report["open_review_count"] == 0
    assert report["reason_counts"]["not_passed"] == {"auto_review_execution_failed": 1}
    for result in report["review_results"]:
        saved = runtime.store.get_by_id(result["review_receipt_id"])
        assert saved.content["review_verdict"] == result["review_verdict"]
        assert "private-provider-token" not in json.dumps(saved.content)
    assert "private-provider-token" not in json.dumps(report)


def test_new_independent_evidence_can_pass_a_previously_failed_review(runtime):
    _, decision_id = _seed(runtime, 821, semantic=None)
    pending_id = _collect(runtime)[0]
    first = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert first["not_passed_count"] == 1 and first["accepted_count"] == 0
    failed_id = first["review_results"][0]["review_receipt_id"]
    scope = resolve_channel_scope(CHANNEL, BASE_SCOPE)
    exact = ScopeRef.from_dict(scope)
    decision = runtime.store.load_proactive_decision(decision_id)
    delivered = [i for i in decision["items"] if i["ever_injected"]]
    identity = _digest(dict(version=SEM_VERSION, decision_id=decision_id, scope=decision["scope"],
                            source_ids=decision["source_ids"], query_digest=decision["query_digest"],
                            release_identity=decision["release_identity"],
                            delivered=[(i["record_id"], i.get("source_id"), i.get("render_digest")) for i in delivered]))
    observation = dict(version=SEM_VERSION, scope=scope, evaluation_identity=identity,
                       decision_digest=_digest(decision_id), record_digests=[_digest(i["record_id"]) for i in delivered],
                       verdict="relevant", reason="evaluated", relevance=["relevant"], off_topic=False,
                       duplicates=False, unanswered=False, decision_surface=decision["task_type"], channel=CHANNEL)
    runtime.store.append(RecordEnvelope.create(
        kind="evaluation_packet", title="Independent relevance evidence", summary="evaluated",
        content=observation, scope=exact, source=SEM_SOURCE,
        meta={"semantic_monitor_digest": _digest(observation), "report_type": SEM_VERSION,
              "semantic_monitor_identity": identity}))
    later = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert later["passed_count"] == later["accepted_count"] == 1 and later["not_passed_count"] == 0
    assert later["pending_count"] == later["open_review_count"] == 0
    assert later["review_results"][0]["inputs_digest"] != first["review_results"][0]["inputs_digest"]
    assert runtime.store.get_by_id(failed_id).content["review_verdict"] == "fail"
    assert runtime.store.get_by_id(pending_id).status == "active"
    assert build_production_query_dataset(runtime, scope=BASE_SCOPE)["progress"]["accepted_case_count"] == 1


def test_acceptance_execution_failure_is_concluded_without_private_errors(runtime, monkeypatch):
    import eimemory.evaluation.production_query_auto_review as review
    _seed(runtime, 831)
    _collect(runtime)
    def fail(*args, **kwargs):
        raise RuntimeError("private-provider-token")
    monkeypatch.setattr(review, "accept_auto_reviewed_production_query", fail)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["ok"] is False and report["accepted_count"] == report["passed_count"] == 0
    result = report["review_results"][0]
    saved = runtime.store.get_by_id(result["review_receipt_id"])
    assert saved.content["review_verdict"] == "fail"
    assert saved.content["reasons"] == ["auto_review_execution_failed"]
    assert "private-provider-token" not in json.dumps(report)


def test_unvalidated_acceptance_return_cannot_certify_a_pass(runtime, monkeypatch):
    import eimemory.evaluation.production_query_auto_review as review
    _seed(runtime, 841)
    pending_id = _collect(runtime)[0]
    monkeypatch.setattr(review, "accept_auto_reviewed_production_query",
                        lambda *args, **kwargs: {"record_id": pending_id})
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["accepted_count"] == report["passed_count"] == 0
    assert report["not_passed_count"] == 1
    saved = runtime.store.get_by_id(report["review_results"][0]["review_receipt_id"])
    assert saved.content["review_verdict"] == "fail"
    assert saved.content["reasons"] == ["auto_accept_failed:accepted_record_invalid"]
