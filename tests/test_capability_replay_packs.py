from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.experience import record_outcome_trace
from eimemory.experience.capability_contract import SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION
from eimemory.governance.capability_acceptance import CAPABILITY_ACCEPTANCE_CASES, run_capability_acceptance
from eimemory.governance.capability_replay_executor import execute_capability_replay_case
from eimemory.models.records import RecordEnvelope, ScopeRef


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


def test_runtime_executor_replays_twelve_contract_backed_acceptance_traces(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        acceptance = run_capability_acceptance(runtime, scope=SCOPE, persist=True)

        report = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            capabilities=sorted(WEAK_CAPABILITIES),
        )
        persisted = [runtime.store.get_by_id(record_id, scope=SCOPE) for record_id in report["persisted_replay_ids"]]
    finally:
        runtime.close()

    assert acceptance["all_passed"] is True
    results = [item for pack in report["packs"] for item in pack["case_results"]]
    assert len(results) == 12
    assert {item["verdict"] for item in results} == {"pass"}
    assert len({item["trace_id"] for item in results}) == 12
    assert len({item["probe_source_id"] for item in results}) == 12
    assert {item["contract_schema"] for item in results} == {CONTRACT_SCHEMA_VERSION}
    assert all(isinstance(item["observation"], dict) and item["observation"] for item in results)
    assert all(record is not None and record.content["result"]["observation"] for record in persisted)


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
    assert {item["verdict"] for item in report["packs"][0]["case_results"]} <= {"not_run", "fail"}


def test_runtime_executor_rejects_text_only_case_evidence(tmp_path) -> None:
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

    assert report["packs"][0]["pass_rate"] == 0.0
    assert {item["verdict"] for item in report["packs"][0]["case_results"]} <= {"not_run", "fail"}


def test_runtime_executor_rejects_wrong_case_missing_source_failed_verifier_and_reused_source(tmp_path) -> None:
    scenarios = (
        ("wrong-case", "search_recent_source", "search_trending_github", True, False, False),
        ("missing-source", "search_recent_source", "search_recent_source", True, True, False),
        ("failed-verifier", "search_recent_source", "search_recent_source", False, False, False),
        ("reused-source", "search_recent_source", "search_recent_source", True, False, True),
    )
    for name, contract_case, replay_case, verifier_passed, cross_scope, reuse_source in scenarios:
        root = tmp_path / name
        runtime = Runtime.create(root=root)
        try:
            _seed_contract_trace(
                runtime,
                scope=SCOPE,
                case_id=contract_case,
                verifier_passed=verifier_passed,
                probe_scope={**SCOPE, "workspace_id": "other-scope"} if cross_scope else SCOPE,
                trace_text="Recent recency window source trust GitHub trending created range stars verified",
                duplicate_trace=reuse_source,
            )
            case = {
                "case_id": replay_case,
                "target_capability": "search.discovery",
                "scope": SCOPE,
            }
            result = execute_capability_replay_case(runtime, case)
        finally:
            runtime.close()

        assert result["verdict"] in {"not_run", "fail"}, name


def _seed_contract_trace(
    runtime: Runtime,
    *,
    scope: dict,
    case_id: str,
    verifier_passed: bool,
    probe_scope: dict,
    trace_text: str,
    duplicate_trace: bool,
) -> None:
    artifact = next(item for item in CAPABILITY_ACCEPTANCE_CASES if item["case_id"] == case_id)
    probe_id = f"probe-{case_id}"
    probe = RecordEnvelope.create(
        kind="replay_result",
        title=f"Capability acceptance probe: {case_id}",
        summary="pass: canonical non-destructive artifact validation",
        scope=ScopeRef.from_dict(probe_scope),
        source="eimemory.capability_acceptance",
        content={
            "report_type": "capability_probe_result",
            "schema_version": "capability_probe_result.v1",
            "case_id": case_id,
            "capability": artifact["capability"],
            "observation": dict(artifact["observation"]),
            "passed": True,
            "verdict": "pass",
            "validator": {"schema_version": CONTRACT_SCHEMA_VERSION, "passed": True, "error": ""},
        },
        meta={
            "report_type": "capability_probe_result",
            "schema_version": "capability_probe_result.v1",
            "case_id": case_id,
            "capability": artifact["capability"],
            "passed": True,
            "verdict": "pass",
        },
    )
    probe.record_id = probe_id
    runtime.store.append(probe)
    contract = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "capability": artifact["capability"],
        "case_id": case_id,
        "observations": dict(artifact["observation"]),
        "checks": [{"name": "canonical_observation_contract", "passed": True, "evidence_ref": probe_id}],
        "source_record_ids": [probe_id],
        "probe": True,
    }
    for index in range(2 if duplicate_trace else 1):
        trace_id = f"trace-{case_id}-{index}"
        trace = RecordEnvelope.create(
            kind="reflection",
            title=f"Outcome trace: {trace_id}",
            summary=trace_text,
            scope=ScopeRef.from_dict(scope),
            source="eimemory.experience.outcome_trace",
            content={
                "schema_version": "outcome_trace.v1",
                "payload": {
                    "trace_id": trace_id,
                    "task_type": "capability.acceptance",
                    "input_summary": trace_text,
                    "outcome": {"status": "success", "success": True, "rehearsal": True},
                    "verifier": {"passed": verifier_passed, "evidence_ref": probe_id},
                    "capability": artifact["capability"],
                    "capability_case_id": case_id,
                    "capability_contract": contract,
                },
            },
            meta={
                "report_type": "outcome_trace",
                "schema_version": "outcome_trace.v1",
                "trace_id": trace_id,
                "outcome_status": "success",
                "primary_label": "success",
                "capability": artifact["capability"],
                "capability_case_id": case_id,
                "contract_verified": True,
            },
        )
        runtime.store.append(trace)


def test_replay_rerun_persists_new_execution_and_readiness_uses_latest(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.run_capability_replay_case = lambda case: {  # type: ignore[attr-defined]
        "verdict": "pass",
        "hit": True,
        "observed": f"verified:{case['case_id']}",
        "evidence_source_id": f"first:{case['case_id']}",
    }
    try:
        first = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            capabilities=["search.discovery"],
            loop_id="stable-replay-loop",
        )
        runtime.run_capability_replay_case = lambda case: {  # type: ignore[attr-defined]
            "verdict": "fail",
            "hit": False,
            "observed": f"failed:{case['case_id']}",
            "reason": "regression_detected",
        }
        second = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            capabilities=["search.discovery"],
            loop_id="stable-replay-loop",
        )
        readiness = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert first["execution_id"] != second["execution_id"]
    assert set(first["persisted_replay_ids"]).isdisjoint(second["persisted_replay_ids"])
    assert readiness["verified_replay"]["executed_count"] == 3
    assert readiness["verified_replay"]["pass_count"] == 0
    assert readiness["verified_replay"]["fail_count"] == 3


def _install_successful_executor(runtime: Runtime) -> None:
    def executor(case):
        return {
            "observed": str(case.get("expected") or ""),
            "hit": True,
            "evidence_source_id": f"test:{case.get('case_id')}",
        }

    runtime.run_capability_replay_case = executor  # type: ignore[attr-defined]
