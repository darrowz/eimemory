from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.governance.capability_dashboard import VERIFIED_REAL_TASK_METHODS
from eimemory.governance.evidence_contract import current_release_identity
from eimemory.governance.tool_receipts import sign_tool_receipt, verify_tool_receipt
from eimemory.models.records import RecordEnvelope, ScopeRef


BASE_SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}
RECEIPT_KEY = "RuntimeReceiptEvidenceKey_0123456789-Strong"


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    instance = Runtime.create(root=tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def _seed_base_release(runtime: Runtime) -> None:
    scope_ref = ScopeRef.from_dict(BASE_SCOPE)
    commit = "d" * 40
    version = "1.9.77"
    runtime._test_runtime_commit = commit
    release_path = f"/opt/eimemory/releases/{commit}"
    receipt_payload = {
        "report_type": "deployment_receipt",
        "promotion_target": "code_patch",
        "action": "code_patch",
        "gate": {"ok": True, "receipt_verified": True},
        "side_effect": {
            "ok": True,
            "production_applied": True,
            "deployment_executed": True,
            "verification": {"ok": True, "skipped": False},
            "deployment": {"ok": True, "skipped": False, "release_path": release_path},
            "post_deploy_health": {
                "ok": True,
                "skipped": False,
                "commit": commit,
                "version": version,
                "release_path": release_path,
            },
            "commit": {"commit_sha": commit},
            "release": {"version": version, "release_path": release_path},
            "rollback_evidence": {
                "prior_commit_sha": "c" * 40,
                "rollback_command": "verified rollback",
            },
        },
    }
    runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Current deployment receipt",
            summary="verified",
            scope=scope_ref,
            source="eimemory.deployment_receipt",
            status="deployed",
            content=receipt_payload,
            meta={
                "report_type": "deployment_receipt",
                "commit_sha": commit,
                "version": version,
                "release_path": release_path,
                "gate_ok": True,
            },
        )
    )
    assert current_release_identity(runtime, scope_ref) is not None


def test_verified_codex_and_hermes_tasks_are_release_bound_per_channel(runtime: Runtime) -> None:
    _seed_base_release(runtime)
    service = AgentRuntimeMemoryService(runtime)

    codex = service.record_terminal(
        channel="codex",
        scope=BASE_SCOPE,
        end_kind="stop",
        session_id="codex-session",
        event_id="codex-turn",
        task_type="code.fix",
        success=True,
        verification="pytest:passed",
        result="regression test passed",
    )
    hermes = service.record_terminal(
        channel="hermes",
        scope=BASE_SCOPE,
        end_kind="task_end",
        session_id="hermes-session",
        event_id="hermes-task",
        task_type="research.audit",
        success=True,
        verification="source-check:passed",
        result="research audit passed",
    )

    codex_scope = resolve_channel_scope("codex", BASE_SCOPE)
    hermes_scope = resolve_channel_scope("hermes", BASE_SCOPE)
    codex_metrics = runtime.build_capability_dashboard_metrics(scope=codex_scope, persist=False)
    hermes_metrics = runtime.build_capability_dashboard_metrics(scope=hermes_scope, persist=False)
    openclaw_metrics = runtime.build_capability_dashboard_metrics(scope=BASE_SCOPE, persist=False)

    assert codex["outcome_trace"]["ok"] is True
    assert hermes["outcome_trace"]["ok"] is True
    assert codex_metrics["sample_counts"]["current_deployment_verified_real_tasks"] == 1
    assert hermes_metrics["sample_counts"]["current_deployment_verified_real_tasks"] == 1
    assert codex_metrics["metrics"]["current_deployment_verified_real_task_success_rate"] == 1.0
    assert hermes_metrics["metrics"]["current_deployment_verified_real_task_success_rate"] == 1.0
    assert openclaw_metrics["sample_counts"]["current_deployment_verified_real_tasks"] == 0
    assert "openclaw.agent_end" in VERIFIED_REAL_TASK_METHODS
    assert "openclaw.task_end" in VERIFIED_REAL_TASK_METHODS
    assert "codex.stop" in VERIFIED_REAL_TASK_METHODS
    assert "hermes.task_end" in VERIFIED_REAL_TASK_METHODS


def test_unverified_codex_success_cannot_enter_verified_real_task_count(runtime: Runtime) -> None:
    _seed_base_release(runtime)
    service = AgentRuntimeMemoryService(runtime)

    result = service.record_terminal(
        channel="codex",
        scope=BASE_SCOPE,
        end_kind="stop",
        session_id="codex-unverified-session",
        event_id="codex-unverified-turn",
        task_type="code.fix",
        success=True,
        verification="",
        result="claimed complete",
    )
    metrics = runtime.build_capability_dashboard_metrics(
        scope=resolve_channel_scope("codex", BASE_SCOPE),
        persist=False,
    )

    assert result["outcome"]["outcome"] == "verification_missing"
    assert metrics["sample_counts"]["verified_real_tasks"] == 0
    assert metrics["sample_counts"]["current_deployment_verified_real_tasks"] == 0


def test_tool_receipt_signature_preserves_runtime_source(monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", RECEIPT_KEY)
    receipt = sign_tool_receipt(
        {
            "source": "codex.post_tool_use",
            "tool_name": "exec_command",
            "tool_call_id": "call-1",
            "duration_ms": 12,
            "passed": True,
            "result_digest": "a" * 64,
            "session_id": "codex-session",
            "run_id": "codex-turn",
        }
    )

    assert receipt["source"] == "codex.post_tool_use"
    assert verify_tool_receipt(
        receipt,
        session_id="codex-session",
        run_id="codex-turn",
    ) is True
