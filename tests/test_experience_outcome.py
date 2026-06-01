from eimemory.api.runtime import Runtime
from eimemory.experience import record_outcome_trace
from eimemory.experience.diagnosis import diagnose_outcome
from eimemory.experience.sanitize import sanitize_outcome_payload
from eimemory.models.records import RecordEnvelope, ScopeRef


def _payload(**overrides) -> dict:
    payload = {
        "trace_id": "trace-001",
        "idempotency_key": "idem-001",
        "task_type": "repo.edit",
        "input_summary": "Implement outcome tracing",
        "expected_tool": "functions.apply_patch",
        "selected_tools": ["functions.apply_patch", "functions.shell_command"],
        "actions": [{"type": "tool_call", "tool": "functions.apply_patch"}],
        "outcome": {"status": "success"},
        "verifier": {"passed": True},
        "feedback": {"summary": "landed"},
    }
    payload.update(overrides)
    return payload


def test_record_outcome_trace_writes_reflection_with_diagnosis_and_schema(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"tenant_id": "tenant-a", "agent_id": "eibrain", "workspace_id": "repo"}

    result = record_outcome_trace(runtime, _payload(), scope=scope)

    assert result["ok"] is True
    record = runtime.store.get_by_id(result["record_id"], scope=scope)
    assert record is not None
    assert record.kind == "reflection"
    assert record.source == "eimemory.experience.outcome_trace"
    assert record.provenance["report_type"] == "outcome_trace"
    assert record.provenance["schema_version"] == "outcome_trace.v1"
    assert record.meta["report_type"] == "outcome_trace"
    assert record.meta["schema_version"] == "outcome_trace.v1"
    assert record.meta["trace_id"] == "trace-001"
    assert record.meta["idempotency_key"] == "idem-001"
    assert record.meta["primary_label"] == "success"
    assert record.meta["diagnosis_signals"] == []
    assert record.meta["risk_level"] == "low"
    assert record.content["diagnosis"]["primary_label"] == "success"
    assert record.content["payload"]["trace_id"] == "trace-001"


def test_record_outcome_trace_is_idempotent_by_key_and_trace_id_within_scope(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "repo"}

    first = record_outcome_trace(runtime, _payload(trace_id="trace-a", idempotency_key="idem-a"), scope=scope)
    by_key = record_outcome_trace(runtime, _payload(trace_id="trace-b", idempotency_key="idem-a"), scope=scope)
    by_trace = record_outcome_trace(runtime, _payload(trace_id="trace-a", idempotency_key="idem-b"), scope=scope)
    other_scope = record_outcome_trace(
        runtime,
        _payload(trace_id="trace-a", idempotency_key="idem-a"),
        scope={"agent_id": "eibrain", "workspace_id": "other"},
    )

    assert first["ok"] is True
    assert by_key["record_id"] == first["record_id"]
    assert by_trace["record_id"] == first["record_id"]
    assert other_scope["record_id"] != first["record_id"]
    assert len(runtime.store.list_records(kinds=["reflection"], scope=scope, limit=10)) == 1


def test_record_outcome_trace_idempotency_scans_past_first_reflection_page(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "eibrain", "workspace_id": "repo"}
    first = record_outcome_trace(runtime, _payload(trace_id="trace-old", idempotency_key="idem-old"), scope=scope)
    scope_ref = ScopeRef.from_dict(scope)
    for index in range(1005):
        runtime.store.append(
            RecordEnvelope.create(
                kind="reflection",
                title=f"Newer non-outcome reflection {index}",
                summary="noise",
                scope=scope_ref,
            )
        )

    duplicate = record_outcome_trace(
        runtime,
        _payload(trace_id="trace-old", idempotency_key="idem-old", input_summary="different"),
        scope=scope,
    )

    assert duplicate["idempotent"] is True
    assert duplicate["record_id"] == first["record_id"]


def test_diagnosis_uses_priority_and_keeps_signals_out_of_primary_label() -> None:
    diagnosis = diagnose_outcome(
        _payload(
            outcome={"status": "failure"},
            safety={"unsafe_or_high_risk": True},
            stale_context=True,
            argument_mismatch=True,
            visual_evidence={"missing": True},
            operator_gap={"detected": True},
            verifier={"passed": False},
        )
    )

    assert diagnosis["primary_label"] == "unsafe_or_high_risk"
    assert set(diagnosis["signals"]) == {"missing_visual_evidence", "operator_gap", "verifier_missing"}
    assert "missing_visual_evidence" not in diagnosis["primary_label"]
    assert "unsafe_or_high_risk" in diagnosis["labels"]
    assert diagnosis["confidence"] > 0.0


