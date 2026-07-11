import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.experience.capability_contract import CASE_CONTRACTS
from eimemory.governance import capability_acceptance
from eimemory.governance.capability_acceptance import run_capability_acceptance


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
    artifact = capability_acceptance.CAPABILITY_ACCEPTANCE_CASES[0]

    digest = capability_acceptance.capability_acceptance_digest(
        capability=artifact["capability"],
        case_id=artifact["case_id"],
        input_data=artifact["input"],
        observation=artifact["observation"],
    )

    assert digest == "ebad46fc05e2393d772072ee687c07f4eedf897322f2baba596d6fe1c5d2be2e"


def test_acceptance_runs_all_twelve_cases_with_distinct_linked_probe_sources(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = run_capability_acceptance(runtime, scope=SCOPE, persist=True)
        probe_records = _probe_records(runtime)
        outcome_records = _outcome_records(runtime)
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["all_passed"] is True
    assert report["case_count"] == 12
    assert report["pass_count"] == 12
    assert report["failed_count"] == 0
    assert {item["case_id"] for item in report["results"]} == set(CASE_CONTRACTS)

    probe_ids = [item["probe_id"] for item in report["results"]]
    trace_ids = [item["trace_id"] for item in report["results"]]
    trace_record_ids = [item["trace_record_id"] for item in report["results"]]
    assert len(set(probe_ids)) == 12
    assert len(set(trace_ids)) == 12
    assert len(set(trace_record_ids)) == 12
    assert report["distinct_probe_sources"] is True
    assert report["distinct_trace_ids"] is True

    assert len(probe_records) == 12
    assert len(outcome_records) == 12
    probes_by_id = {record.record_id: record for record in probe_records}
    outcomes_by_id = {record.record_id: record for record in outcome_records}
    for item in report["results"]:
        probe = probes_by_id[item["probe_id"]]
        trace = outcomes_by_id[item["trace_record_id"]]
        assert probe.content["input"]
        assert probe.content["checks"]
        assert probe.content["observation"]
        assert probe.content["digest"]
        assert probe.content["execution_id"] == report["execution_id"]
        assert probe.meta["case_id"] == item["case_id"]
        contract = trace.content["payload"]["capability_contract"]
        assert contract["probe"] is True
        assert contract["source_record_ids"] == [probe.record_id]
        assert {check["evidence_ref"] for check in contract["checks"]} == {probe.record_id}
        assert trace.content["payload"]["outcome"]["rehearsal"] is True
        assert trace.content["payload"]["verifier"]["passed"] is True
        assert trace.content["payload"]["verifier"]["evidence_ref"] == probe.record_id


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
    assert report["pass_count"] == 11
    assert report["failed_count"] == 1
    assert len(failed) == 1
    assert failed[0]["case_id"] == "device_safe_boundary"
    assert failed[0]["error"] == "forced validator failure"
    assert failed[0]["trace_record_id"] == ""
    assert len({item["probe_id"] for item in report["results"]}) == 12
    assert len({item["trace_id"] for item in report["results"]}) == 12
    assert len(probe_records) == 12
    assert len(outcome_records) == 11
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
    assert len({item["probe_id"] for item in first["results"]}) == 12
    assert len({item["trace_id"] for item in first["results"]}) == 12
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
    assert runtime_report["case_count"] == 12

    cli_root = tmp_path / "cli"
    monkeypatch.setenv("EIMEMORY_ROOT", str(cli_root))
    assert cli_main(["learn", "capability-acceptance", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["persisted"] is True
    assert output["case_count"] == 12
    persisted_runtime = Runtime.create(root=cli_root)
    try:
        assert len(_probe_records_for_scope(persisted_runtime, output["scope"])) == 12
    finally:
        persisted_runtime.close()


def test_cli_exits_nonzero_when_probe_sources_are_not_distinct(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    def invalid_report(self, **kwargs):
        return {
            "ok": True,
            "all_passed": True,
            "case_count": 12,
            "pass_count": 12,
            "distinct_probe_sources": False,
            "distinct_trace_ids": True,
            "results": [
                {"case_id": case_id, "passed": True, "probe_id": "duplicate", "trace_id": f"trace-{index}"}
                for index, case_id in enumerate(CASE_CONTRACTS)
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
