from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.learning.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import RecordEnvelope, ScopeRef


SUPERVISOR_COMMANDS = ("nightly",)
LEGACY_SUPERVISOR_COMMANDS = ("learn-think", "learn-watch")
SUMMARY_DEFAULTS = {
    "last_success_at": "",
    "last_error_at": "",
    "duration_ms": 0,
    "memory_peak": 0,
    "produced_count": 0,
    "promoted_count": 0,
    "rolled_back_count": 0,
}
RELEASE_FIELDS = ("release_commit", "release_version", "deployment_receipt_id", "release_session_id")
NIGHTLY_COMMAND_SCHEMA = "nightly_command_outcome.v1"
NIGHTLY_COMMAND_SOURCE = "eimemory.scheduler.nightly_command"
_IDENTITY_OUTCOMES = frozenset({
    "applied", "fresh", "ingest_stamped", "no_longer_eligible", "source_missing", "source_ambiguous",
    "source_identity_changed", "source_version_unknown", "archived_payload_unsupported",
    "source_projection_or_digest_mismatch", "source_storage_key_mismatch", "source_version_changed",
    "source_scope_not_admitted", "scope_move_unsupported", "target_conflict",
    "identity_transition_forbidden", "source_timestamp_unknown", "transaction_failed", "other",
    "retained_both_current_canonical", "retained_current_original", "identity_decision_unresolved",
})
_IDENTITY_MISMATCH_FIELDS = frozenset({
    "hydration", "exact_ref", "kind", "status", "source", "title", "summary", "detail",
    "created_at", "updated_at", "payload_roundtrip_digest", "meta_json", "payload_json",
    "payload_stored_digest", "diagnostic_unavailable", "other",
})
_IDENTITY_ROUNDTRIP_FIELDS = frozenset(RecordEnvelope.__dataclass_fields__) | {"unknown_fields", "diagnostic_unavailable"}
_IDENTITY_NORMALIZATIONS = frozenset({
    "missing_source_id", "missing_aliases", "missing_aliases_version", "source_partition_overlay",
})


def _fixed_counts(value: Any, allowed: frozenset[str], *, unknown: str | None = "other") -> dict[str, int]:
    counts: dict[str, int] = {}
    for key, count in value.items() if isinstance(value, dict) else ():
        if type(count) is not int or not 0 <= count < 2 ** 63:
            continue
        name = key if isinstance(key, str) and key in allowed else unknown
        if name is None:
            continue
        if isinstance(key, str) and key.startswith("transaction_failed:") and "transaction_failed" in allowed:
            name = "transaction_failed"
        counts[name] = min(2 ** 63 - 1, counts.get(name, 0) + count)
    return counts


def _identity_command_summary(report: dict[str, Any], *, raised: bool = False) -> dict[str, Any]:
    return {
        "ok": report.get("ok") is True and not raised,
        "exception_raised": bool(raised),
        **{key: report[key] for key in (
            "candidate_count", "eligible_in_place_count", "repaired_count", "blocked_count", "skipped_fresh_count",
            "retained_pair_count",
        ) if type(report.get(key)) is int and 0 <= report[key] < 2 ** 63},
        "outcome_counts": _fixed_counts(report.get("outcome_counts"), _IDENTITY_OUTCOMES),
        "mismatch_field_counts": _fixed_counts(report.get("mismatch_field_counts"), _IDENTITY_MISMATCH_FIELDS),
        **{key: _fixed_counts(report.get(key), _IDENTITY_ROUNDTRIP_FIELDS, unknown="unknown_fields")
           for key in ("roundtrip_field_counts", "roundtrip_missing_field_counts")},
        "legacy_normalization_counts": _fixed_counts(report.get("legacy_normalization_counts"),
                                                       _IDENTITY_NORMALIZATIONS, unknown=None),
    }


