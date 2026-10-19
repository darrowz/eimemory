"""Detect and persist a stale-policy failure in the system repair router."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any
from uuid import uuid4

from eimemory.core.clock import now_iso
from eimemory.contracts.code_incident_provenance import native_detection_provenance
from eimemory.governance.code_automation_policy import (
    CODE_AUTOMATION_POLICY_DEFAULT_PATH,
    load_code_automation_policy,
)
from eimemory.governance.evidence_contract import current_release_identity
from eimemory.models.records import RecordEnvelope, ScopeRef


DETECTOR_ID = "eimemory.system_code_repair_failure.v1"
INCIDENT_CLASS = "code.system_repair_policy_stale"
SOURCE = "eimemory.system_code_repair_failure"
CURRENT_SOURCE = SOURCE + ".current"
CURRENT_SCHEMA = "system_code_repair_current.v1"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def detect_system_code_repair_failure(
    repair_report: Mapping[str, Any],
    *,
    policy: Mapping[str, Any],
    release_commit: str,
    detected_at: str,
) -> dict[str, Any]:
    """Turn an observed stale consumed policy into one bounded incident."""
    source_facts = native_detection_provenance(DETECTOR_ID)

    current_commit = str(release_commit or "").strip().lower()
    repository = policy.get("repository") if isinstance(policy.get("repository"), Mapping) else {}
    policy_base_commit = str(repository.get("base_commit") or "").strip().lower()
    policy_digest = str(policy.get("policy_digest") or "").strip().lower()
    policy_transaction_id = str(repair_report.get("policy_transaction_id") or "").strip()
    consumed_block = bool(
        repair_report.get("ok") is False
        and repair_report.get("status") == "blocked"
        and repair_report.get("reason") == "automation_policy_already_consumed"
        and policy_transaction_id
    )
    actionable = bool(
        consumed_block
        and policy.get("ok") is True
        and policy.get("status") == "enabled"
        and _COMMIT_RE.fullmatch(current_commit)
        and _COMMIT_RE.fullmatch(policy_base_commit)
        and policy_base_commit != current_commit
        and _DIGEST_RE.fullmatch(policy_digest)
    )
    blocked_reason = str(repair_report.get("reason") or "").strip()
    observation = {
        "schema": "system_code_repair_failure.v1",
        "detector": DETECTOR_ID,
        "detected_at": str(detected_at or ""),
        "release_commit": current_commit,
        "blocked_reason": blocked_reason,
        "consumed_policy_digest": policy_digest,
        "consumed_policy_base_commit": policy_base_commit,
        "policy_transaction_id": policy_transaction_id,
        'source_facts_digest': source_facts['digest'],
    }
    incident: dict[str, Any] | None = None
    if actionable:
        identity = {key: value for key, value in observation.items() if key != "detected_at"}
        digest = _digest(identity)
        incident = {
            "incident_id": f"incident-system-repair-policy-{digest[:24]}",
            "incident_digest": digest,
            "incident_class": INCIDENT_CLASS,
            "title": "Consumed stale policy blocks current system repair routing",
            "summary": (
                f"The current release {current_commit} observed that the consumed one-shot policy "
                f"for older base {policy_base_commit} stops the autonomous repair router before it "
                "can establish whether any current incident matches that policy. Make stale policy "
                "state idle when it authorizes no current trusted incident, without reusing or "
                "resetting the consumed grant."
            ),
            "diagnostic_codes": ["system_code_repair:automation_policy_already_consumed"],
            "acceptance_requirements": [
                "consumed_policy_is_never_reused_or_reset",
                "policy_consumption_checked_only_for_an_exact_current_incident",
                "stale_policy_without_matching_current_incident_returns_idle",
                "provider_not_called_without_exact_unconsumed_policy",
                "protected_routing_regression_plan_passes",
            ],
        }
    return {
        **observation,
        "ok": not actionable,
        "status": "failure_detected" if actionable else "non_actionable",
        "origin": "system_detector",
        **source_facts['facts'], 'source_facts': source_facts,
        "observation_valid": True,
        "incident": incident,
        "router_observation": {key: repair_report.get(key) for key in
                               ("ok", "status", "reason", "policy_transaction_id")},
        "policy_observation": {"ok": policy.get("ok"), "status": policy.get("status"),
                               "policy_digest": policy_digest,
                               "repository": {"base_commit": policy_base_commit}},
    }


def validate_system_code_repair_failure_report(report: Any) -> bool:
    """Recompute the native observation; ordinary source labels are insufficient."""
    if not isinstance(report, Mapping):
        return False
    try:
        _instant(report["detected_at"])
        expected = detect_system_code_repair_failure(
            report["router_observation"], policy=report["policy_observation"],
            release_commit=report["release_commit"], detected_at=report["detected_at"])
        return all(report.get(key) == value for key, value in expected.items())
    except (KeyError, TypeError, ValueError):
        return False


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("repair_observation_time_invalid")
    return parsed


def _current_id(scope: ScopeRef, commit: str) -> str:
    if not _COMMIT_RE.fullmatch(commit):
        raise ValueError("repair_observation_release_invalid")
    return "system-repair-current-" + _digest({"schema": CURRENT_SCHEMA, "scope": asdict(scope), "commit": commit})


def _read_current(sqlite: Any, scope: ScopeRef, commit: str) -> RecordEnvelope | None:
    rows = sqlite.list_records_by_sources_exact_scope(scope=scope, kinds=["reflection"],
        sources=[CURRENT_SOURCE], record_ids=[_current_id(scope, commit)], limit=2)
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("repair_current_observation_conflict")
    record = rows[0]
    body = record.content
    if (record.scope != scope or record.source != CURRENT_SOURCE or record.status != "active"
            or record.source_id != "default" or body.get("schema") != CURRENT_SCHEMA
            or body.get("scope") != asdict(scope) or body.get("release_commit") != commit
            or not isinstance(body.get("observation_id"), str) or not body["observation_id"]
            or body.get("state") not in {"in_flight", "complete"}
            or body.get("digest") != _digest({k: v for k, v in body.items() if k != "digest"})):
        raise ValueError("repair_current_observation_unverified")
    _instant(body["started_at"])
    if body["state"] == "complete" and not validate_system_code_repair_failure_report(body.get("report")):
        raise ValueError("repair_current_report_unverified")
    return record


def begin_system_code_repair_observation(runtime: Any, *, scope: ScopeRef, detected_at: str) -> str:
    """Declare a new real router invocation before it runs; an old finisher cannot win."""
    release = current_release_identity(runtime, scope)
    if release is None:
        raise ValueError("repair_current_release_unavailable")
    _instant(detected_at)
    token = uuid4().hex
    def persist(sqlite):
        previous = _read_current(sqlite, scope, release.commit)
        if previous is not None and _instant(previous.content["started_at"]) > _instant(detected_at):
            raise ValueError("repair_observation_superseded")
        body = {"schema": CURRENT_SCHEMA, "scope": asdict(scope), "release_commit": release.commit,
                "observation_id": token, "started_at": detected_at, "state": "in_flight"}
        body["digest"] = _digest(body)
        record = RecordEnvelope.create(kind="reflection", title="Current native repair router observation",
            scope=scope, source=CURRENT_SOURCE, status="active", content=body)
        record.record_id = _current_id(scope, release.commit)
        if previous is not None:
            record.time.created_at = previous.time.created_at
        sqlite.upsert(record, commit=False)
        return token, [record], []
    return runtime.store.mutate_records_atomically(persist)


def current_system_code_repair_failure(store: Any, *, scope: ScopeRef, expected_commit: str) -> str | None:
    """Exact latest failure digest, real successful retirement '', or unknown None."""
    def read(sqlite):
        current = _read_current(sqlite, scope, expected_commit)
        if current is None or current.content["state"] != "complete":
            return None
        report = current.content["report"]
        incident = report.get("incident")
        if incident is None:
            # Unknown/policy-owner/router exceptions are not successful retirement.
            router = report["router_observation"]
            return "" if router.get("ok") is True and router.get("status") in {"idle", "processed", "existing"} else None
        from eimemory.storage.code_evolution_store import CodeEvolutionStore
        ledger = CodeEvolutionStore(store)
        consumed = ledger.get_policy_consumption(report['consumed_policy_digest'])
        transaction = ledger.get_transaction(report['policy_transaction_id'])
        if (not consumed or not transaction or consumed.get('transaction_id') != report['policy_transaction_id']
                or any(transaction.get(key) != value for key, value in asdict(scope).items())
                or transaction.get('policy_digest') != report['consumed_policy_digest']
                or transaction.get('base_commit') != report['consumed_policy_base_commit']):
            return None
        return incident["incident_digest"]
    try:
        return store.read_consistent(read)
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
        return None


def record_system_code_repair_failure(
    runtime: Any,
    *,
    scope: ScopeRef | Mapping[str, Any],
    repair_report: Mapping[str, Any],
    detected_at: str = "",
    observation_id: str = "",
) -> dict[str, Any]:
    """Persist one idempotent detector incident from an actual router report."""

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))
    release = current_release_identity(runtime, scope_ref)
    policy = load_code_automation_policy(path=CODE_AUTOMATION_POLICY_DEFAULT_PATH)
    report = detect_system_code_repair_failure(
        repair_report,
        policy=policy,
        release_commit=release.commit if release is not None else "",
        detected_at=detected_at or now_iso(),
    )
    incident = report.get("incident")
    if release is None:
        raise ValueError("repair_current_release_unavailable")
    _instant(report["detected_at"])
    def persist(sqlite):
        previous = _read_current(sqlite, scope_ref, release.commit)
        if observation_id:
            if (previous is None or previous.content["observation_id"] != observation_id
                    or previous.content["state"] != "in_flight"):
                return {**report, "status": "superseded", "incident_record_id": ""}, [], []
        elif previous is not None and _instant(previous.content["started_at"]) > _instant(report["detected_at"]):
            return {**report, "status": "superseded", "incident_record_id": ""}, [], []
        elif (not observation_id and previous is not None and previous.content['state'] == 'complete'
              and previous.content['started_at'] == report['detected_at'] and previous.content['report'] != report):
            raise ValueError('repair_same_time_observation_conflict')
        changed = []
        record = None
        if isinstance(incident, Mapping):
            marker = f"system-code-repair-failure:{incident['incident_digest']}"
            record = sqlite.get_by_id(incident["incident_id"], scope=scope_ref, exact_scope=True)
            if record is not None:
                if (record.kind != "incident" or record.source != SOURCE or record.scope != scope_ref
                        or not validate_system_code_repair_failure_report(record.content.get("detector_report"))
                        or record.content.get("incident_digest") != incident["incident_digest"]
                        or any(record.content.get(key) != value for key, value in incident.items())):
                    raise ValueError("repair_incident_identity_conflict")
            else:
                record = RecordEnvelope.create(
                kind="incident",
                title=str(incident["title"]),
                summary=str(incident["summary"]),
                detail="Bounded system observation from the autonomous repair router.",
                content={**dict(incident), "detector_report": report},
                tags=["code-evolution", "system-repair", "system-detected"],
                source=SOURCE,
                scope=scope_ref,
                status="active",
                provenance={
                    "origin": "system_detector",
                    "detector": DETECTOR_ID,
                    **report['source_facts']['facts'],
                },
                meta={
                    "idempotency_key": marker,
                    "incident_class": INCIDENT_CLASS,
                    "incident_digest": incident["incident_digest"],
                    "observation_valid": True,
                },
                )
                record.record_id = incident["incident_id"]
                sqlite.upsert(record, commit=False)
                changed.append(record)
        body = {"schema": CURRENT_SCHEMA, "scope": asdict(scope_ref), "release_commit": release.commit,
                "observation_id": observation_id or uuid4().hex, "started_at": report["detected_at"],
                "state": "complete", "report": report}
        body["digest"] = _digest(body)
        current = RecordEnvelope.create(kind="reflection", title="Current native repair router observation",
            scope=scope_ref, source=CURRENT_SOURCE, status="active", content=body)
        current.record_id = _current_id(scope_ref, release.commit)
        if previous is not None:
            current.time.created_at = previous.time.created_at
        sqlite.upsert(current, commit=False)
        changed.append(current)
        return {**report, "incident_record_id": record.record_id if record else "",
                "current_observation_record_id": current.record_id}, changed, []
    return runtime.store.mutate_records_atomically(persist)


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "DETECTOR_ID",
    "INCIDENT_CLASS",
    "SOURCE",
    "detect_system_code_repair_failure",
    "record_system_code_repair_failure",
]
