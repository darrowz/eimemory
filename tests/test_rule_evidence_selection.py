"""Rule promotion consumes conservative event ordering through real storage."""
from dataclasses import asdict, replace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.learning.rule_evidence_selection import latest_rule_evidence
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef


SCOPE = ScopeRef(tenant_id="selection", agent_id="agent", workspace_id="repo", user_id="owner")
TIED = "2026-01-01T00:00:00Z"
LATER = "2026-01-01T00:00:01Z"


def evidence(kind, rule_id, positive, *, record_id, timestamp=TIED, scope=SCOPE, diagnostic=False):
    meta = ({"target_rule_id": rule_id, "verdict": "pass" if positive else "fail",
             "pass_rate": 1.0 if positive else 0.0, "sample_size": 1}
            if kind == "replay" else {"target_ref": {"kind": "rule", "record_id": rule_id},
                                      "decision": "accept" if positive else "reject", "reviewed_by": "operator"})
    record = RecordEnvelope.create(kind="replay_result" if kind == "replay" else "feedback",
        title="Rule outcome evidence", scope=scope, meta=meta,
        source="evolution.replay" if diagnostic else "evolution.behavioral_replay" if kind == "replay" else "evolution.feedback")
    record.record_id = record_id
    record.time = TimeRef(timestamp, timestamp, timestamp)
    return record


@pytest.mark.parametrize("pass_sorts_first", [False, True])
@pytest.mark.parametrize("kind", ["replay", "feedback"])
def test_nightly_does_not_promote_tied_failure_then_consumes_new_success(tmp_path, pass_sorts_first, kind):
    runtime = Runtime.create(root=tmp_path)
    try:
        scope = asdict(SCOPE)
        rule = runtime.evolution.store_rule(title="Conservative ordering", summary="Use current verified evidence",
            task_type="chat.reply", retrieval_policy={"route_hint": "task_context_first"}, scope=scope, status="accepted")
        other_kind = "feedback" if kind == "replay" else "replay"
        runtime.store.append(evidence(other_kind, rule.record_id, True, record_id="rec_111111111111"))
        first_id, second_id = "rec_ffffffffffff", "rec_aaaaaaaaaaaa"
        passing = evidence(kind, rule.record_id, True, record_id=first_id if pass_sorts_first else second_id)
        failing = evidence(kind, rule.record_id, False, record_id=second_id if pass_sorts_first else first_id)
        runtime.store.append(passing)
        runtime.store.append(failing)
        stored = runtime.store.list_records(kinds=[passing.kind], scope=SCOPE, limit=10)
        assert stored[0].record_id == first_id
        assert (stored[0].record_id == passing.record_id) is pass_sorts_first

        denied = runtime.run_rule_evolution(scope=scope, apply=True, min_roi=0.0)
        assert denied["roi_summary"]["roi_signal"] >= 0.0
        assert rule.record_id not in denied["record_ids"]["promoted_rules"]
        assert runtime.store.get_by_id(rule.record_id, scope=SCOPE).status == "accepted"
        assert runtime.evolution.promotion_candidates(scope=scope)["candidate_count"] == 0

        # An actual newer positive event supersedes the ambiguous instant.
        # Newer out-of-scope evidence and baseline diagnostics cannot revoke it.
        runtime.store.append(evidence(kind, rule.record_id, True, record_id="rec_222222222222", timestamp=LATER))
        runtime.store.append(evidence(kind, rule.record_id, False, record_id="rec_333333333333",
            timestamp="2026-01-01T00:00:02Z", scope=replace(SCOPE, user_id="another-user")))
        runtime.store.append(evidence("replay", rule.record_id, False, record_id="rec_444444444444",
            timestamp="2026-01-01T00:00:02Z", diagnostic=True))
        assert runtime.evolution.promotion_candidates(scope=scope)["candidate_count"] == 1
        recovered = runtime.run_rule_evolution(scope=scope, apply=True, min_roi=0.0)
        assert recovered["record_ids"]["promoted_rules"] == [rule.record_id]
        assert runtime.store.get_by_id(rule.record_id, scope=SCOPE).status == "active"
    finally:
        runtime.close()


def test_selector_uses_event_time_not_input_order_and_preserves_equal_time_failures():
    old = evidence("replay", "rule-id", False, record_id="rec_ffffffffffff")
    new = evidence("replay", "rule-id", True, record_id="rec_aaaaaaaaaaaa", timestamp=LATER)
    assert latest_rule_evidence([old, new], positive_field="verdict", positive_value="pass") is new
    old.time.updated_at = "2026-01-01T00:00:01.000000+00:00"
    assert latest_rule_evidence([new, old], positive_field="verdict", positive_value="pass") is old
    old.meta.update(verdict="pass", pass_rate=0.5)
    assert latest_rule_evidence([new, old], positive_field="verdict", positive_value="pass") is old


def test_selector_does_not_authorize_an_unorderable_event():
    passed = evidence("replay", "rule-id", True, record_id="rec_aaaaaaaaaaaa")
    passed.time.updated_at = "not-a-time"
    assert latest_rule_evidence([passed], positive_field="verdict", positive_value="pass") is None