def _same_command_run(outcome: dict[str, Any], supervisor: dict[str, Any]) -> bool:
    return bool(
        supervisor.get("run_id") and supervisor.get("record_id")
        and outcome.get("run_id") == supervisor["run_id"]
        and outcome.get("supervisor_record_id") == supervisor["record_id"]
        and all(outcome.get(key, "") == supervisor.get(key, "") for key in RELEASE_FIELDS)
        and type(outcome.get("scheduler_execution_ok")) is bool
        and outcome["scheduler_execution_ok"] == (supervisor.get("nightly_diagnostics") or {}).get("execution_ok")
        and outcome.get("scheduler_ok") is bool(supervisor.get("ok"))
        and outcome.get("step_evidence", {}) == _step_evidence(supervisor)
    )


def _step_evidence(supervisor: dict[str, Any]) -> dict[str, Any]:
    diagnostics = supervisor.get("nightly_diagnostics") or {}
    refs = {}
    if "code_maintenance_evidence" in diagnostics:
        refs["code_evolution"] = diagnostics["code_maintenance_evidence"]
    native = diagnostics.get("native_l5_assessment")
    if isinstance(native, dict) and "failure_evidence" in native:
        refs["native_l5_assessment_failure"] = native["failure_evidence"]
    return refs


def _verify_step_evidence(runtime: Any, *, scope: ScopeRef, supervisor: dict[str, Any],
                          expected_release: dict[str, Any] | None = None) -> None:
    refs = _step_evidence(supervisor)
    if not refs:
        return  # Historical summaries did not archive step observations.
    from eimemory.contracts.nightly_step_evidence import step_evidence_diagnostics
    from eimemory.governance.learning.nightly_step_evidence import read_nightly_step_evidence
    if expected_release is None:
        from eimemory.governance.release.evidence_contract import current_release_identity, release_identity_payload
        release = current_release_identity(runtime, scope)
        expected_release = release_identity_payload(release) if release is not None else {}
    for step, reference in refs.items():
        checked = step_evidence_diagnostics(reference)
        if checked.get("archive_verified") is not True:
            if supervisor.get("ok") is True:
                raise ValueError("nightly_success_step_evidence_unverified")
            continue  # A recorded archive failure is still a genuine failed ending.
        if checked != reference:
            raise ValueError("nightly_step_evidence_projection_changed")
        read_nightly_step_evidence(runtime, scope=scope, run_id=supervisor.get("run_id", ""),
                                   step=step, reference=reference, expected_release=expected_release)


def _valid_command_result(outcome: dict[str, Any]) -> bool:
    if any(type(outcome.get(key)) is not bool for key in (
            "ok", "scheduler_ok", "scheduler_execution_ok", "identity_repair_ok")):
        return False
    identity = outcome.get("identity_repair")
    if not isinstance(identity, dict) or any(type(identity.get(key)) is not bool
                                            for key in ("ok", "exception_raised")):
        return False
    scheduler_ok = outcome["scheduler_ok"] and outcome["scheduler_execution_ok"]
    ok = scheduler_ok and outcome["identity_repair_ok"]
    phase = "scheduler" if not scheduler_ok else "identity_repair" if not outcome["identity_repair_ok"] else ""
    return (outcome["identity_repair_ok"] == identity["ok"]
            and not (identity.get("exception_raised") and identity["ok"])
            and outcome["ok"] == ok and type(outcome.get("exit_code")) is int
            and outcome["exit_code"] == (0 if ok else 1) and outcome.get("first_failed_phase") == phase)


def _command_outcome_payload(record: RecordEnvelope) -> dict[str, Any]:
    content = record.content
    return {
        "schema": NIGHTLY_COMMAND_SCHEMA, "status": "recorded", "record_id": record.record_id,
        **{key: content[key] for key in (
            "run_id", "supervisor_record_id", "completed_at", "ok", "exit_code", "scheduler_ok",
            "scheduler_execution_ok", "identity_repair_ok", "first_failed_phase",
        ) if key in content},
        **{key: content[key] for key in RELEASE_FIELDS if key in content},
        **({"step_evidence": content["step_evidence"]} if "step_evidence" in content else {}),
        "identity_repair": _identity_command_summary(
            content.get("identity_repair") or {},
            raised=(content.get("identity_repair") or {}).get("exception_raised") is True,
        ),
    }


