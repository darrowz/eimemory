import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.experience.capability_contract import CASE_CONTRACTS
from eimemory.governance import capability_acceptance
from eimemory.governance import capability_probe_executor
from eimemory.governance import change_policy
from eimemory.governance import memory_graph
from eimemory.governance import safety_replay
from eimemory.ei_bridge.registry import AgentAdapterRegistry
from eimemory.governance.capability_acceptance import (
    CAPABILITY_ACCEPTANCE_CASE_IDS,
    CORE_CAPABILITY_ACCEPTANCE_CASE_IDS,
    run_capability_acceptance,
)


SCOPE = {
    "tenant_id": "default",
    "agent_id": "acceptance-agent",
    "workspace_id": "capability-acceptance",
    "user_id": "operator",
}


def _probe_records(runtime: Runtime) -> list:
    return [
        record
        for record in runtime.store.list_records(kinds=["replay_result"], scope=SCOPE, limit=100)
        if record.meta.get("report_type") == "capability_probe_result"
    ]


def _outcome_records(runtime: Runtime) -> list:
    return [
        record
        for record in runtime.store.list_records(kinds=["reflection"], scope=SCOPE, limit=100)
        if record.source == "eimemory.experience.outcome_trace"
    ]


def test_public_acceptance_digest_preserves_canonical_artifact_hash() -> None:
    artifact = capability_acceptance.capability_acceptance_case("search_recent_source")
    execution = capability_probe_executor.execute_probe(artifact, runtime=None, evidence_ref="probe-source")

    digest = capability_acceptance.capability_acceptance_digest(
        executor_id=execution["executor_id"],
        executor_version=execution["executor_version"],
        input_data=execution["input"],
        output=execution["output"],
        observation=execution["observation"],
        checks=execution["checks"],
    )

    assert digest == execution["execution_digest"]
    assert len(digest) == 64


def test_public_acceptance_case_returns_read_only_deep_copy() -> None:
    first = capability_acceptance.capability_acceptance_case("search_recent_source")
    first["input"]["recency_window"] = "forged"
    first["fixture"]["sources"][0]["verified"] = False

    second = capability_acceptance.capability_acceptance_case("search_recent_source")

    assert second["input"]["recency_window"] == "30d"
    assert second["fixture"]["sources"][0]["verified"] is True
    assert capability_acceptance.capability_acceptance_case("unknown-case") == {}


def test_acceptance_cases_contain_only_executable_inputs_fixtures_and_invariants() -> None:
    for case_id in CASE_CONTRACTS:
        artifact = capability_acceptance.capability_acceptance_case(case_id)
        assert set(artifact) == {"case_id", "capability", "input", "fixture", "expected_invariants"}
        assert artifact["input"]
        assert artifact["fixture"]
        assert artifact["expected_invariants"]
        assert "observation" not in artifact
        assert "passed" not in artifact


