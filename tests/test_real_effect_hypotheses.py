from datetime import datetime, timezone
import pytest

from eimemory.api.runtime import Runtime
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.models.records import RecordEnvelope, ScopeRef, RecallBundle
from eimemory.retrieval.proactive import ProactiveRecallService
from eimemory.retrieval.effect_signals import record_signal
from eimemory.governance.learning.effect_hypotheses import produce_effect_hypotheses
from eimemory.knowledge.evidence_contracts import versioned_record_ref

BASE = {"tenant_id": "default", "agent_id": "effects", "workspace_id": "workspace", "user_id": "user"}
SCOPE = resolve_channel_scope("hermes", BASE)
RELEASE = {"release_commit": "a" * 40, "release_version": "test", "deployment_receipt_id": "r", "release_session_id": "s"}


@pytest.fixture
def runtime(tmp_path):
    value = Runtime.create(root=tmp_path)
    value.proactive = ProactiveRecallService(value, control_percent=0, release_identity=RELEASE)
    yield value
    value.close()


def memory(runtime, **kwargs):
    return runtime.store.append(RecordEnvelope.create(kind="memory", title="Deployment preference",
        summary="Prefer the Borealis deployment command for my workspace.", scope=ScopeRef.from_dict(SCOPE),
        content={"text": "Prefer the Borealis deployment command for my workspace."}, **kwargs))


def observe(runtime, record, index, *, correction="suspected", rating=None, task="unknown", injected=True):
    session = f"session-{index}"
    decision = runtime.proactive.decide(channel="hermes", scope=BASE, source_ids=["default"],
        session_id=session, query_id="turn", query="remember my previous deployment preference",
        recall_bundle=RecallBundle(items=[record], rules=[], reflections=[], confidence=.99, next_action_hint=""))
    assert decision["items"]
    params = {"channel": "hermes", "scope": SCOPE, "source_ids": ["default"], "session_id": session,
              "turn_id": "turn", "decision_id": decision["decision_id"]}
    if injected:
        runtime.proactive.mark_injected(**params, injected_citations=[i["citation"] for i in decision["items"]], release_identity=RELEASE)
    record_signal(runtime.store, **params, phase="turn_completed", event_id="completed",
                  labels={"tool_chain": "succeeded", "task_success": task})
    record_signal(runtime.store, **params, phase="next_user", event_id="next",
                  labels={"correction": correction, "reask": "none"})
    if rating:
        record_signal(runtime.store, **params, phase="explicit_rating", event_id="vote", labels={"rating": rating})
    return decision


def test_actual_delivery_produces_versioned_hypothesis_and_not_l5(runtime):
    record = memory(runtime)
    for i in range(3):
        observe(runtime, record, i)
    report = produce_effect_hypotheses(runtime, scope=BASE, min_decisions=3, persist=True)
    hypothesis = next(h for h in report["hypotheses"] if h["metric"] == "correction_rate")
    assert hypothesis["target_ref"] == versioned_record_ref(record)
    assert hypothesis["trial_status"] == "eligible_for_bounded_trial"
    assert hypothesis["observed_decisions"] == 3 and len(hypothesis["signal_ids"]) == 6
    assert not hypothesis["causality_verified"] and not hypothesis["certifies_l5"]
    stored = runtime.store.get_by_id(hypothesis["hypothesis_id"], scope=SCOPE)
    assert stored.kind == "reflection" and stored.status == "archived"
    assert produce_effect_hypotheses(runtime, scope=BASE, min_decisions=3, persist=True)["hypotheses"] == report["hypotheses"]


def test_offered_only_or_stale_memory_never_authorizes_change(runtime):
    record = memory(runtime)
    observe(runtime, record, 0, injected=False)
    report = produce_effect_hypotheses(runtime, scope=BASE)
    assert not report["hypotheses"] and report["unattributed_failures"]
    observe(runtime, record, 1)
    record.summary = "User updated this preference."
    runtime.store.append(record)
    report = produce_effect_hypotheses(runtime, scope=BASE)
    assert record.record_id in report["stale_targets"] and not report["hypotheses"]


def test_mandatory_memory_is_protected(runtime):
    record = memory(runtime, meta={"hard_policy": True})
    observe(runtime, record, 0)
    report = produce_effect_hypotheses(runtime, scope=BASE)
    assert report["protected_targets"] == [record.record_id]
    assert not report["hypotheses"]
