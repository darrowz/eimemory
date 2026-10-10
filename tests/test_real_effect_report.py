from datetime import datetime, timezone
import pytest

from eimemory.api.runtime import Runtime
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.governance.learning.effect_report import build_effect_report, metrics
from eimemory.governance.learning.effect_dataset import day_window
from eimemory.retrieval.proactive import ProactiveRecallService
from eimemory.retrieval.effect_signals import record_signal

BASE = {"tenant_id": "default", "agent_id": "report", "workspace_id": "workspace", "user_id": "user"}
RELEASE = {"release_commit": "a" * 40, "release_version": "test", "deployment_receipt_id": "r", "release_session_id": "s"}


@pytest.fixture
def runtime(tmp_path):
    value = Runtime.create(root=tmp_path)
    value.proactive = ProactiveRecallService(value, control_percent=0, release_identity=RELEASE)
    yield value
    value.close()


def sample(runtime, index, *, success="unknown", control=False, date="2026-10-09T12:00:00+00:00"):
    result = runtime.proactive.decide(channel="hermes", scope=BASE, source_ids=["default"],
        session_id="report-" + str(index), query_id="turn", query="same query")
    decision = runtime.store.load_proactive_decision(result["decision_id"])
    with runtime.store._lock:
        runtime.store.sqlite.conn.execute("UPDATE proactive_decisions SET created_at=?,control_cohort=? WHERE decision_id=?", (date, int(control), result["decision_id"]))
        runtime.store.sqlite.conn.commit()
    params = {"channel": "hermes", "scope": resolve_channel_scope("hermes", BASE), "source_ids": ["default"],
              "session_id": decision["session_id"], "turn_id": "turn", "decision_id": decision["decision_id"]}
    record_signal(runtime.store, **params, phase="turn_completed", event_id="done",
                  labels={"tool_chain": "succeeded", "task_success": success, "latency_ms": 10 * (index + 1)})
    return params


def test_report_unknowns_denominators_ab_and_archive(runtime):
    one = sample(runtime, 0, success="failed", control=True)
    two = sample(runtime, 1, success="succeeded")
    sample(runtime, 2)
    record_signal(runtime.store, **one, phase="next_user", event_id="next", labels={"correction": "suspected", "reask": "none"})
    first = build_effect_report(runtime, scope=BASE, report_date="2026-10-09", persist=True)
    rate = first["metrics"]["task_success_rate"]
    assert rate == {"numerator": 1, "denominator": 2, "value": .5, "unknown": 1, "coverage": 2 / 3}
    assert first["metrics"]["correction_rate"]["denominator"] == 1
    assert first["metrics"]["latency_ms"] == {"samples": 3, "p50": 20, "p95": 30}
    assert first["ab_strata"][0]["differences"]["task_success_rate"] == 1
    assert first["certifies_l5"] is False and first["certifies_improvement"] is False
    record = runtime.store.get_by_id(first["record_id"], scope=resolve_channel_scope("hermes", BASE))
    assert record.status == "archived"
    assert build_effect_report(runtime, scope=BASE, report_date="2026-10-09", persist=True)["record_id"] == first["record_id"]
    other = build_effect_report(runtime, scope={**BASE, "user_id": "other"}, report_date="2026-10-09")
    assert other["metrics"]["decisions"] == 0 and other["metrics"]["task_success_rate"]["value"] is None


def test_decision_date_survives_pruning_and_delayed_feedback(runtime):
    one = sample(runtime, 0)
    with runtime.store._lock:
        runtime.store.sqlite.conn.execute("DELETE FROM proactive_decisions WHERE decision_id=?", (one["decision_id"],))
        runtime.store.sqlite.conn.commit()
    report = build_effect_report(runtime, scope=BASE, report_date="2026-10-09")
    assert report["metrics"]["decisions"] == 1
    assert build_effect_report(runtime, scope=BASE, report_date="2026-10-10")["metrics"]["decisions"] == 0
    replay = record_signal(runtime.store, **one, phase="turn_completed", event_id="done",
                          labels={"tool_chain": "succeeded", "task_success": "unknown", "latency_ms": 10})
    assert replay["replayed"]


def test_conflicting_votes_are_unknown_not_double_counted(runtime):
    one = sample(runtime, 0)
    for event, rating in (("vote1", "positive"), ("vote2", "negative")):
        record_signal(runtime.store, **one, phase="explicit_rating", event_id=event, labels={"rating": rating})
    rate = build_effect_report(runtime, scope=BASE, report_date="2026-10-09")["metrics"]["negative_rating_rate"]
    assert rate["denominator"] == 0 and rate["unknown"] == 1


def test_shanghai_day_window():
    day, start, end = day_window("2026-10-09")
    assert start == datetime(2026, 10, 8, 16, tzinfo=timezone.utc)
    assert (end - start).total_seconds() == 86400
