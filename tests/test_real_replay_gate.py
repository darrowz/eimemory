from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json

from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.core.clock import now_iso
from eimemory.experience.outcome import build_outcome_trace_record
from eimemory.evaluation.task_replay import run_real_task_replay
from eimemory.governance.real_replay_gate import (
    MIN_VERIFIED_REAL_REPLAY_PASS_RATE,
    MIN_VERIFIED_REAL_REPLAY_SAMPLES,
    MIN_VERIFIED_REAL_REPLAY_TASK_TYPES,
    build_verified_real_replay_summary,
)
from eimemory.governance.evidence_contract import (
    ReleaseIdentity,
    current_release_identity,
    release_identity_payload,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.runtime_identity import runtime_package_tree_digest


SCOPE = ScopeRef(
    agent_id="real-replay-agent",
    workspace_id="real-replay-workspace",
    user_id="real-replay-user",
)


def _append_real_source(
    runtime: Runtime,
    *,
    index: int,
    task_type: str,
    scope: ScopeRef = SCOPE,
    rehearsal: bool = False,
    success: bool = True,
    source: str = "openclaw.task_end",
    evidence_class: str = "verified_real_task",
) -> str:
    release = _ensure_release(runtime, scope=scope)
    event_id = f"evt-real-replay-{index}"
    trace_id = f"trace-{index}"
    verification = "verified terminal result"
    terminal_digest = AgentRuntimeMemoryService._terminal_contract_digest(
        {
            "channel": source.split(".", 1)[0],
            "scope": asdict(scope),
            "end_kind": source.split(".", 1)[1],
            "session_id": f"session-{index}",
            "event_id": f"run-{index}",
            "task_type": task_type,
            "success": success,
            "rehearsal": rehearsal,
            "verification": verification,
            "result": "",
            "receipt_ids": [],
        }
    )
    event_payload = {
        "id": event_id,
        "source": source,
        "hook": source.split(".", 1)[1] if "." in source else "",
        "session_id": f"session-{index}",
        "run_id": f"run-{index}",
        "event_type": task_type,
        "outcome_trace_id": trace_id,
        "outcome_trace_task_type": task_type,
        "verification": verification,
        "verification_receipts": [],
        "result": "",
        "terminal_contract_digest": terminal_digest,
        **release_identity_payload(release),
    }
    outcome_payload = {
        "outcome": "good" if success else "bad",
        "success": success,
        "source": source,
        "source_trust": "system_verified",
        "terminal_contract_digest": terminal_digest,
    }
    trace_payload = {
        "trace_id": trace_id,
        "idempotency_key": f"{source}:session-{index}:run-{index}",
        "session_id": f"session-{index}",
        "source": source,
        "task_type": task_type,
        "input_summary": "must never enter replay evidence",
        "platform_message_id": f"secret-platform-id-{index}",
        "outcome": {
            "status": "good" if success else "bad",
            "success": success,
            "rehearsal": rehearsal,
        },
        "verifier": {
            "passed": success,
            "method": source,
            "evidence_refs": [event_id],
        },
        "evidence_class": evidence_class,
        "terminal_contract_digest": terminal_digest,
        "recorded_at": now_iso(),
        **release_identity_payload(release),
    }
    trace_build = build_outcome_trace_record(trace_payload, scope=scope)
    terminal = runtime.store.record_terminal_bundle(
        verified_receipts=[],
        channel="openclaw",
        session_id=f"session-{index}",
        run_id=f"run-{index}",
        trace_id=trace_id,
        event_payload=event_payload,
        outcome_payload=outcome_payload,
        trace_record=trace_build.record,
        scope=asdict(scope),
    )
    assert terminal["outcome_trace"]["ok"] is True
    return terminal["outcome_trace"]["record_id"]


def _ensure_release(runtime: Runtime, *, scope: ScopeRef) -> ReleaseIdentity:
    release = current_release_identity(runtime, scope)
    if release is not None:
        return release
    commit = "a" * 40
    version = "1.9.86"
    release_path = f"/opt/eimemory/releases/{commit}"
    runtime._test_runtime_commit = commit
    runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Verified real replay deployment",
            scope=scope,
            source="eimemory.deployment_receipt",
            status="deployed",
            content={
                "report_type": "deployment_receipt",
                "promotion_target": "code_patch",
                "action": "code_patch",
                "gate": {"ok": True, "receipt_verified": True},
                "side_effect": {
                    "ok": True,
                    "production_applied": True,
                    "deployment_executed": True,
                    "verification": {"ok": True, "skipped": False},
                    "deployment": {
                        "ok": True,
                        "skipped": False,
                        "release_path": release_path,
                    },
                    "post_deploy_health": {
                        "ok": True,
                        "skipped": False,
                        "commit": commit,
                        "version": version,
                        "release_path": release_path,
                    },
                    "commit": {"commit_sha": commit},
                    "release": {
                        "version": version,
                        "release_path": release_path,
                    },
                    "rollback_evidence": {
                        "prior_commit_sha": "b" * 40,
                        "rollback_command": "verified rollback",
                    },
                },
            },
            meta={"report_type": "deployment_receipt"},
        )
    )
    release = current_release_identity(runtime, scope)
    assert release is not None
    return release