def persist_nightly_command_outcome(
    runtime: Any, *, scope: dict[str, Any] | ScopeRef, supervisor: dict[str, Any],
    identity_repair: dict[str, Any], identity_exception: bool = False,
) -> dict[str, Any]:
    """Append the CLI ending without rewriting a newer scheduler run's row."""
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    if not all(isinstance(supervisor.get(key), str) and supervisor[key] for key in ("run_id", "record_id")):
        raise ValueError("nightly_command_scheduler_identity_missing")
    diagnostics = supervisor.get("nightly_diagnostics")
    if not isinstance(diagnostics, dict) or type(diagnostics.get("execution_ok")) is not bool:
        raise ValueError("nightly_command_scheduler_result_missing")
    scheduled = runtime.store.get_by_id(supervisor["record_id"], scope=scope_ref, exact_scope=True)
    if (scheduled is None or scheduled.scope != scope_ref or scheduled.kind != "reflection"
            or scheduled.source != "eimemory.autonomous_learning" or not _is_supervisor_record(scheduled)
            or scheduled.content.get("command") != "nightly"
            or any((scheduled.meta or {}).get(key, "") != supervisor.get(key, "") for key in RELEASE_FIELDS)):
        raise ValueError("nightly_command_scheduler_binding_mismatch")
    if scheduled.content.get("run_id") != supervisor["run_id"]:
        raise ValueError("nightly_command_scheduler_run_changed")
    if (type(supervisor.get("ok")) is not bool or scheduled.content.get("ok") is not supervisor["ok"]
            or scheduled.content.get("nightly_diagnostics") != diagnostics):
        raise ValueError("nightly_command_scheduler_result_changed")
    _verify_step_evidence(runtime, scope=scope_ref, supervisor=supervisor,
                          expected_release={key: scheduled.meta.get(key, "") for key in RELEASE_FIELDS})
    identity = _identity_command_summary(identity_repair, raised=identity_exception)
    scheduler_ok = supervisor.get("ok") is True
    ok = scheduler_ok and diagnostics["execution_ok"] and identity["ok"]
    content = {
        "report_type": NIGHTLY_COMMAND_SCHEMA, "schema": NIGHTLY_COMMAND_SCHEMA,
        "run_id": supervisor["run_id"], "supervisor_record_id": supervisor["record_id"],
        "completed_at": now_iso(), "ok": ok, "exit_code": 0 if ok else 1,
        "scheduler_ok": scheduler_ok, "scheduler_execution_ok": diagnostics["execution_ok"],
        "identity_repair_ok": identity["ok"], "identity_repair": identity,
        "first_failed_phase": "scheduler" if not scheduler_ok or not diagnostics["execution_ok"]
            else "identity_repair" if not identity["ok"] else "",
        **({"step_evidence": _step_evidence(supervisor)} if _step_evidence(supervisor) else {}),
    }
    record = append_learning_record_once(
        runtime, kind="reflection", title="Nightly command outcome",
        summary=f"nightly command ok={ok} exit_code={content['exit_code']}", scope=scope_ref,
        loop_id="supervisor", step_name="nightly_command_outcome",
        semantic_key=stable_semantic_key(NIGHTLY_COMMAND_SCHEMA, supervisor["run_id"], supervisor["record_id"]),
        authority_tier="L0", status="archived", source=NIGHTLY_COMMAND_SOURCE,
        content=content, meta={"report_type": NIGHTLY_COMMAND_SCHEMA, "nightly_run_id": supervisor["run_id"],
                               "supervisor_record_id": supervisor["record_id"]},
        evidence=[supervisor["record_id"]],
    )
    if (not _same_command_run(record.content, supervisor)
            or any(record.content.get(key) != content[key] for key in (
                "ok", "exit_code", "scheduler_ok", "scheduler_execution_ok", "identity_repair_ok",
                "identity_repair", "first_failed_phase",
            ))):
        raise ValueError("nightly_command_release_or_run_mismatch")
    return _command_outcome_payload(record)


