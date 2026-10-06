"""Automatic pending-case collection and decision retention pins (cycle 3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.evaluation.production_query_auto_review import auto_review_pending_production_queries
from eimemory.evaluation.production_query_dataset import (
    build_production_query_dataset,
    collect_pending_production_queries,
)
from eimemory.evaluation.real_query_schema import PRODUCTION_RECALL_AUTO_REVIEW_FLAG
from eimemory.retrieval.query_identity import effective_query_digest, query_text_digest
from eimemory.scheduler import jobs
from eimemory.storage.sqlite_store import MAX_PINNED_PROACTIVE_DECISIONS, PENDING_DECISION_PIN_MAX_AGE_DAYS
from test_production_query_auto_review import BASE_SCOPE, CHANNEL, _seed


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", "fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz")
    monkeypatch.setenv("EIMEMORY_CAPTURE_ORIGINAL_QUERY", "1")
    monkeypatch.delenv("EIMEMORY_CAPTURE_QUERY_SCOPES", raising=False)
    monkeypatch.delenv(PRODUCTION_RECALL_AUTO_REVIEW_FLAG, raising=False)
    monkeypatch.delenv(jobs.PRODUCTION_RECALL_AUTO_COLLECT_FLAG, raising=False)


@pytest.fixture
def runtime(tmp_path):
    rt = Runtime.create(root=tmp_path / "runtime")
    try:
        yield rt
    finally:
        rt.close()


def _decision_id(label: str) -> str:
    return "pd:" + sha256(label.encode()).hexdigest()[:32]


def _flood(runtime, count: int, *, cap: int, prefix: str = "flood") -> list[str]:
    """Insert newer unrelated decisions, pruning with a small ring cap."""
    scope = resolve_channel_scope(CHANNEL, BASE_SCOPE)
    ids = []
    for index in range(count):
        decision_id = _decision_id(f"{prefix}-{index}")
        query = f"unrelated flood query {index}"
        runtime.store.record_proactive_decision({
            "decision_id": decision_id, "channel": CHANNEL, "scope": scope,
            "source_key": sha256(b"hermes").hexdigest(), "source_ids": ["hermes"],
            "session_id": f"fs-{index}", "turn_id": f"ft-{index}", "query_id": f"fq-{index}",
            "query_digest": query_text_digest(query),
            "effective_query_digest": effective_query_digest("research.task", query),
            "task_type": "research.task", "policy_version": "proactive.test.v1",
            "release_identity": {"release_commit": "a" * 40, "release_version": "1.14.16",
                                 "deployment_receipt_id": "receipt", "release_session_id": "session"},
            "release_bound": True, "control_cohort": False, "pair_id": f"fp-{index}",
            "created_at": f"2099-01-01T00:00:{index:02d}+00:00",
        }, [], [], max_global_decisions=cap)
        ids.append(decision_id)
    return ids


def _exists(runtime, decision_id: str) -> bool:
    return runtime.store.load_proactive_decision(decision_id) is not None


def _pins(runtime) -> dict[str, str]:
    with runtime.store.locked() as db:
        return {str(row["decision_id"]): str(row["reason"]) for row in db.execute(
            "SELECT decision_id, reason FROM proactive_decision_retention_pins")}


def test_unpinned_decision_is_still_pruned_by_the_ring(runtime):
    _, decision_id = _seed(runtime, 1)
    _flood(runtime, 4, cap=3)
    assert not _exists(runtime, decision_id)


def test_collected_case_pins_its_decision_through_the_ring_prune(runtime):
    _, decision_id = _seed(runtime, 2)
    report = collect_pending_production_queries(runtime, scope=BASE_SCOPE, channel=CHANNEL)
    assert report["new_count"] == 1 and report["existing_count"] == 0
    assert report["retention_pins"]["pending_case"]["pinned_count"] == 1
    assert _pins(runtime) == {decision_id: "pending_case"}
    flood = _flood(runtime, 6, cap=3)
    assert _exists(runtime, decision_id)
    # The ring still keeps exactly `cap` unpinned decisions.
    assert [d for d in flood if _exists(runtime, d)] == flood[-3:]


def test_pinned_decision_keeps_the_case_auto_reviewable_after_flood(runtime):
    _, decision_id = _seed(runtime, 3)
    collect_pending_production_queries(runtime, scope=BASE_SCOPE, channel=CHANNEL)
    _flood(runtime, 6, cap=2)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["accepted_count"] == 1
    assert "pending_capture_decision_missing" not in report["reason_counts"]["rejected"]
    # Acceptance re-validates the decision; the accepted pin keeps it valid.
    collect_pending_production_queries(runtime, scope=BASE_SCOPE, channel=CHANNEL)
    # Flood decisions are real empty-result observations, so they are pending pins.
    assert _pins(runtime)[decision_id] == "accepted_case"
    _flood(runtime, 6, cap=2, prefix="later")
    built = build_production_query_dataset(runtime, scope=BASE_SCOPE)
    assert built["progress"]["accepted_case_count"] == 1


def test_pin_bounds_cap_expiry_priority_and_orphans(runtime):
    ids = [_seed(runtime, 10 + index)[1] for index in range(4)]
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    old = now - timedelta(days=PENDING_DECISION_PIN_MAX_AGE_DAYS + 1)
    store = runtime.store
    # Missing decisions are never pinned.
    report = store.pin_proactive_decisions([_decision_id("never-existed")], reason="pending_case", now=now)
    assert report["decision_missing_count"] == 1 and report["total_pinned"] == 0
    # Pending pins expire after the vault window; accepted pins do not.
    store.pin_proactive_decisions([ids[0]], reason="pending_case", now=old)
    store.pin_proactive_decisions([ids[1]], reason="accepted_case", now=old)
    report = store.pin_proactive_decisions([], reason="pending_case", now=now)
    assert report["expired_count"] == 1 and _pins(runtime) == {ids[1]: "accepted_case"}
    # Over the cap, pending pins are released before accepted ones.
    store.pin_proactive_decisions([ids[2]], reason="pending_case", now=now - timedelta(hours=1), max_pins=2)
    store.pin_proactive_decisions([ids[3]], reason="pending_case", now=now, max_pins=2)
    assert _pins(runtime) == {ids[1]: "accepted_case", ids[3]: "pending_case"}
    # accepted_case upgrades a pending pin and is never downgraded.
    store.pin_proactive_decisions([ids[3]], reason="accepted_case", now=now, max_pins=2)
    store.pin_proactive_decisions([ids[3]], reason="pending_case", now=now, max_pins=2)
    assert _pins(runtime)[ids[3]] == "accepted_case"
    with pytest.raises(ValueError):
        store.pin_proactive_decisions([ids[0]], reason="anything", now=now)
    assert MAX_PINNED_PROACTIVE_DECISIONS == 2048


def test_orphan_pins_are_released(runtime):
    _, decision_id = _seed(runtime, 20)
    runtime.store.pin_proactive_decisions([decision_id], reason="pending_case")
    with runtime.store.locked() as db:
        db.execute("DELETE FROM proactive_decision_items WHERE decision_id=?", (decision_id,))
        db.execute("DELETE FROM proactive_decisions WHERE decision_id=?", (decision_id,))
        db.commit()
    report = runtime.store.pin_proactive_decisions([], reason="pending_case")
    assert report["released_orphan_count"] == 1 and report["total_pinned"] == 0


def test_nightly_collection_step_is_idempotent_and_does_not_accept(runtime):
    _seed(runtime, 30)
    _seed(runtime, 31, delivered=False)
    first = jobs._run_production_recall_collection(runtime, scope=BASE_SCOPE)
    assert first["ok"] is True and first["status"] == "completed"
    assert first["new_count"] == 2 and first["existing_count"] == 0
    assert len(first["new_pending_record_ids"]) == 2
    assert first["retention_pins"]["pending_case"]["total_pinned"] == 2
    second = jobs._run_production_recall_collection(runtime, scope=BASE_SCOPE)
    assert second["new_count"] == 0 and second["existing_count"] == 2
    assert second["retention_pins"]["pending_case"]["pinned_count"] == 0
    # Collection is observation only; nothing becomes an accepted case.
    assert build_production_query_dataset(runtime, scope=BASE_SCOPE)["progress"]["accepted_case_count"] == 0
    steps: list[dict] = []
    from eimemory.scheduler.result_contract import _nightly_step
    _nightly_step(steps, "production_recall_collection", lambda: second)
    assert steps[0]["ok"] is True and steps[0]["evaluation_status"] == "completed"


def test_nightly_collection_step_can_be_disabled(runtime, monkeypatch):
    _seed(runtime, 40)
    monkeypatch.setenv(jobs.PRODUCTION_RECALL_AUTO_COLLECT_FLAG, "0")
    report = jobs._run_production_recall_collection(runtime, scope=BASE_SCOPE)
    assert report == {"ok": True, "status": "disabled",
                      "policy": {"flag": jobs.PRODUCTION_RECALL_AUTO_COLLECT_FLAG, "enabled": False}}
    assert collect_pending_production_queries(runtime, scope=BASE_SCOPE, channel=CHANNEL)["new_count"] == 1


def test_nightly_collects_before_semantic_monitor_and_auto_review():
    source = open(jobs.__file__, encoding="utf-8").read()
    assert (source.index('"production_recall_collection",')
            < source.index('"semantic_relevance_monitor",')
            < source.index('"production_recall_auto_review",'))