def _clone_source_with_same_terminal_evidence(
    runtime: Runtime,
    *,
    source_record_id: str,
    suffix: str,
) -> str:
    source = runtime.store.get_by_id(source_record_id, scope=SCOPE)
    assert source is not None
    payload = deepcopy(source.content["payload"])
    evidence_ref = str(payload["verifier"]["evidence_refs"][0])
    conn = runtime.store.sqlite.conn
    event_row = conn.execute(
        "SELECT payload_json FROM events WHERE id=?",
        (evidence_ref,),
    ).fetchone()
    outcome_row = conn.execute(
        "SELECT payload_json FROM event_outcomes WHERE event_id=?",
        (evidence_ref,),
    ).fetchone()
    assert event_row is not None and outcome_row is not None
    event_payload = json.loads(str(event_row["payload_json"]))
    event_payload["id"] = f"{evidence_ref}-{suffix}"
    cloned_event = runtime.store.record_event(event_payload, scope=asdict(SCOPE))
    outcome_payload = json.loads(str(outcome_row["payload_json"]))
    for generated_key in ("id", "event_id", "recorded_at"):
        outcome_payload.pop(generated_key, None)
    runtime.record_outcome(
        cloned_event["id"],
        outcome_payload,
        scope=asdict(SCOPE),
    )
    payload["verifier"]["evidence_refs"] = [cloned_event["id"]]
    payload["idempotency_key"] = f"{payload['idempotency_key']}:{suffix}"
    clone = build_outcome_trace_record(payload, scope=SCOPE)
    assert clone.record.record_id != source_record_id
    return runtime.store.append(clone.record).record_id


def _append_forged_source(
    runtime: Runtime,
    *,
    index: int,
    task_type: str,
    scope: ScopeRef = SCOPE,
    rehearsal: bool = False,
    success: bool = True,
    source: str = "openclaw.task_end",
    evidence_class: str = "verified_real_task",
) -> str:
    build = build_outcome_trace_record(
        {
                "trace_id": f"trace-{index}",
                "idempotency_key": f"forged-{index}",
                "session_id": f"session-{index}",
                "source": source,
                "task_type": task_type,
                "input_summary": "must never enter replay evidence",
                "platform_message_id": f"secret-platform-id-{index}",
                "outcome": {
                    "status": "success" if success else "failed",
                    "success": success,
                    "rehearsal": rehearsal,
                },
                "evidence_class": evidence_class,
                "verifier": {
                    "passed": success,
                    "method": source,
                    "evidence_refs": [f"missing-event-{index}"],
                },
                "recorded_at": "2026-07-28T00:00:00+00:00",
        },
        scope=scope,
    )
    return runtime.store.append(build.record).record_id


def _seed_recall_target(runtime: Runtime) -> None:
    runtime.memory.ingest(
        text="The verified replay answer is cobalt.",
        memory_type="fact",
        title="Verified replay target",
        scope=asdict(SCOPE),
        source="test.real_replay",
        force_capture=True,
    )


def _dataset(source_ids: list[str], *, failing: set[int] | None = None) -> dict:
    failing = failing or set()
    return {
        "name": "verified-real-production-replay",
        "scope": asdict(SCOPE),
        "threshold": 0.8,
        "cases": [
            {
                "case_id": f"real-{index}",
                "source_record_id": source_id,
                "query": "verified replay answer",
                "expected_text": ["missing"] if index in failing else ["cobalt"],
            }
            for index, source_id in enumerate(source_ids)
        ],
    }


def _ten_sources(runtime: Runtime) -> list[str]:
    return [
        _append_real_source(
            runtime,
            index=index,
            task_type=f"production.task.{index % 5}",
        )
        for index in range(10)
    ]


