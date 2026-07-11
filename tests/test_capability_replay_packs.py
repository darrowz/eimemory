from __future__ import annotations

from datetime import datetime, timedelta, timezone

from eimemory.api.runtime import Runtime
from eimemory.experience import record_outcome_trace
from eimemory.experience.capability_contract import SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION
from eimemory.governance import capability_acceptance
from eimemory.governance.capability_acceptance import (
    PROBE_SCHEMA_VERSION,
    capability_acceptance_digest,
    run_capability_acceptance,
)
from eimemory.governance.capability_replay_executor import execute_capability_replay_case
from eimemory.governance.capability_probe_executor import execute_probe
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


def test_capability_replay_packs_do_not_activate_core_capabilities_without_contracts(tmp_path) -> None:
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
            assert pack["pass_rate"] == 0.0
            assert {result["verdict"] for result in pack["case_results"]} == {"not_run"}
            assert pack["rollback_plan"]["command"]
            assert pack["observe_plan"]["min_observations"] >= 1

        ledger = runtime.learning_ledger(scope=SCOPE, attribute_outcomes=False)
        for capability in CORE_CAPABILITIES:
            item = ledger["capabilities"][capability]
            assert item["status"] != "active"
            assert item["score"] == 0.0
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
        assert len(records) == report["case_count"] + 1
        case_records = [record for record in records if record.meta["report_type"] == "capability_replay_pack"]
        manifest = next(record for record in records if record.meta["report_type"] == "capability_replay_manifest")
        assert {record.meta["capability"] for record in case_records} == {"memory.recall"}
        assert {record.meta["verdict"] for record in case_records} == {"not_run"}
        assert report["manifest_record_id"] == manifest.record_id
        assert manifest.content["schema_version"] == "capability_replay_manifest.v1"
        assert manifest.content["execution_id"] == report["execution_id"]
        assert manifest.content["complete"] is True
        assert manifest.content["member_record_ids"] == {"memory.recall": report["persisted_replay_ids"]}
        assert manifest.content["expected_case_ids"] == {
            "memory.recall": ["recall_version_truth", "recall_low_score_root_cause", "recall_graph_route"]
        }
        assert manifest.provenance["manifest_digest"] == manifest.content["manifest_digest"]
    finally:
        runtime.close()


