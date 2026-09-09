from __future__ import annotations

import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance import promotion_watch as watch_module
from eimemory.governance.outcome_evidence import outcome_evidence
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = {"agent_id": "audit", "workspace_id": "production", "user_id": "alice"}


def seed(runtime, pattern_id):
    runtime.upsert_intent_pattern({
        "id": pattern_id, "pattern": "audit policy", "default_event_type": "repair",
        "status": "shadow", "post_promotion_watch": watch_module._initial_watch(
            candidate_id="", promotion_request_id="", pattern_id=pattern_id),
    }, scope=SCOPE)


def pattern(runtime, pattern_id):
    row = runtime.store.sqlite.conn.execute("SELECT payload_json FROM intent_patterns WHERE id=?", (pattern_id,)).fetchone()
    return json.loads(row[0])


def test_automatic_rollback_cannot_be_resurrected_by_outcome_watch(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        seed(runtime, "rollback")
        event = runtime.record_event({"user_phrase": "audit policy", "event_type": "repair", "policy_attribution": {"policy_suggestion_ids": ["rollback"]}}, scope=SCOPE)
        result = runtime.record_outcome(event["id"], {
            "outcome": "bad", "correction_from_user": "不要这样",
            "policy_attribution": {"policy_suggestion_ids": ["rollback"]},
        }, scope=SCOPE)
        assert result["rollback"]["rolled_back_pattern_ids"] == ["rollback"]
        stored = pattern(runtime, "rollback")
        assert stored["status"] == "rolled_back"
        assert stored["post_promotion_watch"]["status"] == "rolled_back"
        watch_module.record_promotion_observation(runtime, pattern_id="rollback", scope=SCOPE, hit=True, improved=True)
        assert pattern(runtime, "rollback")["status"] == "rolled_back"


def test_late_outcome_uses_original_event_not_latest_session_audit(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        seed(runtime, "original")
        seed(runtime, "newest")
        events = [runtime.record_event({
            "user_phrase": f"task A {i}", "session_id": "shared-session",
            "policy_attribution": {"policy_suggestion_ids": ["original"]},
        }, scope=SCOPE) for i in range(3)]
        runtime.store.append(RecordEnvelope.create(
            kind="recall_view", title="Newer task B", source="openclaw.before_prompt_build",
            scope=ScopeRef.from_dict(SCOPE), content={"session_id": "shared-session", "policy_suggestion_ids": ["newest"]},
            meta={"session_id": "shared-session"}))
        for event in events:
            runtime.record_outcome(event["id"], {"outcome": "good", "verifier": {"passed": True}}, scope=SCOPE)
        assert pattern(runtime, "newest")["post_promotion_watch"]["observed_count"] == 0
        assert pattern(runtime, "original")["status"] == "active"


@pytest.mark.parametrize("extra", [{"rehearsal": True, "verifier": {"passed": False}}, {"verifier": {"passed": False}}, {}])
def test_nonproduction_or_unverified_success_does_not_activate(tmp_path, extra):
    with Runtime.create(root=tmp_path) as runtime:
        seed(runtime, "protected")
        for i in range(3):
            event = runtime.record_event({"user_phrase": f"task {i}", "policy_attribution": {"policy_suggestion_ids": ["protected"]}}, scope=SCOPE)
            runtime.record_outcome(event["id"], {
                "outcome": "good", "policy_attribution": {"policy_suggestion_ids": ["protected"]}, **extra,
            }, scope=SCOPE)
        stored = pattern(runtime, "protected")
        assert stored["status"] == "shadow"
        assert stored["post_promotion_watch"]["observed_count"] == 0


def test_watch_and_ledger_failure_roll_back_together(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        seed(runtime, "atomic")
        before = pattern(runtime, "atomic")
        def fail(*args, **kwargs):
            raise RuntimeError("ledger unavailable")
        monkeypatch.setattr(watch_module, "_record_watch_ledger", fail)
        with pytest.raises(RuntimeError, match="ledger unavailable"):
            watch_module.record_promotion_observation(runtime, pattern_id="atomic", scope=SCOPE, event_id="one", hit=True, improved=True)
        assert pattern(runtime, "atomic") == before


def test_outcome_bound_to_previous_policy_version_cannot_promote_replacement(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        seed(runtime, "versioned")
        event_payload = {"id": "original-event", "user_phrase": "original task", "policy_attribution": {"policy_suggestion_ids": ["versioned"]}}
        event = runtime.record_event(event_payload, scope=SCOPE)
        revised = pattern(runtime, "versioned")
        revised["execution_policy"] = ["Different implementation"]
        runtime.upsert_intent_pattern(revised, scope=SCOPE)
        # A transport retry must not rebind an existing event to a new version.
        runtime.record_event(event_payload, scope=SCOPE)
        runtime.record_outcome(event["id"], {"outcome": "good", "verifier": {"passed": True}}, scope=SCOPE)
        assert pattern(runtime, "versioned")["post_promotion_watch"]["observed_count"] == 0


def test_personal_watch_cannot_mutate_shared_policy(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        payload = {"id": "shared", "pattern": "shared", "status": "shadow"}
        runtime.upsert_intent_pattern(payload, scope={**SCOPE, "user_id": ""})
        result = watch_module.record_promotion_observation(runtime, pattern_id="shared", scope=SCOPE, hit=True)
        assert result["status"] == "not_found"
        assert "post_promotion_watch" not in pattern(runtime, "shared")


def test_rehearsal_correction_cannot_roll_back_real_policy(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        seed(runtime, "real")
        event = runtime.record_event({"user_phrase": "rehearsal", "policy_attribution": {"policy_suggestion_ids": ["real"]}}, scope=SCOPE)
        runtime.record_outcome(event["id"], {
            "outcome": "bad", "correction_from_user": "不要这样", "rehearsal": True,
            "policy_attribution": {"policy_suggestion_ids": ["real"]},
        }, scope=SCOPE)
        assert pattern(runtime, "real")["status"] == "shadow"


def test_conflicting_outcome_cannot_roll_back_another_events_policy(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        seed(runtime, "A")
        seed(runtime, "B")
        event = runtime.record_event({"user_phrase": "task A", "policy_attribution": {"policy_suggestion_ids": ["A"]}}, scope=SCOPE)
        runtime.record_outcome(event["id"], {
            "outcome": "bad", "correction_from_user": "不要这样",
            "policy_attribution": {"policy_suggestion_ids": ["B"]},
        }, scope=SCOPE)
        assert pattern(runtime, "B")["status"] == "shadow"


def test_compact_audit_keeps_original_task_binding(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        binding = {"session_id": "session", "task_anchor": "run:original", "audit_record_id": "audit-original", "policy_version_ids": {"policy": "a" * 64}}
        content = runtime.store.sqlite._compact_recall_view_content(binding)
        meta = runtime.store.sqlite._compact_recall_view_meta(binding)
        for compact in (content, meta):
            assert compact["task_anchor"] == binding["task_anchor"]
            assert compact["audit_record_id"] == binding["audit_record_id"]
            assert compact["policy_version_ids"] == binding["policy_version_ids"]


@pytest.mark.parametrize("terminal", ["rolled_back", "active"])
def test_stale_watch_write_cannot_overwrite_committed_terminal_state(tmp_path, terminal):
    with Runtime.create(root=tmp_path) as runtime:
        seed(runtime, "stale")
        stale = pattern(runtime, "stale")
        if terminal == "rolled_back":
            runtime.rollback_intent_pattern("stale", scope=SCOPE)
        else:
            for i in range(3):
                watch_module.record_promotion_observation(runtime, pattern_id="stale", scope=SCOPE, event_id=str(i), hit=True, improved=True)
        with pytest.raises(RuntimeError, match="policy_state_conflict"):
            watch_module._write_pattern(runtime, stale, scope=SCOPE)
        assert pattern(runtime, "stale")["status"] == terminal


@pytest.mark.parametrize("outcome", ["good", "bad"])
def test_unbound_event_cannot_acquire_policy_attribution_from_outcome(tmp_path, outcome):
    with Runtime.create(root=tmp_path) as runtime:
        seed(runtime, "unbound")
        event = runtime.record_event({"user_phrase": "unbound task"}, scope=SCOPE)
        runtime.record_outcome(event["id"], {"outcome": outcome, "verifier": {"passed": True},
            "correction_from_user": "不要这样" if outcome == "bad" else "",
            "policy_attribution": {"policy_suggestion_ids": ["unbound"]}}, scope=SCOPE)
        stored = pattern(runtime, "unbound")
        assert stored["status"] == "shadow"
        assert stored["post_promotion_watch"]["observed_count"] == 0


def test_historical_replay_failures_do_not_supply_real_rollback_threshold(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        seed(runtime, "history")
        for i in range(3):
            event = runtime.record_event({"user_phrase": f"independent task {i}",
                "policy_attribution": {"policy_suggestion_ids": ["history"]}}, scope=SCOPE)
            runtime.record_outcome(event["id"], {"outcome": "bad", "verifier": {"passed": True},
                "rehearsal": i < 2,
                "policy_attribution": {"policy_suggestion_ids": ["history"]}}, scope=SCOPE)
        assert pattern(runtime, "history")["status"] == "shadow"
        assert pattern(runtime, "history")["post_promotion_watch"]["observed_count"] == 1


def test_bare_bad_outcome_is_not_verified_but_user_correction_is_safety_evidence():
    assert not outcome_evidence({"outcome": "bad"})["production_eligible"]
    assert outcome_evidence({"outcome": "bad", "correction_from_user": "wrong result"})["production_eligible"]


def test_policy_version_is_rechecked_inside_watch_write_transaction(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        seed(runtime, "racing")
        event = runtime.record_event({"user_phrase": "old task", "policy_attribution": {"policy_suggestion_ids": ["racing"]}}, scope=SCOPE)
        original = watch_module.record_promotion_observation
        def replace_before_lock(*args, **kwargs):
            replacement = pattern(runtime, "racing")
            replacement["execution_policy"] = ["replacement behavior"]
            runtime.upsert_intent_pattern(replacement, scope=SCOPE)
            return original(*args, **kwargs)
        monkeypatch.setattr(watch_module, "record_promotion_observation", replace_before_lock)
        runtime.record_outcome(event["id"], {"outcome": "good", "verifier": {"passed": True}}, scope=SCOPE)
        assert pattern(runtime, "racing")["post_promotion_watch"]["observed_count"] == 0


def test_rollback_binding_check_holds_sqlite_write_transaction(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        seed(runtime, "locked")
        event = runtime.record_event({"user_phrase": "task", "policy_attribution": {"policy_suggestion_ids": ["locked"]}}, scope=SCOPE)
        sqlite = runtime.store.sqlite
        original = sqlite.resolve_outcome_policy_attribution
        def check_lock(*args, **kwargs):
            assert sqlite.conn.in_transaction
            return original(*args, **kwargs)
        monkeypatch.setattr(sqlite, "resolve_outcome_policy_attribution", check_lock)
        runtime.store.record_outcome(event["id"], {"outcome": "bad", "correction_from_user": "不要这样"}, scope=SCOPE)
