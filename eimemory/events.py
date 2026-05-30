from __future__ import annotations

import json
import re
from dataclasses import asdict
from hashlib import sha256
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import ScopeRef


DEFAULT_INTENT_PATTERNS: tuple[dict[str, Any], ...] = (
    {
        "pattern": "唱首歌|放首歌|来首歌",
        "default_event_type": "media_playback",
        "interpreted_intent": "播放音乐给用户听",
        "first_questions": ["想听哪首歌？", "是在当前设备播放，还是发可播放链接/音频？"],
        "execution_policy": [
            "优先考虑用户能否实际听见",
            "先判断播放出口和物理条件",
            "不要默认进入原创歌词/TTS/版权长解释",
        ],
        "success_criteria": "用户能听到或打开播放",
    },
    {
        "pattern": "坏了|没反应|失败了",
        "default_event_type": "repair",
        "interpreted_intent": "先自主诊断并尝试修复",
        "execution_policy": ["先查状态", "看日志", "定位根因", "低风险修复", "验证", "再汇报"],
        "ask_first_boundaries": ["花钱", "授权", "外发", "不可逆删除", "生产破坏"],
        "success_criteria": "根因明确，低风险修复已验证，或清楚说明阻塞条件",
    },
    {
        "pattern": "搜索最近 GitHub 最火|GitHub 热门项目|最近最火 GitHub",
        "default_event_type": "trending_search",
        "interpreted_intent": "搜索指定时间窗口内增长最快或讨论热度最高的 GitHub 项目",
        "execution_policy": ["先说明 trending 口径", "确认时间范围", "按 stars 增长、讨论热度或榜单来源检索"],
        "success_criteria": "结果附带时间窗口、排序口径和可验证链接",
    },
    {
        "pattern": "最高星项目|star 最多|stars 最多",
        "default_event_type": "github_star_ranking",
        "interpreted_intent": "按 GitHub stars 总量排序查找项目，不等同于最近 trending",
        "execution_policy": ["说明 created 范围和 stars sort 口径", "不要把最高星误报成最近最火"],
        "success_criteria": "结果包含 stars 排序口径、查询范围和仓库链接",
    },
)


def normalize_scope(scope: ScopeRef | dict[str, Any] | None) -> ScopeRef:
    return scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)


def event_id(payload: dict[str, Any], scope: ScopeRef) -> str:
    stable = json.dumps(
        {
            "scope": asdict(scope),
            "source": payload.get("source"),
            "user_phrase": payload.get("user_phrase"),
            "event_type": payload.get("event_type"),
            "timestamp": payload.get("timestamp"),
            "goal": payload.get("goal"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "evt_" + sha256(stable.encode("utf-8")).hexdigest()[:16]


def pattern_id(payload: dict[str, Any], scope: ScopeRef) -> str:
    stable = json.dumps(
        {
            "scope": asdict(scope),
            "pattern": payload.get("pattern"),
            "event_type": payload.get("default_event_type") or payload.get("event_type"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "ip_" + sha256(stable.encode("utf-8")).hexdigest()[:16]


def outcome_id(event_id_value: str, payload: dict[str, Any]) -> str:
    stable = json.dumps(
        {
            "event_id": event_id_value,
            "outcome": payload.get("outcome"),
            "correction_from_user": payload.get("correction_from_user"),
            "policy_update": payload.get("policy_update"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "out_" + sha256(stable.encode("utf-8")).hexdigest()[:16]


def normalized_terms(text: str) -> set[str]:
    return {term.lower() for term in re.findall(r"[\w]+", str(text or ""), flags=re.UNICODE) if term.strip()}


def pattern_matches(pattern: str, phrase: str) -> bool:
    phrase_text = str(phrase or "").lower()
    for raw in str(pattern or "").split("|"):
        token = raw.strip().lower()
        if token and token in phrase_text:
            return True
    pattern_terms = normalized_terms(pattern)
    phrase_terms = normalized_terms(phrase)
    return bool(pattern_terms and phrase_terms and pattern_terms & phrase_terms)


def event_similarity(event: dict[str, Any], phrase: str, event_type: str = "") -> float:
    phrase_terms = normalized_terms(phrase)
    searchable = " ".join(
        str(event.get(key) or "")
        for key in ("user_phrase", "interpreted_intent", "goal", "lesson", "next_policy", "verification", "event_type")
    )
    event_terms = normalized_terms(searchable)
    if not phrase_terms or not event_terms:
        lexical = 0.0
    else:
        overlap = len(phrase_terms & event_terms)
        lexical = overlap / max(1, min(len(phrase_terms), len(event_terms)))
    if event_type and str(event.get("event_type") or "") == event_type:
        lexical += 0.35
    return min(1.0, lexical)


def ensure_event_payload(payload: dict[str, Any], scope: ScopeRef) -> dict[str, Any]:
    data = dict(payload or {})
    timestamp = str(data.get("timestamp") or now_iso())
    data["id"] = str(data.get("id") or event_id({**data, "timestamp": timestamp}, scope))
    data["timestamp"] = timestamp
    data["source"] = str(data.get("source") or "manual")
    data["event_type"] = str(data.get("event_type") or "communication")
    data["user_phrase"] = str(data.get("user_phrase") or "")
    data["interpreted_intent"] = str(data.get("interpreted_intent") or data.get("goal") or "")
    data["goal"] = str(data.get("goal") or data["interpreted_intent"])
    data["confidence"] = _clamp_float(data.get("confidence"), default=0.5)
    return data


def ensure_pattern_payload(payload: dict[str, Any], scope: ScopeRef) -> dict[str, Any]:
    data = dict(payload or {})
    data["pattern"] = str(data.get("pattern") or "")
    data["default_event_type"] = str(data.get("default_event_type") or data.get("event_type") or "communication")
    data["id"] = str(data.get("id") or pattern_id(data, scope))
    data["confidence"] = _clamp_float(data.get("confidence"), default=0.75)
    return data


def ensure_outcome_payload(event_id_value: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    data["event_id"] = str(event_id_value)
    data["id"] = str(data.get("id") or outcome_id(event_id_value, data))
    data["outcome"] = str(data.get("outcome") or "uncertain")
    data["recorded_at"] = str(data.get("recorded_at") or now_iso())
    return data


def _clamp_float(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return round(max(0.0, min(1.0, number)), 3)