def test_missing_tool_call_considers_expected_tool_absent_and_reply_only_actions() -> None:
    absent = diagnose_outcome(
        _payload(
            outcome={"status": "failure"},
            expected_tool="web.run",
            selected_tools=["functions.shell_command"],
            actions=[{"type": "tool_call", "tool": "functions.shell_command"}],
        )
    )
    reply_only = diagnose_outcome(
        _payload(
            outcome={"status": "failure"},
            expected_tool="functions.apply_patch",
            selected_tools=["functions.apply_patch"],
            actions=[{"type": "reply", "content": "I will do it"}],
        )
    )

    assert absent["primary_label"] == "missing_tool_call"
    assert reply_only["primary_label"] == "missing_tool_call"


def test_success_requires_success_status_and_verifier_not_false() -> None:
    assert diagnose_outcome(_payload(outcome={"status": "success"}, verifier={"passed": True}))["primary_label"] == "success"
    assert diagnose_outcome(_payload(outcome={"status": "success"}, verifier={}))["primary_label"] == "success"
    assert (
        diagnose_outcome(_payload(outcome={"status": "success"}, verifier={"passed": False}))["primary_label"]
        == "unknown_failure"
    )


def test_sanitize_preserves_allowed_context_fields() -> None:
    sanitized = sanitize_outcome_payload(
        _payload(
            world_state={"door": "open"},
            visual_evidence={"observations": ["saw settings dialog"]},
            operator_gap={"expected": "clicked save", "actual": "only replied"},
            policy_attribution={"policy": "verify before completion"},
        )
    )

    assert sanitized["world_state"] == {"door": "open"}
    assert sanitized["visual_evidence"] == {"observations": ["saw settings dialog"]}
    assert sanitized["operator_gap"]["expected"] == "clicked save"
    assert sanitized["policy_attribution"]["policy"] == "verify before completion"


def test_record_outcome_trace_accepts_trace_context_and_hoists_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    payload = _payload(
        trace_id=None,
        idempotency_key=None,
        trace_context={"trace_id": "trace-context-1", "idempotency_key": "idem-context-1"},
        outcome="bad",
        visual_evidence={"required": True, "available": False},
        operator_gap={"missing_confirmation": True},
        world_state={"expected": "dashboard open", "observed": "not opened"},
        feedback="不是这个意思",
        risk={"level": "L1"},
    )

    result = record_outcome_trace(runtime, payload, scope={"agent_id": "eibrain"})

    assert result["ok"] is True
    record = runtime.store.get_by_id(result["record_id"], scope={"agent_id": "eibrain"})
    assert record.meta["trace_id"] == "trace-context-1"
    assert record.meta["idempotency_key"] == "idem-context-1"
    assert record.meta["primary_label"] == "state_tracking_error"
    assert record.meta["risk_level"] == "L1"
    assert record.content["visual_evidence"]["available"] is False
    assert set(record.meta["diagnosis_signals"]) == {
        "missing_visual_evidence",
        "operator_gap",
        "world_state_mismatch",
    }


def test_sanitize_rejects_raw_images_sensitive_data_credential_urls_and_large_values(tmp_path) -> None:
    unsafe_payloads = [
        _payload(raw_image_stored=True),
        _payload(visual_evidence={"image": "data:image/png;base64,abc"}),
        _payload(world_state={"blob": "A" * 5000}),
        _payload(world_state={"secret": "open-sesame"}),
        _payload(world_state={"note": "authorization: Bearer abc123"}),
        _payload(world_state={"callback_url": "https://user:pass@example.com/callback"}),
        _payload(world_state={"items": list(range(101))}),
        _payload(world_state={"a": {"b": {"c": {"d": {"e": {"f": "too deep"}}}}}}),
    ]

    for index, payload in enumerate(unsafe_payloads):
        runtime = Runtime.create(root=tmp_path / str(index))
        result = record_outcome_trace(runtime, payload, scope={"agent_id": "x"})
        assert result["ok"] is False
        assert "unsafe payload" in result["error"]
        runtime.close()


def test_invalid_payload_returns_error_and_does_not_write(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    result = record_outcome_trace(runtime, {"trace_id": ""}, scope={"agent_id": "eibrain"})

    assert result["ok"] is False
    assert result["error"]
    assert runtime.store.list_records(kinds=["reflection"], scope={"agent_id": "eibrain"}, limit=10) == []