def test_verified_real_replay_closes_gate_and_persists_only_redacted_provenance(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_recall_target(runtime)
        source_ids = _ten_sources(runtime)

        report = run_real_task_replay(
            runtime,
            _dataset(source_ids, failing={8, 9}),
            seed=False,
            persist_report=True,
        )
        gate = build_verified_real_replay_summary(runtime, scope=SCOPE)
        record = runtime.store.get_by_id(report["persisted_record_id"], scope=SCOPE)
    finally:
        runtime.close()

    assert report["real_provenance_contract"] == "verified_real_replay.v1"
    assert report["package_tree_digest"] == runtime_package_tree_digest()
    assert report["verified_real_sample_count"] == MIN_VERIFIED_REAL_REPLAY_SAMPLES
    assert report["verified_real_task_types"] == MIN_VERIFIED_REAL_REPLAY_TASK_TYPES
    assert report["pass_rate"] == MIN_VERIFIED_REAL_REPLAY_PASS_RATE
    assert gate["ok"] is True
    assert gate["sample_count"] == 10
    assert gate["distinct_task_types"] == 5
    assert gate["pass_rate"] == 0.8
    assert gate["record_id"] == report["persisted_record_id"]
    serialized = str(report)
    assert "secret-platform-id" not in serialized
    assert "must never enter replay evidence" not in serialized
    assert all(sample["real_provenance_ok"] is True for sample in report["samples"])
    assert all(sample["source_evidence_digest"] for sample in report["samples"])
    assert record is not None
    persisted = str(record.content["report"])
    assert "real-0" not in persisted
    assert "verified-real-production-replay" not in persisted


def test_forged_outcome_trace_without_terminal_event_chain_is_rejected(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_recall_target(runtime)
        forged_id = _append_forged_source(
            runtime,
            index=77,
            task_type="production.forged",
        )
        report = run_real_task_replay(
            runtime,
            _dataset([forged_id]),
            seed=False,
            persist_report=True,
        )
    finally:
        runtime.close()

    assert report["verified_real_sample_count"] == 0
    assert report["samples"][0]["real_provenance_reason"] == "terminal_evidence_invalid"


def test_manual_label_and_duplicate_source_cannot_close_real_replay_gate(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_recall_target(runtime)
        source_id = _append_real_source(runtime, index=0, task_type="production.task.0")
        manual = run_real_task_replay(
            runtime,
            {
                "name": "manual-label",
                "scope": asdict(SCOPE),
                "cases": [
                    {
                        "case_id": f"manual-{index}",
                        "query": "verified replay answer",
                        "expected_text": ["cobalt"],
                        **({"source_record_id": source_id} if index else {}),
                    }
                    for index in range(10)
                ],
            },
            seed=False,
            persist_report=True,
        )
        duplicate = run_real_task_replay(
            runtime,
            _dataset([source_id] * 10),
            seed=False,
            persist_report=True,
        )
        gate = build_verified_real_replay_summary(runtime, scope=SCOPE)
    finally:
        runtime.close()

    assert manual["verified_real_sample_count"] == 1
    assert duplicate["verified_real_sample_count"] == 1
    assert gate["ok"] is False
    assert gate["sample_deficit"] == 9
    assert gate["rejection_reasons"]["duplicate_source_record"] == 9


def test_distinct_trace_records_cannot_multiply_one_terminal_evidence_chain(
    tmp_path,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_recall_target(runtime)
        source_id = _append_real_source(
            runtime,
            index=0,
            task_type="production.task.0",
        )
        clones = [
            _clone_source_with_same_terminal_evidence(
                runtime,
                source_record_id=source_id,
                suffix=str(index),
            )
            for index in range(1, 10)
        ]
        report = run_real_task_replay(
            runtime,
            _dataset([source_id, *clones]),
            seed=False,
            persist_report=True,
        )
        gate = build_verified_real_replay_summary(runtime, scope=SCOPE)
    finally:
        runtime.close()

    assert report["verified_real_sample_count"] == 1
    assert {
        sample["real_provenance_reason"] for sample in report["samples"][1:]
    } == {"duplicate_terminal_evidence"}
    assert gate["ok"] is False
    assert gate["sample_count"] == 1
    assert gate["rejection_reasons"]["duplicate_terminal_evidence"] == 9


def test_untrusted_rehearsal_cross_scope_and_unsuccessful_sources_are_rejected(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    other_scope = ScopeRef(
        agent_id=SCOPE.agent_id,
        workspace_id="other",
        user_id=SCOPE.user_id,
    )
    try:
        _seed_recall_target(runtime)
        sources = [
            _append_forged_source(runtime, index=1, task_type="a", rehearsal=True),
            _append_forged_source(runtime, index=2, task_type="b", success=False),
            _append_forged_source(runtime, index=3, task_type="c", source="synthetic.fixture"),
            _append_forged_source(
                runtime,
                index=4,
                task_type="d",
                evidence_class="manually_asserted",
            ),
            _append_forged_source(runtime, index=5, task_type="e", scope=other_scope),
        ]
        report = run_real_task_replay(
            runtime,
            _dataset(sources),
            seed=False,
            persist_report=True,
        )
        gate = build_verified_real_replay_summary(runtime, scope=SCOPE)
    finally:
        runtime.close()

    assert report["verified_real_sample_count"] == 0
    reasons = {sample["real_provenance_reason"] for sample in report["samples"]}
    assert {
        "rehearsal_source",
        "unsuccessful_source",
        "untrusted_terminal_source",
        "unverified_evidence_class",
        "source_record_unavailable_in_scope",
    } <= reasons
    assert gate["ok"] is False


def test_case_scope_override_is_rejected_before_recall_or_persistence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    other_scope = ScopeRef(
        agent_id=SCOPE.agent_id,
        workspace_id="other",
        user_id=SCOPE.user_id,
    )
    try:
        source_id = _append_real_source(
            runtime,
            index=88,
            task_type="production.cross.scope",
            scope=other_scope,
        )
        report = run_real_task_replay(
            runtime,
            {
                "name": "cross-scope",
                "scope": asdict(SCOPE),
                "cases": [
                    {
                        "case_id": "platform-message-id",
                        "scope": asdict(other_scope),
                        "source_record_id": source_id,
                        "query": "must not execute",
                    }
                ],
            },
            seed=False,
            persist_report=True,
        )
        record = runtime.store.get_by_id(report["persisted_record_id"], scope=SCOPE)
    finally:
        runtime.close()

    assert report["samples"][0]["executed"] is False
    assert report["samples"][0]["real_provenance_reason"] == "case_scope_mismatch"
    assert report["samples"][0]["source_evidence_digest"] == ""
    assert record is not None
    assert "platform-message-id" not in str(record.content["report"])


def test_verified_real_replay_threshold_deficits_are_exact(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_recall_target(runtime)
        source_ids = [
            _append_real_source(
                runtime,
                index=index,
                task_type=f"production.task.{index % 4}",
            )
            for index in range(9)
        ]
        run_real_task_replay(
            runtime,
            _dataset(source_ids, failing={0, 1}),
            seed=False,
            persist_report=True,
        )
        gate = build_verified_real_replay_summary(runtime, scope=SCOPE)
    finally:
        runtime.close()

    assert gate["ok"] is False
    assert gate["sample_count"] == 9
    assert gate["sample_deficit"] == 1
    assert gate["distinct_task_types"] == 4
    assert gate["task_type_deficit"] == 1
    assert gate["pass_rate"] == 7 / 9
    assert gate["pass_rate_deficit"] == MIN_VERIFIED_REAL_REPLAY_PASS_RATE - (7 / 9)


def test_real_replay_gate_rejects_stale_package_digest(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_recall_target(runtime)
        report = run_real_task_replay(
            runtime,
            _dataset(_ten_sources(runtime)),
            seed=False,
            persist_report=True,
        )
        monkeypatch.setattr(
            "eimemory.governance.real_replay_gate.runtime_package_tree_digest",
            lambda: "f" * 64,
        )
        gate = build_verified_real_replay_summary(runtime, scope=SCOPE)
    finally:
        runtime.close()

    assert report["package_tree_digest"] != "f" * 64
    assert gate["ok"] is False
    assert gate["reason"] == "current_code_replay_missing"


def test_newer_other_digest_report_does_not_hide_current_code_report(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_recall_target(runtime)
        source_ids = _ten_sources(runtime)
        current = run_real_task_replay(
            runtime,
            _dataset(source_ids),
            seed=False,
            persist_report=True,
        )
        monkeypatch.setattr(
            "eimemory.evaluation.task_replay.runtime_package_tree_digest",
            lambda: "e" * 64,
        )
        run_real_task_replay(
            runtime,
            _dataset(source_ids),
            seed=False,
            persist_report=True,
        )
        gate = build_verified_real_replay_summary(runtime, scope=SCOPE)
    finally:
        runtime.close()

    assert current["package_tree_digest"] == runtime_package_tree_digest()
    assert gate["ok"] is True
    assert gate["record_id"] == current["persisted_record_id"]
