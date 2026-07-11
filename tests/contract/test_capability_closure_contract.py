from __future__ import annotations

from eimemory.api.runtime import Runtime


SCOPE = {"agent_id": "closure-contract", "workspace_id": "capability"}


def test_capability_replay_without_executor_is_pending_not_passed(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            capabilities=["memory.recall"],
        )

        assert report["ok"] is True
        pack = report["packs"][0]
        assert pack["capability"] == "memory.recall"
        assert pack["pass_rate"] == 0.0
        assert pack["score"] == 0.0
        assert {result["verdict"] for result in pack["case_results"]} == {"not_run"}
        assert all(result["hit"] is None for result in pack["case_results"])

        records = runtime.store.list_records(kinds=["replay_result"], scope=SCOPE, limit=20)
        assert records
        assert {record.meta["verdict"] for record in records} == {"not_run"}

        ledger = runtime.learning_ledger(scope=SCOPE, attribute_outcomes=False)
        item = ledger["capabilities"]["memory.recall"]
        assert item["score"] == 0.0
        assert item["status"] == "needs_outcome_recalculation"
    finally:
        runtime.close()


def test_capability_replay_uses_real_executor_before_recording_pass(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    def executor(case):
        return {
            "observed": f"met: {case['expected']}",
            "hit": True,
        }

    runtime.run_capability_replay_case = executor  # type: ignore[attr-defined]
    try:
        report = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            capabilities=["memory.recall"],
        )

        pack = report["packs"][0]
        assert pack["pass_rate"] == 1.0
        assert pack["score"] > 0.0
        assert {result["verdict"] for result in pack["case_results"]} == {"pass"}

        ledger = runtime.learning_ledger(scope=SCOPE, attribute_outcomes=False)
        item = ledger["capabilities"]["memory.recall"]
        assert item["status"] == "active"
        assert item["evidence_count"] >= 3
    finally:
        runtime.close()


def test_user_correction_creates_replay_case_without_claiming_behavior_pass(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.record_user_correction_replay(
            {
                "text": "Answer deployment status only after checking live evidence.",
                "context": "assistant answered from stale memory",
                "target_capability": "evidence.query_first",
                "expected_behavior": "Query git/runtime/deploy evidence before answering status questions.",
            },
            scope=SCOPE,
            persist=True,
        )

        assert report["ok"] is True
        assert report["replay"]["verdict"] == "not_run"
        assert report["replay"]["pass_rate"] == 0.0
        assert report["replay"]["verification_status"] == "pending_post_answer"

        replay = runtime.store.get_by_id(report["replay_record_id"], scope=SCOPE)
        assert replay is not None
        assert replay.meta["verdict"] == "not_run"
        assert replay.meta["pass_rate"] == 0.0

        edges = runtime.store.list_memory_edges(scope=SCOPE, record_ids=[report["lesson_record_id"]], limit=20)
        relations = {edge.meta.get("relation") for edge in edges}
        assert "COVERED_BY_REPLAY_CASE" in relations
        assert "VALIDATED_BY_REPLAY" not in relations
    finally:
        runtime.close()


def test_pre_answer_gate_matches_rules_without_claiming_answer_compliance(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        replay = runtime.record_user_correction_replay(
            {
                "text": "Answer deployment status only after checking live evidence.",
                "context": "assistant answered from stale memory",
                "target_capability": "evidence.query_first",
                "expected_behavior": "Query git/runtime/deploy evidence before answering status questions.",
            },
            scope=SCOPE,
            persist=True,
        )

        gate = runtime.build_ground_truth_pre_answer_gate(
            query="What version is deployed?",
            scope=SCOPE,
            persist=True,
        )

        assert gate["ok"] is True
        assert gate["gate_required"] is True
        assert gate["verdict"] == "matched"
        assert gate["verification_status"] == "pending_answer_check"
        assert gate["rules"][0]["rule_id"] == replay["ground_truth_rule_id"]
        assert gate["replay_gate"]["verification_status"] == "pending_answer_check"
    finally:
        runtime.close()