def test_empty_runtime_requires_all_real_probe_executors(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    monkeypatch.delitem(capability_probe_executor.PROBE_EXECUTORS, "search_recent_source")
    try:
        report = run_capability_acceptance(runtime, scope=SCOPE, persist=True)
        failed_probe = next(record for record in _probe_records(runtime) if record.meta["case_id"] == "search_recent_source")
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["pass_count"] == 26
    assert failed_probe.content["passed"] is False
    assert "executor" in failed_probe.content["validator"]["error"]


def test_executor_wrong_raw_output_fails_invariants_and_emits_no_success_trace(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)

    def wrong_output(_input, _fixture, _runtime):
        return {"selected_sources": [], "source_verified": False}

    monkeypatch.setitem(capability_probe_executor.PROBE_EXECUTORS, "search_recent_source", wrong_output)
    try:
        report = run_capability_acceptance(runtime, scope=SCOPE, persist=True)
        failed = next(item for item in report["results"] if item["case_id"] == "search_recent_source")
        traces = _outcome_records(runtime)
    finally:
        runtime.close()

    assert failed["passed"] is False
    assert failed["trace_record_id"] == ""
    assert len(traces) == 26


def test_trace_persistence_failure_rewrites_probe_as_failed(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    original = runtime.record_outcome_trace

    def fail_one(payload, *, scope):
        if payload["capability_case_id"] == "search_recent_source":
            return {"ok": False, "error": "forced trace write failure"}
        return original(payload, scope=scope)

    monkeypatch.setattr(runtime, "record_outcome_trace", fail_one)
    try:
        report = run_capability_acceptance(runtime, scope=SCOPE, persist=True)
        failed = next(item for item in report["results"] if item["case_id"] == "search_recent_source")
        probe = runtime.store.get_by_id(failed["probe_id"], scope=SCOPE)
    finally:
        runtime.close()

    assert failed["passed"] is False
    assert probe is not None
    assert probe.content["passed"] is False
    assert probe.content["verdict"] == "fail"
    assert probe.meta["passed"] is False


def test_acceptance_runs_all_cases_with_distinct_linked_probe_sources(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = run_capability_acceptance(runtime, scope=SCOPE, persist=True)
        probe_records = _probe_records(runtime)
        outcome_records = _outcome_records(runtime)
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["all_passed"] is True
    assert report["case_count"] == 27
    assert report["pass_count"] == 27
    assert report["failed_count"] == 0
    assert {item["case_id"] for item in report["results"]} == set(CAPABILITY_ACCEPTANCE_CASE_IDS)

    probe_ids = [item["probe_id"] for item in report["results"]]
    trace_ids = [item["trace_id"] for item in report["results"]]
    trace_record_ids = [item["trace_record_id"] for item in report["results"]]
    assert len(set(probe_ids)) == 27
    assert len(set(trace_ids)) == 27
    assert len(set(trace_record_ids)) == 27
    assert report["distinct_probe_sources"] is True
    assert report["distinct_trace_ids"] is True

    assert len(probe_records) == 27
    assert len(outcome_records) == 27
    probes_by_id = {record.record_id: record for record in probe_records}
    outcomes_by_id = {record.record_id: record for record in outcome_records}
    for item in report["results"]:
        probe = probes_by_id[item["probe_id"]]
        trace = outcomes_by_id[item["trace_record_id"]]
        assert probe.content["input"]
        assert probe.content["executor_id"]
        assert probe.content["executor_version"]
        assert probe.content["output"]
        assert probe.content["checks"]
        assert probe.content["observation"]
        assert probe.content["execution_digest"]
        assert all(check["passed"] is True for check in probe.content["checks"])
        assert probe.content["execution_id"] == report["execution_id"]
        assert probe.meta["case_id"] == item["case_id"]
        contract = trace.content["payload"]["capability_contract"]
        assert contract["probe"] is True
        assert contract["source_record_ids"] == [probe.record_id]
        assert {check["evidence_ref"] for check in contract["checks"]} == {probe.record_id}
        assert trace.content["payload"]["outcome"]["rehearsal"] is True
        assert trace.content["payload"]["verifier"]["passed"] is True
        assert trace.content["payload"]["verifier"]["evidence_ref"] == probe.record_id


def test_core_acceptance_and_replay_close_every_core_capability(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = "a" * 40
    try:
        acceptance = run_capability_acceptance(
            runtime,
            scope=SCOPE,
            persist=True,
            case_ids=list(CORE_CAPABILITY_ACCEPTANCE_CASE_IDS),
        )
        replay = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            capabilities=[
                "memory.recall",
                "tool.routing",
                "knowledge.intake",
                "proactive.judgment",
                "safety.boundary",
            ],
            acceptance_execution_id=acceptance["execution_id"],
            acceptance_probe_ids_by_case={
                item["case_id"]: item["probe_record_id"] for item in acceptance["results"]
            },
        )
    finally:
        runtime.close()

    assert acceptance["ok"] is True
    assert acceptance["case_count"] == 15
    assert acceptance["pass_count"] == 15
    assert len(set(acceptance["probe_record_ids"])) == 15
    assert replay["persisted_replay_count"] == 15
    assert {item["verdict"] for pack in replay["packs"] for item in pack["case_results"]} == {"pass"}
    assert len(replay["score_record_ids"]) == 5


def test_core_acceptance_fails_when_real_safety_subsystem_is_broken(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = "a" * 40
    monkeypatch.setattr(safety_replay, "classify_safety_action", lambda _payload: "allow")
    try:
        report = run_capability_acceptance(
            runtime,
            scope=SCOPE,
            persist=True,
            case_ids=["safety_secret"],
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["case_count"] == 1
    assert report["pass_count"] == 0
    assert report["results"][0]["case_id"] == "safety_secret"
    assert report["results"][0]["passed"] is False


def test_core_acceptance_fails_when_real_memory_graph_subsystem_is_broken(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = "a" * 40
    monkeypatch.setattr(
        memory_graph,
        "graph_route_for_query",
        lambda *_args, **_kwargs: {"primary": "semantic", "edge_types": ["semantic"], "event_graph": False},
    )
    try:
        report = run_capability_acceptance(
            runtime,
            scope=SCOPE,
            persist=False,
            case_ids=["recall_graph_route"],
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["results"][0]["passed"] is False


def test_core_acceptance_fails_when_real_bridge_router_subsystem_is_broken(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = "a" * 40
    monkeypatch.setattr(AgentAdapterRegistry, "find", lambda _self, _target: None)
    try:
        report = run_capability_acceptance(
            runtime,
            scope=SCOPE,
            persist=False,
            case_ids=["route_query_first"],
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["results"][0]["passed"] is False


def test_core_acceptance_fails_when_real_intake_policy_subsystem_is_broken(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = "a" * 40
    monkeypatch.setattr(Runtime, "source_quality_report", lambda _self, **_kwargs: {"sources": []})
    try:
        report = run_capability_acceptance(
            runtime,
            scope=SCOPE,
            persist=False,
            case_ids=["intake_source_quality"],
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["results"][0]["passed"] is False


def test_core_acceptance_fails_when_real_change_policy_subsystem_is_broken(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = "a" * 40
    monkeypatch.setattr(change_policy, "decide_change_policy", lambda **_kwargs: {"decision": "observe"})
    try:
        report = run_capability_acceptance(
            runtime,
            scope=SCOPE,
            persist=False,
            case_ids=["judge_need_version_bump"],
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["results"][0]["passed"] is False


def test_core_replay_rejects_acceptance_evidence_from_user_alias_scope(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = "a" * 40
    shared_scope = {**SCOPE, "user_id": ""}
    try:
        acceptance = run_capability_acceptance(
            runtime,
            scope=shared_scope,
            persist=True,
            case_ids=list(CORE_CAPABILITY_ACCEPTANCE_CASE_IDS),
        )
        replay = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=False,
            capabilities=[
                "memory.recall",
                "tool.routing",
                "knowledge.intake",
                "proactive.judgment",
                "safety.boundary",
            ],
            acceptance_execution_id=acceptance["execution_id"],
            acceptance_probe_ids_by_case={
                item["case_id"]: item["probe_record_id"] for item in acceptance["results"]
            },
        )
    finally:
        runtime.close()

    assert acceptance["ok"] is True
    assert {
        item["verdict"]
        for pack in replay["packs"]
        for item in pack["case_results"]
    } <= {"fail", "not_run"}
    assert {
        item["reason"]
        for pack in replay["packs"]
        for item in pack["case_results"]
    } == {"contract_backed_outcome_evidence_missing"}


def test_acceptance_rejects_empty_case_selection_without_writes(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = run_capability_acceptance(runtime, scope=SCOPE, persist=True, case_ids=[])
        records = runtime.store.list_records(scope=SCOPE, limit=10)
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["all_passed"] is False
    assert report["status"] == "rejected"
    assert report["blocked_reasons"] == ["empty_case_ids"]
    assert report["case_count"] == 0
    assert report["distinct_probe_sources"] is False
    assert records == []


def test_acceptance_validator_failure_is_persisted_without_success_trace(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    original_validate = capability_acceptance.validate_capability_contract

    def fail_one(contract, **kwargs):
        if contract.get("case_id") == "device_safe_boundary":
            return "forced validator failure"
        return original_validate(contract, **kwargs)

    monkeypatch.setattr(capability_acceptance, "validate_capability_contract", fail_one)
    try:
        report = run_capability_acceptance(runtime, scope=SCOPE, persist=True)
        probe_records = _probe_records(runtime)
        outcome_records = _outcome_records(runtime)
        event_count = runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        task_success_records = [
            record
            for record in runtime.store.list_records(scope=SCOPE, limit=100)
            if record.meta.get("task_success") is True or record.content.get("task_success") is True
        ]
    finally:
        runtime.close()

    failed = [item for item in report["results"] if not item["passed"]]
    assert report["ok"] is False
    assert report["all_passed"] is False
    assert report["pass_count"] == 26
    assert report["failed_count"] == 1
    assert len(failed) == 1
    assert failed[0]["case_id"] == "device_safe_boundary"
    assert failed[0]["error"] == "forced validator failure"
    assert failed[0]["trace_record_id"] == ""
    assert len({item["probe_id"] for item in report["results"]}) == 27
    assert len({item["trace_id"] for item in report["results"]}) == 27
    assert len(probe_records) == 27
    assert len(outcome_records) == 26
    assert failed[0]["probe_id"] in {record.record_id for record in probe_records}
    assert failed[0]["probe_id"] not in {
        source_id
        for record in outcome_records
        for source_id in record.content["payload"]["capability_contract"]["source_record_ids"]
    }
    assert event_count == 0
    assert task_success_records == []


def test_acceptance_default_execution_ids_are_fresh_and_dry_run_has_no_writes(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        first = run_capability_acceptance(runtime, scope=SCOPE, persist=False)
        second = run_capability_acceptance(runtime, scope=SCOPE, persist=False)
        records = runtime.store.list_records(scope=SCOPE, limit=100)
        event_count = runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        runtime.close()

    assert first["execution_id"] != second["execution_id"]
    assert first["persisted"] is False
    assert second["persisted"] is False
    assert len({item["probe_id"] for item in first["results"]}) == 27
    assert len({item["trace_id"] for item in first["results"]}) == 27
    assert records == []
    assert event_count == 0


def test_reused_explicit_execution_id_still_links_each_trace_to_its_current_probe(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        first = run_capability_acceptance(runtime, scope=SCOPE, persist=True, execution_id="shared-execution")
        second = run_capability_acceptance(runtime, scope=SCOPE, persist=True, execution_id="shared-execution")
        second_trace_sources = {
            item["case_id"]: runtime.store.get_by_id(item["trace_record_id"], scope=SCOPE)
            .content["payload"]["capability_contract"]["source_record_ids"]
            for item in second["results"]
        }
    finally:
        runtime.close()

    assert first["ok"] is True
    assert second["ok"] is True
    assert set(first["trace_record_ids"]).isdisjoint(second["trace_record_ids"])
    for item in second["results"]:
        assert second_trace_sources[item["case_id"]] == [item["probe_id"]]


def test_runtime_and_cli_expose_persisted_capability_acceptance(tmp_path, monkeypatch, capsys) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        runtime_report = runtime.run_capability_acceptance(scope=SCOPE, persist=False)
    finally:
        runtime.close()
    assert runtime_report["ok"] is True
    assert runtime_report["case_count"] == 27

    cli_root = tmp_path / "cli"
    monkeypatch.setenv("EIMEMORY_ROOT", str(cli_root))
    assert cli_main(["learn", "capability-acceptance", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["persisted"] is True
    assert output["case_count"] == 27
    persisted_runtime = Runtime.create(root=cli_root)
    try:
        assert len(_probe_records_for_scope(persisted_runtime, output["scope"])) == 27
    finally:
        persisted_runtime.close()


def test_cli_exits_nonzero_when_probe_sources_are_not_distinct(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    def invalid_report(self, **kwargs):
        return {
            "ok": True,
            "all_passed": True,
            "case_count": 27,
            "pass_count": 27,
            "distinct_probe_sources": False,
            "distinct_trace_ids": True,
            "results": [
                {"case_id": case_id, "passed": True, "probe_id": "duplicate", "trace_id": f"trace-{index}"}
                for index, case_id in enumerate(CAPABILITY_ACCEPTANCE_CASE_IDS)
            ],
        }

    monkeypatch.setattr(Runtime, "run_capability_acceptance", invalid_report)
    assert cli_main(["learn", "capability-acceptance", "--json"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True


def _probe_records_for_scope(runtime: Runtime, scope: dict) -> list:
    return [
        record
        for record in runtime.store.list_records(kinds=["replay_result"], scope=scope, limit=100)
        if record.meta.get("report_type") == "capability_probe_result"
    ]
