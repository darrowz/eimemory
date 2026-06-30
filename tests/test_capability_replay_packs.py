from __future__ import annotations

from eimemory.api.runtime import Runtime


SCOPE = {"agent_id": "agent-replay-packs", "workspace_id": "capability-replay"}
CORE_CAPABILITIES = {
    "memory.recall",
    "tool.routing",
    "knowledge.intake",
    "proactive.judgment",
    "safety.boundary",
}
WEAK_CAPABILITIES = {
    "search.discovery",
    "research.synthesis",
    "operations.uumit",
    "device.control",
}


def test_capability_replay_packs_activate_non_code_capabilities(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.build_capability_replay_packs(scope=SCOPE, persist=True)

        assert report["ok"] is True
        assert set(report["capabilities"]) >= CORE_CAPABILITIES
        assert report["pack_count"] >= len(CORE_CAPABILITIES)
        assert report["case_count"] >= len(CORE_CAPABILITIES) * 3
        assert report["persisted_replay_count"] == report["case_count"]

        for pack in report["packs"]:
            assert len(pack["cases"]) >= 3
            assert pack["pass_rate"] == 1.0
            assert pack["rollback_plan"]["command"]
            assert pack["observe_plan"]["min_observations"] >= 1

        ledger = runtime.learning_ledger(scope=SCOPE, attribute_outcomes=False)
        for capability in CORE_CAPABILITIES:
            item = ledger["capabilities"][capability]
            assert item["status"] == "active"
            assert item["score"] >= 0.75
            assert item["evidence_count"] >= 3
    finally:
        runtime.close()


def test_capability_replay_packs_are_queryable_as_replay_results(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            capabilities=["memory.recall"],
        )

        records = runtime.store.list_records(kinds=["replay_result"], scope=SCOPE, limit=20)
        assert len(records) == report["case_count"]
        assert {record.meta["report_type"] for record in records} == {"capability_replay_pack"}
        assert {record.meta["capability"] for record in records} == {"memory.recall"}
        assert all(record.meta["verdict"] == "pass" for record in records)
    finally:
        runtime.close()


def test_capability_replay_packs_include_named_weak_capability_cases(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            capabilities=sorted(WEAK_CAPABILITIES),
        )

        assert set(report["capabilities"]) == WEAK_CAPABILITIES
        for pack in report["packs"]:
            capability = pack["capability"]
            case_ids = {case["case_id"] for case in pack["cases"]}
            assert not any(case_id.startswith("generic_") for case_id in case_ids)
            assert len(case_ids) >= 3
            assert all(case["target_capability"] == capability for case in pack["cases"])

        ledger = runtime.learning_ledger(scope=SCOPE, attribute_outcomes=False)
        for capability in WEAK_CAPABILITIES:
            item = ledger["capabilities"][capability]
            assert item["status"] == "active"
            assert item["score"] >= 0.75
            assert item["evidence_count"] >= 3
    finally:
        runtime.close()