def read_nightly_command_outcome(
    runtime: Any, *, scope: ScopeRef, supervisor: dict[str, Any],
) -> dict[str, Any]:
    """Only this exact scheduler execution can acquire a recorded CLI ending."""
    unavailable = {"schema": NIGHTLY_COMMAND_SCHEMA, "status": "not_recorded"}
    lookup = getattr(runtime.store, "list_records_by_meta_value", None)
    if not supervisor.get("run_id") or not supervisor.get("record_id") or not callable(lookup):
        return unavailable
    try:
        records = lookup(kinds=["reflection"], scope=scope, meta_key="nightly_run_id",
                         meta_value=supervisor["run_id"], limit=3)
        if records is None or len(records) > 2:
            return unavailable
        matched = [record for record in records
                   if record.scope == scope and record.source == NIGHTLY_COMMAND_SOURCE and record.status == "archived"
                   and record.content.get("schema") == record.content.get("report_type") == NIGHTLY_COMMAND_SCHEMA
                   and all(record.content.get(key, "") == (record.meta or {}).get(key, "") for key in RELEASE_FIELDS)
                   and _valid_command_result(record.content)
                   and _same_command_run(record.content, supervisor)]
        if len(matched) != 1:
            return unavailable
        _verify_step_evidence(runtime, scope=scope, supervisor=supervisor,
                              expected_release={key: matched[0].meta.get(key, "") for key in RELEASE_FIELDS})
        return _command_outcome_payload(matched[0])
    except Exception:
        return unavailable


def supervisor_summary(
    *,
    command: str,
    ok: bool,
    duration_ms: int,
    memory_peak: int,
    produced_count: int = 0,
    promoted_count: int = 0,
    rolled_back_count: int = 0,
    error: str = "",
    run_id: str = "",
    started_at: str = "",
) -> dict[str, Any]:
    now = now_iso()
    return {
        "command": str(command),
        **SUMMARY_DEFAULTS,
        "last_success_at": now if ok else "",
        "last_error_at": "" if ok else now,
        "duration_ms": max(0, int(duration_ms or 0)),
        "memory_peak": max(0, int(memory_peak or 0)),
        "produced_count": max(0, int(produced_count or 0)),
        "promoted_count": max(0, int(promoted_count or 0)),
        "rolled_back_count": max(0, int(rolled_back_count or 0)),
        "ok": bool(ok),
        "error": str(error or ""),
        **({"run_id": str(run_id), "started_at": str(started_at)} if run_id else {}),
    }


def persist_supervisor_summary(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    summary: dict[str, Any],
) -> RecordEnvelope:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    command = str(summary.get("command") or "unknown")
    if command == "nightly":
        _verify_step_evidence(runtime, scope=scope_ref, supervisor=summary)
    record = append_learning_record_once(
        runtime,
        kind="reflection",
        title=f"Supervisor run: {command}",
        summary=f"{command} ok={bool(summary.get('ok'))} produced={int(summary.get('produced_count') or 0)}",
        scope=scope_ref,
        loop_id="supervisor",
        step_name="supervisor_run",
        semantic_key=stable_semantic_key("supervisor_run", command, scope_ref),
        authority_tier="L0",
        status="active" if summary.get("ok") else "failed",
        content={"report_type": "supervisor_run", **dict(summary)},
        meta={"report_type": "supervisor_run", "command": command, "ok": bool(summary.get("ok"))},
    )
    record.status = "active" if summary.get("ok") else "failed"
    # append_learning_record_once binds these fields to the verified runtime
    # receipt. Keep that binding when updating the reusable supervisor row;
    # caller-supplied labels cannot replace or manufacture release authority.
    bound_release = {key: record.meta[key] for key in RELEASE_FIELDS if key in record.meta}
    record.content = {
        "report_type": "supervisor_run",
        **{key: value for key, value in summary.items() if key not in RELEASE_FIELDS},
        **bound_release,
    }
    record.meta = {**dict(record.meta or {}), "report_type": "supervisor_run", "command": command, "ok": bool(summary.get("ok"))}
    record.touch()
    return runtime.store.rewrite(record)


