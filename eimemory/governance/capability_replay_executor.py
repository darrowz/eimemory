from __future__ import annotations

import json
from typing import Any

from eimemory.governance.capability_attribution import collect_capability_evidence
from eimemory.models.records import ScopeRef


CASE_EVIDENCE_REQUIREMENTS: dict[str, tuple[tuple[str, ...], ...]] = {
    "search_recent_source": (("recent", "recency", "time window", "近期", "最近", "时间窗口"), ("source quality", "source trust", "trust score", "来源质量", "来源可信度", "可信度")),
    "search_trending_github": (("github",), ("trending", "created range", "热门", "趋势", "创建时间"), ("stars", "star sort", "星标", "星数")),
    "search_primary_source": (("official", "primary source", "官方", "一手来源"), ("verified", "verification", "已验证", "核验")),
    "research_evidence_gate": (("evidence", "citation", "cite", "证据", "引用"), ("inference", "claim", "fact", "推断", "主张", "事实")),
    "research_conflict_resolution": (("conflict", "disagree", "冲突", "不一致"), ("recency", "confidence", "时效", "置信度")),
    "research_actionable_takeaway": (("actionable", "implementation", "可执行", "落地"), ("replay", "playbook", "decision", "回放", "剧本", "决策")),
    "uumit_requirement_checklist": (("requirement", "checklist", "需求", "检查清单"), ("acceptance", "delivery", "验收", "交付")),
    "uumit_quality_gate": (("quality", "质量"), ("version", "visual", "customer constraint", "版本", "视觉", "客户约束")),
    "uumit_post_delivery_followup": (("follow-up", "followup", "after delivery", "后续", "交付后"), ("outcome", "correction", "policy", "结果", "纠正", "策略")),
    "device_physical_channel": (("channel", "speaker", "audio output", "通道", "扬声器", "音频输出"), ("control", "playback", "控制", "播放")),
    "device_missing_info": (("missing target", "target device", "缺少目标", "目标设备"), ("clarify", "safe inference", "澄清", "安全推断")),
    "device_safe_boundary": (("reversible", "rollback", "可逆", "回滚"), ("verification signal", "verified output", "验证信号", "输出验证")),
}


def execute_capability_replay_case(runtime: Any, case: dict[str, Any]) -> dict[str, Any]:
    """Replay one capability case against verified, attributable outcome evidence."""

    scope = ScopeRef.from_dict(case.get("scope") or {})
    capability = str(case.get("target_capability") or "").strip()
    evidence_by_capability = collect_capability_evidence(runtime, scope=scope, limit=500)
    evidence_items = sorted(
        (
            item
            for item in evidence_by_capability.get(capability, [])
            if float(item.get("score") or 0.0) >= 0.7
            and str(item.get("source_kind") or "") in {"outcome_trace", "event_outcome"}
            and str(item.get("source_id") or "")
        ),
        key=lambda item: str(item.get("source_id") or ""),
    )
    requirements = CASE_EVIDENCE_REQUIREMENTS.get(str(case.get("case_id") or ""), ())
    matching_items = [item for item in evidence_items if _matches_requirements(_evidence_text(runtime, item, scope=scope), requirements)]
    if not matching_items:
        return {
            "verdict": "not_run",
            "hit": None,
            "observed": "",
            "reason": "case_specific_outcome_evidence_missing",
        }
    evidence = matching_items[0]
    source_id = str(evidence.get("source_id") or "")
    source_record = runtime.store.get_by_id(source_id, scope=scope)
    if source_record is None:
        return {
            "verdict": "not_run",
            "hit": None,
            "observed": f"source_id={source_id}",
            "reason": "outcome_evidence_not_retrievable",
        }
    content = source_record.content if isinstance(source_record.content, dict) else {}
    payload = content.get("payload") if isinstance(content.get("payload"), dict) else {}
    verifier = payload.get("verifier") if isinstance(payload.get("verifier"), dict) else {}
    outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
    outcome_status = str(outcome.get("status") or source_record.meta.get("outcome_status") or "").lower()
    hit = bool(
        source_record.source == "eimemory.experience.outcome_trace"
        and verifier.get("passed") is True
        and outcome_status in {"success", "good", "passed", "pass", "completed"}
    )
    return {
        "verdict": "pass" if hit else "fail",
        "hit": hit,
        "evidence_source_id": source_id,
        "observed": f"source_id={source_id};trace_id={payload.get('trace_id', '')};verifier_passed={verifier.get('passed') is True}",
        **({"reason": "verified_outcome_integrity_check_failed"} if not hit else {}),
    }


def _evidence_text(runtime: Any, evidence: dict[str, Any], *, scope: ScopeRef) -> str:
    source_id = str(evidence.get("source_id") or "")
    record = runtime.store.get_by_id(source_id, scope=scope)
    if record is None:
        return str(evidence.get("summary") or "").lower()
    return " ".join(
        (
            str(evidence.get("summary") or ""),
            str(record.title or ""),
            str(record.summary or ""),
            str(record.detail or ""),
            json.dumps(record.content if isinstance(record.content, dict) else {}, ensure_ascii=False, sort_keys=True, default=str),
        )
    ).lower()


def _matches_requirements(text: str, requirements: tuple[tuple[str, ...], ...]) -> bool:
    value = str(text or "").lower()
    return bool(requirements) and all(any(term in value for term in group) for group in requirements)
