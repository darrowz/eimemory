from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.experience import record_outcome_trace


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
    _install_successful_executor(runtime)
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
        assert {record.meta["verdict"] for record in records} == {"not_run"}
    finally:
        runtime.close()


def test_capability_replay_packs_include_named_weak_capability_cases(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    _install_successful_executor(runtime)
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


def test_capability_replay_rejects_inconsistent_pass_claims(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.run_capability_replay_case = lambda _case: {"verdict": "pass", "hit": False, "observed": ""}  # type: ignore[attr-defined]
    try:
        report = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            capabilities=["search.discovery"],
        )
    finally:
        runtime.close()

    assert report["packs"][0]["pass_rate"] == 0.0
    assert {item["verdict"] for item in report["packs"][0]["case_results"]} == {"fail"}
    assert {item["reason"] for item in report["packs"][0]["case_results"]} == {"inconsistent_pass_evidence"}


def test_capability_replay_rejects_pass_without_source_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.run_capability_replay_case = lambda case: {  # type: ignore[attr-defined]
        "verdict": "pass",
        "hit": True,
        "observed": f"verified:{case['case_id']}",
    }
    try:
        report = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            capabilities=["search.discovery"],
        )
    finally:
        runtime.close()

    assert report["packs"][0]["pass_rate"] == 0.0
    assert {item["reason"] for item in report["packs"][0]["case_results"]} == {"missing_replay_evidence_source"}


def test_runtime_executor_replays_verified_outcome_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        summaries = [
            "Recent recency window and source quality verified",
            "GitHub trending projects use created range and stars sort",
            "Official docs and primary source verification completed",
        ]
        for index, summary in enumerate(summaries):
            record_outcome_trace(
                runtime,
                {
                    "trace_id": f"search-verified-{index}",
                    "idempotency_key": f"idem-search-verified-{index}",
                    "task_type": "search_discovery",
                    "input_summary": summary,
                    "outcome": {"status": "success"},
                    "verifier": {"passed": True},
                    "feedback": {"summary": "primary source verified"},
                },
                scope=SCOPE,
            )

        report = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            capabilities=["search.discovery"],
        )
    finally:
        runtime.close()

    assert report["packs"][0]["pass_rate"] == 1.0
    assert all(item["hit"] is True for item in report["packs"][0]["case_results"])
    assert all("source_id=" in item["observed"] for item in report["packs"][0]["case_results"])


def test_runtime_executor_rejects_generic_outcomes_for_specific_cases(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        for index in range(3):
            record_outcome_trace(
                runtime,
                {
                    "trace_id": f"search-generic-{index}",
                    "idempotency_key": f"idem-search-generic-{index}",
                    "task_type": "search_discovery",
                    "input_summary": f"Lookup task completed {index}",
                    "outcome": {"status": "success"},
                    "verifier": {"passed": True},
                    "feedback": {"summary": "completed"},
                },
                scope=SCOPE,
            )

        report = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            capabilities=["search.discovery"],
        )
    finally:
        runtime.close()

    assert report["packs"][0]["pass_rate"] == 0.0
    assert {item["reason"] for item in report["packs"][0]["case_results"]} == {"case_specific_outcome_evidence_missing"}


def test_runtime_executor_accepts_verified_chinese_case_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    summaries = [
        "核验近期时间窗口与来源质量可信度",
        "GitHub 热门趋势按创建时间与星标数量排序",
        "完成官方一手来源核验并标记已验证",
    ]
    try:
        for index, summary in enumerate(summaries):
            record_outcome_trace(
                runtime,
                {
                    "trace_id": f"search-cn-{index}",
                    "idempotency_key": f"idem-search-cn-{index}",
                    "task_type": "search_discovery",
                    "input_summary": summary,
                    "outcome": {"status": "success"},
                    "verifier": {"passed": True},
                    "feedback": {"summary": "已核验"},
                },
                scope=SCOPE,
            )

        report = runtime.build_capability_replay_packs(scope=SCOPE, persist=True, capabilities=["search.discovery"])
    finally:
        runtime.close()

    assert report["packs"][0]["pass_rate"] == 1.0


def _install_successful_executor(runtime: Runtime) -> None:
    def executor(case):
        return {
            "observed": str(case.get("expected") or ""),
            "hit": True,
            "evidence_source_id": f"test:{case.get('case_id')}",
        }

    runtime.run_capability_replay_case = executor  # type: ignore[attr-defined]