def build_supervisor_contract(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    commands: tuple[str, ...] = SUPERVISOR_COMMANDS,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    latest = _latest_supervisor_records(runtime, scope=scope_ref, commands=commands)
    runs = {command: _summary_from_record(latest.get(command), command=command) for command in commands}
    if "nightly" in runs:
        runs["nightly"]["command_execution"] = read_nightly_command_outcome(
            runtime, scope=scope_ref, supervisor=runs["nightly"],
        )
    if not latest:
        status = "unknown"
    elif any(item.get("last_error_at") and not item.get("last_success_at") for item in runs.values()):
        status = "degraded"
    elif any(_is_stuck(item) for item in runs.values() if item.get("last_success_at")):
        status = "stuck"
    elif all(item.get("last_success_at") for item in runs.values()):
        status = "healthy"
    else:
        status = "degraded"
    scheduler_status = status
    if "nightly" in runs and latest.get("nightly") is not None:
        command = runs["nightly"]["command_execution"]
        if command.get("status") == "recorded" and command.get("ok") is False:
            status = "degraded"
        elif command.get("status") != "recorded" and status == "healthy":
            status = "unknown"
    return {"status": status, "scheduler_status": scheduler_status, "runs": runs}


def _latest_supervisor_records(
    runtime: Any,
    *,
    scope: ScopeRef,
    commands: tuple[str, ...],
) -> dict[str, RecordEnvelope]:
    latest: dict[str, RecordEnvelope] = {}
    wanted = {str(command) for command in commands if str(command)}
    list_by_meta = getattr(runtime.store, "list_records_by_meta_value", None)
    if callable(list_by_meta):
        for command in wanted:
            records = list_by_meta(
                kinds=["reflection"],
                scope=scope,
                meta_key="command",
                meta_value=command,
                limit=1,
            )
            if records and _is_supervisor_record(records[0]):
                latest[command] = records[0]

    missing = wanted - latest.keys()
    page_size = 200
    offset = 0
    while missing:
        page = runtime.store.list_records(kinds=["reflection"], scope=scope, limit=page_size, offset=offset)
        for record in page:
            if not _is_supervisor_record(record):
                continue
            meta = record.meta if isinstance(record.meta, dict) else {}
            content = record.content if isinstance(record.content, dict) else {}
            command = str(meta.get("command") or content.get("command") or "")
            if command in missing:
                latest[command] = record
                missing.remove(command)
        if not missing or len(page) < page_size:
            break
        offset += page_size
    return latest


def _is_supervisor_record(record: RecordEnvelope) -> bool:
    meta = record.meta if isinstance(record.meta, dict) else {}
    content = record.content if isinstance(record.content, dict) else {}
    return str(meta.get("report_type") or content.get("report_type") or "") == "supervisor_run"


def _summary_from_record(record: RecordEnvelope | None, *, command: str) -> dict[str, Any]:
    if record is None:
        return {"command": command, **SUMMARY_DEFAULTS, "ok": False, "error": "no_run_record"}
    content = record.content if isinstance(record.content, dict) else {}
    raw_meta = getattr(record, "meta", None)
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    record_id = getattr(record, "record_id", None)
    return {
        "command": command,
        **SUMMARY_DEFAULTS,
        **{key: content.get(key, default) for key, default in SUMMARY_DEFAULTS.items()},
        "ok": bool(content.get("ok")),
        "error": str(content.get("error") or ""),
        **({"record_id": record_id} if isinstance(record_id, str) and record_id else {}),
        **{key: content[key] for key in ("run_id", "started_at") if key in content},
        **{key: content.get(key) or meta.get(key)
           for key in RELEASE_FIELDS if content.get(key) or meta.get(key)},
        # Stored by the same nightly run; preserve the first failure boundary
        # instead of reducing the operational view to a lone false flag.
        "nightly_diagnostics": (
            dict(content["nightly_diagnostics"])
            if isinstance(content.get("nightly_diagnostics"), dict)
            else {}
        ),
    }


def _is_stuck(summary: dict[str, Any]) -> bool:
    raw = str(summary.get("last_success_at") or "")
    if not raw:
        return False
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    age_seconds = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
    return age_seconds > 3 * 24 * 3600
