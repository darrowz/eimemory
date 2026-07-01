from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import re
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef


def promote_repeated_sops_to_skill_candidates(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    min_repeats: int = 3,
    persist: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    groups = _sop_groups(runtime, scope=scope_ref, limit=limit)
    skills: list[dict[str, Any]] = []
    blocked_skills: list[dict[str, Any]] = []
    candidate_ids: list[str] = []
    registry_ids: list[str] = []
    for key, records in sorted(groups.items()):
        if len(records) < max(1, int(min_repeats)):
            continue
        entry = _skill_entry(key, records, scope=scope_ref)
        if not any(_replay_passed(record) for record in records):
            blocked_skills.append(
                {
                    "sop_key": key,
                    "target_capability": entry["target_capability"],
                    "source_record_ids": list(entry.get("source_record_ids") or []),
                    "missing_contract": ["replay_evidence"],
                }
            )
            continue
        missing_contract = _missing_execution_contract(entry)
        if missing_contract:
            blocked_skills.append(
                {
                    "sop_key": key,
                    "target_capability": entry["target_capability"],
                    "source_record_ids": list(entry.get("source_record_ids") or []),
                    "missing_contract": missing_contract,
                }
            )
            continue
        if persist:
            candidate = _upsert_skill_candidate(runtime, entry, records, scope=scope_ref)
            registry = _upsert_registry_entry(runtime, entry, candidate.record_id, records, scope=scope_ref)
            candidate_ids.append(candidate.record_id)
            registry_ids.append(registry.record_id)
            entry["candidate_id"] = candidate.record_id
            entry["registry_record_id"] = registry.record_id
        skills.append(entry)
    return {
        "ok": True,
        "report_type": "skill_sedimentation",
        "scope": asdict(scope_ref),
        "min_repeats": max(1, int(min_repeats)),
        "sop_group_count": len(groups),
        "skill_candidate_count": len(skills),
        "blocked_skill_count": len(blocked_skills),
        "blocked_skills": blocked_skills,
        "candidate_ids": candidate_ids,
        "registry_record_ids": registry_ids,
        "skills": skills,
    }


def list_eiskills(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    records = [
        record
        for record in runtime.store.list_records(kinds=["learning_playbook"], scope=scope_ref, limit=limit)
        if str(record.meta.get("report_type") or "") == "eiskill_registry_entry"
    ]
    skills = [_registry_skill(record) for record in records]
    return {"ok": True, "report_type": "eiskills_registry", "scope": asdict(scope_ref), "skill_count": len(skills), "skills": skills}


def call_eiskill(
    runtime: Any,
    *,
    skill_id: str,
    scope: dict[str, Any] | ScopeRef | None = None,
    context: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    registry = runtime.store.get_by_id(str(skill_id), scope=scope_ref)
    if registry is None:
        registry = _find_registry_by_skill_id(runtime, str(skill_id), scope=scope_ref)
    if registry is None:
        return {"ok": False, "error": "eiskill_not_found", "skill_id": str(skill_id)}
    skill = _registry_skill(registry)
    missing_contract = _missing_execution_contract(skill)
    if missing_contract:
        return {
            "ok": False,
            "error": "eiskill_contract_incomplete",
            "skill_id": skill["skill_id"],
            "missing_contract": missing_contract,
        }
    record_id = ""
    if persist:
        record = RecordEnvelope.create(
            kind="learning_eval",
            title=f"eiskill invocation: {skill['name']}",
            summary=f"Invoked {skill['skill_id']}",
            scope=scope_ref,
            source="eimemory.eiskills",
            status="active",
            content={
                "report_type": "eiskill_invocation",
                "skill_id": skill["skill_id"],
                "context": dict(context or {}),
                "steps": list(skill.get("steps") or []),
            },
            meta={
                "report_type": "eiskill_invocation",
                "skill_id": skill["skill_id"],
                "target_capability": skill.get("target_capability", ""),
            },
        )
        runtime.store.append(record)
        record_id = record.record_id
        registry.meta["reuse_count"] = int(registry.meta.get("reuse_count") or 0) + 1
        registry.content["reuse_count"] = int(registry.content.get("reuse_count") or 0) + 1
        registry.time.updated_at = now_iso()
        runtime.store.rewrite(registry)
    return {"ok": True, "skill_id": skill["skill_id"], "name": skill["name"], "steps": skill["steps"], "record_id": record_id}


def _sop_groups(runtime: Any, *, scope: ScopeRef, limit: int) -> dict[str, list[RecordEnvelope]]:
    groups: dict[str, list[RecordEnvelope]] = {}
    for record in runtime.store.list_records(kinds=["learning_playbook"], scope=scope, limit=limit):
        if str(record.meta.get("report_type") or "") == "eiskill_registry_entry":
            continue
        key = _sop_key(record)
        if not key:
            continue
        groups.setdefault(key, []).append(record)
    return groups


def _sop_key(record: RecordEnvelope) -> str:
    for payload in (record.meta, record.content):
        value = str(payload.get("sop_key") or payload.get("semantic_key") or "").strip()
        if value:
            return _slug(value)
    return _slug(record.title or record.summary)


def _replay_passed(record: RecordEnvelope) -> bool:
    for payload in (record.meta, record.content):
        if payload.get("replay_passed") is True:
            return True
        if str(payload.get("replay_verdict") or payload.get("verdict") or "").strip().lower() == "pass":
            return True
        pass_rate = _float_or_none(payload.get("pass_rate") or payload.get("replay_pass_rate"))
        threshold = _float_or_none(payload.get("threshold") or payload.get("min_pass_rate")) or 1.0
        if pass_rate is not None and pass_rate >= threshold:
            return True
    return False


def _skill_entry(key: str, records: list[RecordEnvelope], *, scope: ScopeRef) -> dict[str, Any]:
    first = records[0]
    target_capability = str(first.meta.get("target_capability") or first.content.get("target_capability") or "proactive.judgment")
    steps = _steps(records)
    trigger_conditions = _trigger_conditions(records)
    skill_id = f"eiskill_{_stable_hash(key, target_capability, asdict(scope))[:16]}"
    return {
        "skill_id": skill_id,
        "name": str(first.title or key),
        "sop_key": key,
        "target_capability": target_capability,
        "repeat_count": len(records),
        "source_record_ids": [record.record_id for record in records],
        "steps": steps,
        "trigger_conditions": trigger_conditions,
        "action": _first_payload_text(records, "action", default=""),
        "verification": _first_payload_text(records, "verification", default=""),
        "rollback": _first_payload_text(records, "rollback", default=""),
        "callable": True,
        "status": "sandbox_ready",
        "candidate_id": "",
        "registry_record_id": "",
    }


def _upsert_skill_candidate(runtime: Any, entry: dict[str, Any], records: list[RecordEnvelope], *, scope: ScopeRef) -> RecordEnvelope:
    candidate_id = f"skillcand_{_stable_hash(entry['skill_id'], 'candidate')[:16]}"
    existing = runtime.store.get_by_id(candidate_id, scope=scope)
    record = RecordEnvelope(
        record_id=candidate_id,
        kind="skill_candidate",
        status="sandbox_ready",
        title=f"Skill candidate: {entry['name']}",
        summary=f"{entry['name']} repeated {entry['repeat_count']} time(s) and passed replay evidence.",
        detail="\n".join(str(step) for step in entry.get("steps") or []),
        content={
            "skill_id": entry["skill_id"],
            "sop_key": entry["sop_key"],
            "target_capability": entry["target_capability"],
            "steps": list(entry.get("steps") or []),
            "trigger_conditions": list(entry.get("trigger_conditions") or []),
            "action": entry["action"],
            "verification": entry["verification"],
            "rollback": entry["rollback"],
            "source_playbook_ids": list(entry.get("source_record_ids") or []),
            "status": "sandbox_ready",
            "generated_by": "eimemory.skill_sedimentation",
        },
        tags=["skill-candidate", "eiskill", "sop-sedimentation", entry["target_capability"]],
        links=[LinkRef(relation="derived_from", target_kind="learning_playbook", target_id=record.record_id) for record in records],
        evidence=[record.record_id for record in records],
        source="eimemory.skill_sedimentation",
        scope=scope,
        time=TimeRef.now(),
        provenance={"source": "eimemory.skill_sedimentation"},
        meta={
            "report_type": "eiskill_candidate",
            "status": "sandbox_ready",
            "skill_id": entry["skill_id"],
            "sop_key": entry["sop_key"],
            "target_capability": entry["target_capability"],
            "repeat_count": entry["repeat_count"],
            "generated_by": "eimemory.skill_sedimentation",
        },
    )
    if existing is not None:
        record.time.created_at = existing.time.created_at
        record.touch()
        return runtime.store.rewrite(record)
    return runtime.store.append(record)


def _upsert_registry_entry(
    runtime: Any,
    entry: dict[str, Any],
    candidate_id: str,
    records: list[RecordEnvelope],
    *,
    scope: ScopeRef,
) -> RecordEnvelope:
    existing = runtime.store.get_by_id(entry["skill_id"], scope=scope)
    content = {
        "report_type": "eiskill_registry_entry",
        "skill_id": entry["skill_id"],
        "candidate_id": candidate_id,
        "name": entry["name"],
        "sop_key": entry["sop_key"],
        "target_capability": entry["target_capability"],
        "steps": list(entry.get("steps") or []),
        "trigger_conditions": list(entry.get("trigger_conditions") or []),
        "action": entry["action"],
        "verification": entry["verification"],
        "rollback": entry["rollback"],
        "callable": True,
        "source_playbook_ids": list(entry.get("source_record_ids") or []),
        "reuse_count": int(getattr(existing, "content", {}).get("reuse_count") or 0) if existing is not None else 0,
    }
    record = RecordEnvelope(
        record_id=entry["skill_id"],
        kind="learning_playbook",
        status="active",
        title=f"eiskill: {entry['name']}",
        summary=f"Callable eiskill for {entry['target_capability']}",
        detail="\n".join(str(step) for step in entry.get("steps") or []),
        content=content,
        tags=["eiskill", "registry", entry["target_capability"]],
        links=[LinkRef(relation="registered_from", target_kind="skill_candidate", target_id=candidate_id)],
        evidence=[record.record_id for record in records],
        source="eimemory.eiskills",
        scope=scope,
        time=TimeRef.now(),
        provenance={"source": "eimemory.skill_sedimentation", "candidate_id": candidate_id},
        meta={
            "report_type": "eiskill_registry_entry",
            "skill_id": entry["skill_id"],
            "candidate_id": candidate_id,
            "target_capability": entry["target_capability"],
            "callable": True,
            "repeat_count": entry["repeat_count"],
            "reuse_count": content["reuse_count"],
        },
    )
    if existing is not None:
        record.time.created_at = existing.time.created_at
        record.touch()
        return runtime.store.rewrite(record)
    return runtime.store.append(record)


def _registry_skill(record: RecordEnvelope) -> dict[str, Any]:
    content = dict(record.content or {})
    raw_skill = {
        "skill_id": str(content.get("skill_id") or record.record_id),
        "candidate_id": str(content.get("candidate_id") or record.meta.get("candidate_id") or ""),
        "name": str(content.get("name") or record.title),
        "sop_key": str(content.get("sop_key") or record.meta.get("sop_key") or ""),
        "target_capability": str(content.get("target_capability") or record.meta.get("target_capability") or ""),
        "steps": [str(step) for step in (content.get("steps") or [])],
        "trigger_conditions": [str(item) for item in (content.get("trigger_conditions") or [])],
        "action": str(content.get("action") or ""),
        "verification": str(content.get("verification") or ""),
        "rollback": str(content.get("rollback") or ""),
        "reuse_count": int(content.get("reuse_count") or record.meta.get("reuse_count") or 0),
        "record_id": record.record_id,
    }
    missing_contract = _missing_execution_contract(raw_skill)
    raw_skill["callable"] = bool(content.get("callable") or record.meta.get("callable")) and not missing_contract
    raw_skill["missing_contract"] = missing_contract
    return raw_skill


def _missing_execution_contract(skill: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    trigger_conditions = [str(item).strip() for item in (skill.get("trigger_conditions") or []) if str(item).strip()]
    if not trigger_conditions:
        missing.append("trigger_conditions")
    for key in ("action", "verification", "rollback"):
        if not str(skill.get(key) or "").strip():
            missing.append(key)
    return missing


def _find_registry_by_skill_id(runtime: Any, skill_id: str, *, scope: ScopeRef) -> RecordEnvelope | None:
    for record in runtime.store.list_records(kinds=["learning_playbook"], scope=scope, limit=500):
        if str(record.meta.get("report_type") or "") != "eiskill_registry_entry":
            continue
        if str(record.content.get("skill_id") or record.record_id) == skill_id:
            return record
    return None


def _steps(records: list[RecordEnvelope]) -> list[str]:
    for record in records:
        raw_steps = record.content.get("steps")
        if isinstance(raw_steps, list) and raw_steps:
            return [str(step) for step in raw_steps if str(step).strip()]
    text = " ".join(str(value or "") for record in records for value in (record.detail, record.summary))
    items = re.split(r"\s*\d+[\.)]\s+|[;\n]+", text)
    steps = [item.strip(" .:-")[:240] for item in items if item.strip(" .:-")]
    return steps[:6] or ["Run the repeated SOP and verify replay evidence before activation."]


def _trigger_conditions(records: list[RecordEnvelope]) -> list[str]:
    result: list[str] = []
    for record in records:
        raw = record.content.get("trigger_conditions") or record.meta.get("trigger_conditions") or record.content.get("triggers")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            continue
        for item in raw:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
    return result


def _first_payload_text(records: list[RecordEnvelope], key: str, *, default: str) -> str:
    for record in records:
        for payload in (record.content, record.meta):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
    return default


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9_.-]+", "-", str(value or "").strip().lower())
    return text.strip("-")[:80]


def _stable_hash(*parts: Any) -> str:
    return sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