def test_capability_replay_packs_include_named_weak_capability_cases(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        run_capability_acceptance(runtime, scope=SCOPE, persist=True)
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


def test_capability_replay_rejects_bare_executor_pass_without_contract_chain(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.run_capability_replay_case = lambda case: {  # type: ignore[attr-defined]
        "verdict": "pass",
        "hit": True,
        "observed": f"verified:{case['case_id']}",
        "evidence_source_id": f"fake-trace:{case['case_id']}",
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
    assert {item["reason"] for item in report["packs"][0]["case_results"]} == {"missing_contract_replay_trace_id"}


def test_capability_replay_rejects_wrong_contract_schema_from_executor(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        run_capability_acceptance(runtime, scope=SCOPE, persist=True)
        real_executor = runtime.run_capability_replay_case

        def wrong_schema(case):
            result = dict(real_executor(case))
            result["contract_schema"] = "capability_contract.v0"
            return result

        runtime.run_capability_replay_case = wrong_schema  # type: ignore[attr-defined]
        report = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            capabilities=["search.discovery"],
        )
    finally:
        runtime.close()

    assert report["packs"][0]["pass_rate"] == 0.0
    assert {item["reason"] for item in report["packs"][0]["case_results"]} == {"invalid_contract_replay_schema"}


def test_runtime_executor_rejects_trace_schema_mismatch_in_content_meta_or_provenance(tmp_path) -> None:
    for location in ("content", "meta", "provenance"):
        runtime = Runtime.create(root=tmp_path / location)
        try:
            acceptance = run_capability_acceptance(runtime, scope=SCOPE, persist=True)
            trace_record = runtime.store.get_by_id(acceptance["results"][0]["trace_record_id"], scope=SCOPE)
            assert trace_record is not None
            getattr(trace_record, location)["schema_version"] = "outcome_trace.v0"
            runtime.store.append(trace_record)

            result = execute_capability_replay_case(
                runtime,
                {
                    "case_id": acceptance["results"][0]["case_id"],
                    "target_capability": acceptance["results"][0]["capability"],
                    "scope": SCOPE,
                },
            )
        finally:
            runtime.close()

        assert result["verdict"] == "fail", location
        assert result["reason"] == "outcome_trace_schema_mismatch", location


def test_runtime_executor_rejects_probe_input_even_with_synchronized_digest(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        acceptance = run_capability_acceptance(runtime, scope=SCOPE, persist=True)
        accepted = acceptance["results"][0]
        probe = runtime.store.get_by_id(accepted["probe_id"], scope=SCOPE)
        trace = runtime.store.get_by_id(accepted["trace_record_id"], scope=SCOPE)
        assert probe is not None and trace is not None
        forged_input = {**probe.content["input"], "recency_window": "forged-window"}
        forged_digest = capability_acceptance_digest(
            executor_id=probe.content["executor_id"],
            executor_version=probe.content["executor_version"],
            input_data=forged_input,
            output=probe.content["output"],
            observation=probe.content["observation"],
            checks=probe.content["checks"],
        )
        probe.content["input"] = forged_input
        probe.content["execution_digest"] = forged_digest
        probe.meta["artifact_digest"] = forged_digest
        probe.provenance["artifact_digest"] = forged_digest
        trace.content["payload"]["verifier"]["artifact_digest"] = forged_digest
        runtime.store.append(probe)
        runtime.store.append(trace)

        result = execute_capability_replay_case(
            runtime,
            {"case_id": accepted["case_id"], "target_capability": accepted["capability"], "scope": SCOPE},
        )
    finally:
        runtime.close()

    assert result["verdict"] == "fail"
    assert result["reason"] == "probe_input_canonical_mismatch"


def test_runtime_executor_rejects_output_checks_or_digest_tampering(tmp_path) -> None:
    for field in ("output", "checks", "execution_digest"):
        runtime = Runtime.create(root=tmp_path / field)
        try:
            acceptance = run_capability_acceptance(runtime, scope=SCOPE, persist=True)
            accepted = acceptance["results"][0]
            probe = runtime.store.get_by_id(accepted["probe_id"], scope=SCOPE)
            assert probe is not None
            if field == "output":
                probe.content["output"] = {**probe.content["output"], "source_verified": False}
            elif field == "checks":
                probe.content["checks"][0]["observed"] = "tampered"
            else:
                probe.content["execution_digest"] = "0" * 64
            runtime.store.append(probe)

            result = execute_capability_replay_case(
                runtime,
                {"case_id": accepted["case_id"], "target_capability": accepted["capability"], "scope": SCOPE},
            )
        finally:
            runtime.close()

        assert result["verdict"] == "fail", field
        assert result["reason"] == "probe_execution_evidence_mismatch", field


def test_runtime_executor_rejects_wrong_canonical_acceptance_trace_id(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        acceptance = run_capability_acceptance(runtime, scope=SCOPE, persist=True)
        accepted = acceptance["results"][0]
        trace = runtime.store.get_by_id(accepted["trace_record_id"], scope=SCOPE)
        assert trace is not None
        wrong_trace_id = "capability-acceptance-wrong-execution-wrong-case-wrong-probe"
        trace.content["payload"]["trace_id"] = wrong_trace_id
        trace.meta["trace_id"] = wrong_trace_id
        trace.meta["business_meta"]["trace_id"] = wrong_trace_id
        trace.provenance["trace_id"] = wrong_trace_id
        runtime.store.append(trace)

        result = execute_capability_replay_case(
            runtime,
            {"case_id": accepted["case_id"], "target_capability": accepted["capability"], "scope": SCOPE},
        )
    finally:
        runtime.close()

    assert result["verdict"] == "fail"
    assert result["reason"] == "outcome_trace_acceptance_id_mismatch"


def test_runtime_executor_rejects_contract_with_probe_false(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        acceptance = run_capability_acceptance(runtime, scope=SCOPE, persist=True)
        accepted = acceptance["results"][0]
        trace = runtime.store.get_by_id(accepted["trace_record_id"], scope=SCOPE)
        assert trace is not None
        trace.content["payload"]["capability_contract"]["probe"] = False
        runtime.store.append(trace)

        result = execute_capability_replay_case(
            runtime,
            {"case_id": accepted["case_id"], "target_capability": accepted["capability"], "scope": SCOPE},
        )
    finally:
        runtime.close()

    assert result["verdict"] == "fail"
    assert result["reason"] == "capability_contract_probe_required"


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


def test_runtime_executor_prefers_freshest_valid_acceptance_trace(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        first = run_capability_acceptance(runtime, scope=SCOPE, persist=True)["results"][0]
        second = run_capability_acceptance(runtime, scope=SCOPE, persist=True)["results"][0]
        stale, fresh = sorted((first, second), key=lambda item: item["trace_record_id"])
        now = datetime.now(timezone.utc)
        record_times = {
            stale["probe_id"]: now - timedelta(seconds=1),
            stale["trace_record_id"]: now - timedelta(seconds=3),
            fresh["probe_id"]: now - timedelta(seconds=2),
            fresh["trace_record_id"]: now,
        }
        for record_id, created_at in record_times.items():
            timestamp = created_at.isoformat(timespec="microseconds")
            record = runtime.store.get_by_id(record_id, scope=SCOPE)
            assert record is not None
            record.time.created_at = timestamp
            record.time.updated_at = timestamp
            record.time.occurred_at = timestamp
            runtime.store.rewrite(record)

        replay = execute_capability_replay_case(
            runtime,
            {
                "case_id": fresh["case_id"],
                "target_capability": fresh["capability"],
                "scope": SCOPE,
            },
        )
    finally:
        runtime.close()

    assert replay["verdict"] == "pass"
    assert replay["probe_source_id"] == fresh["probe_id"]
    assert replay["trace_record_id"] == fresh["trace_record_id"]


def test_runtime_executor_does_not_fallback_when_freshest_trace_is_invalid(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        old = run_capability_acceptance(runtime, scope=SCOPE, persist=True)["results"][0]
        latest = run_capability_acceptance(runtime, scope=SCOPE, persist=True)["results"][0]
        old_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="microseconds")
        for record_id in (old["probe_id"], old["trace_record_id"]):
            record = runtime.store.get_by_id(record_id, scope=SCOPE)
            assert record is not None
            record.time.created_at = old_time
            runtime.store.rewrite(record)
        latest_trace = runtime.store.get_by_id(latest["trace_record_id"], scope=SCOPE)
        assert latest_trace is not None
        latest_trace.content["schema_version"] = "outcome_trace.invalid"
        runtime.store.rewrite(latest_trace)

        replay = execute_capability_replay_case(
            runtime,
            {"case_id": latest["case_id"], "target_capability": latest["capability"], "scope": SCOPE},
        )
    finally:
        runtime.close()

    assert replay["verdict"] == "fail"
    assert replay["reason"] == "outcome_trace_schema_mismatch"


def test_runtime_executor_binds_requested_acceptance_execution_and_probe(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        bound_report = run_capability_acceptance(runtime, scope=SCOPE, persist=True)
        bound = bound_report["results"][0]
        run_capability_acceptance(runtime, scope=SCOPE, persist=True)

        replay = execute_capability_replay_case(
            runtime,
            {
                "case_id": bound["case_id"],
                "target_capability": bound["capability"],
                "scope": SCOPE,
                "acceptance_execution_id": bound_report["execution_id"],
                "required_probe_source_id": bound["probe_id"],
            },
        )
    finally:
        runtime.close()

    assert replay["verdict"] == "pass"
    assert replay["probe_source_id"] == bound["probe_id"]


def test_runtime_executor_rejects_inactive_trace_or_probe(tmp_path) -> None:
    for inactive_record in ("trace", "probe"):
        runtime = Runtime.create(root=tmp_path / inactive_record)
        try:
            accepted = run_capability_acceptance(runtime, scope=SCOPE, persist=True)["results"][0]
            record_id = accepted["trace_record_id"] if inactive_record == "trace" else accepted["probe_id"]
            record = runtime.store.get_by_id(record_id, scope=SCOPE)
            assert record is not None
            record.status = "quarantined"
            runtime.store.rewrite(record)
            replay = execute_capability_replay_case(
                runtime,
                {"case_id": accepted["case_id"], "target_capability": accepted["capability"], "scope": SCOPE},
            )
        finally:
            runtime.close()

        assert replay["verdict"] == "fail", inactive_record
        assert replay["reason"] in {"outcome_trace_status_invalid", "invalid_capability_probe"}, inactive_record


def test_runtime_executor_rejects_invalid_or_future_candidate_times(tmp_path) -> None:
    unsafe_times = {
        "malformed": "not-a-timestamp",
        "naive": "2026-07-12T05:00:00",
        "future": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="microseconds"),
        "inconsistent": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="microseconds"),
    }
    for label, unsafe_time in unsafe_times.items():
        runtime = Runtime.create(root=tmp_path / label)
        try:
            accepted = run_capability_acceptance(runtime, scope=SCOPE, persist=True)["results"][0]
            trace = runtime.store.get_by_id(accepted["trace_record_id"], scope=SCOPE)
            assert trace is not None
            trace.time.created_at = unsafe_time
            runtime.store.rewrite(trace)
            replay = execute_capability_replay_case(
                runtime,
                {"case_id": accepted["case_id"], "target_capability": accepted["capability"], "scope": SCOPE},
            )
        finally:
            runtime.close()

        assert replay["verdict"] == "fail", label
        assert replay["reason"] in {
            "candidate_evidence_time_invalid",
            "candidate_evidence_time_in_future",
            "candidate_evidence_time_inconsistent",
        }, label


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


def test_runtime_executor_rejects_wrong_case_contract(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_contract_trace(
            runtime,
            scope=SCOPE,
            case_id="search_recent_source",
            verifier_passed=True,
            probe_scope=SCOPE,
            trace_text="Recent recency window source trust GitHub trending created range stars verified",
            duplicate_trace=False,
        )
        result = execute_capability_replay_case(
            runtime,
            {"case_id": "search_trending_github", "target_capability": "search.discovery", "scope": SCOPE},
        )
    finally:
        runtime.close()

    assert result["verdict"] == "not_run"
    assert result["reason"] == "contract_backed_outcome_evidence_missing"


def test_runtime_executor_rejects_probe_missing_from_scope(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_contract_trace(
            runtime,
            scope=SCOPE,
            case_id="search_recent_source",
            verifier_passed=True,
            probe_scope={**SCOPE, "workspace_id": "other-scope"},
            trace_text="Recent recency window source trust verified",
            duplicate_trace=False,
        )
        result = execute_capability_replay_case(
            runtime,
            {"case_id": "search_recent_source", "target_capability": "search.discovery", "scope": SCOPE},
        )
    finally:
        runtime.close()

    assert result["reason"] == "probe_source_unavailable_in_scope"
    assert result["verdict"] == "not_run"


def test_runtime_executor_rejects_failed_verifier(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_contract_trace(
            runtime,
            scope=SCOPE,
            case_id="search_recent_source",
            verifier_passed=False,
            probe_scope=SCOPE,
            trace_text="Recent recency window source trust verified",
            duplicate_trace=False,
        )
        result = execute_capability_replay_case(
            runtime,
            {"case_id": "search_recent_source", "target_capability": "search.discovery", "scope": SCOPE},
        )
    finally:
        runtime.close()

    assert result["verdict"] == "not_run"
    assert result["reason"] == "contract_backed_outcome_evidence_missing"


def test_runtime_executor_rejects_reused_probe_source(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_contract_trace(
            runtime,
            scope=SCOPE,
            case_id="search_recent_source",
            verifier_passed=True,
            probe_scope=SCOPE,
            trace_text="Recent recency window source trust verified",
            duplicate_trace=True,
        )
        result = execute_capability_replay_case(
            runtime,
            {"case_id": "search_recent_source", "target_capability": "search.discovery", "scope": SCOPE},
        )
    finally:
        runtime.close()

    assert result["verdict"] == "fail"
    assert result["reason"] == "reused_probe_source"


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
    artifact = capability_acceptance.capability_acceptance_case(case_id)
    probe_id = f"probe-{case_id}"
    execution_id = "fixture-execution"
    execution = execute_probe(artifact, runtime=runtime, evidence_ref=probe_id)
    digest = execution["execution_digest"]
    checks = execution["checks"]
    probe = RecordEnvelope.create(
        kind="replay_result",
        title=f"Capability acceptance probe: {case_id}",
        summary="pass: canonical non-destructive artifact validation",
        scope=ScopeRef.from_dict(probe_scope),
        source="eimemory.capability_acceptance",
        content={
            "report_type": "capability_probe_result",
            "schema_version": PROBE_SCHEMA_VERSION,
            "execution_id": execution_id,
            "case_id": case_id,
            "capability": artifact["capability"],
            "executor_id": execution["executor_id"],
            "executor_version": execution["executor_version"],
            "input": dict(execution["input"]),
            "output": dict(execution["output"]),
            "checks": checks,
            "observation": dict(execution["observation"]),
            "execution_digest": digest,
            "passed": True,
            "verdict": "pass",
            "validator": {"schema_version": CONTRACT_SCHEMA_VERSION, "passed": True, "error": ""},
        },
        meta={
            "report_type": "capability_probe_result",
            "schema_version": PROBE_SCHEMA_VERSION,
            "execution_id": execution_id,
            "case_id": case_id,
            "capability": artifact["capability"],
            "artifact_digest": digest,
            "passed": True,
            "verdict": "pass",
        },
        provenance={
            "report_type": "capability_probe_result",
            "schema_version": PROBE_SCHEMA_VERSION,
            "execution_id": execution_id,
            "artifact_digest": digest,
        },
    )
    probe.record_id = probe_id
    runtime.store.append(probe)
    contract = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "capability": artifact["capability"],
        "case_id": case_id,
        "observations": dict(execution["observation"]),
        "checks": checks,
        "source_record_ids": [probe_id],
        "probe": True,
    }
    for index in range(2 if duplicate_trace else 1):
        trace_id = f"capability-acceptance-{execution_id}-{case_id}-{probe_id}"
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
                    "verifier": {
                        "passed": verifier_passed,
                        "method": "execute_capability_probe",
                        "evidence_ref": probe_id,
                        "artifact_digest": digest,
                    },
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
                "business_meta": {
                    "report_type": "outcome_trace",
                    "schema_version": "outcome_trace.v1",
                    "trace_id": trace_id,
                    "outcome_status": "success",
                    "primary_label": "success",
                    "capability": artifact["capability"],
                    "capability_case_id": case_id,
                    "contract_verified": True,
                },
            },
            provenance={
                "report_type": "outcome_trace",
                "schema_version": "outcome_trace.v1",
                "trace_id": trace_id,
                "idempotency_key": f"fixture-{case_id}-{index}",
            },
        )
        runtime.store.append(trace)


def test_replay_rerun_persists_new_execution_and_readiness_uses_latest(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        run_capability_acceptance(runtime, scope=SCOPE, persist=True)
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
