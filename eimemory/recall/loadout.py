from __future__ import annotations

from typing import Any


PERSONA_TYPES = frozenset(
    {
        "persona",
        "preference",
        "instruction",
        "user_preference",
        "operator_preference",
        "user_profile",
    }
)
_DROP_TYPES = frozenset(
    {
        "conversation",
        "research",
        "paper",
        "research_note",
        "research_finding",
        "visual_identity_event",
        "deployment_evidence",
        "task_episode",
    }
)
_DROP_TITLE = ("[paper]", "arxiv", "completed turn", "openclaw agent outcome", "locomo", "bfcl")
_MAX_ITEM_CHARS = 360


def assemble_loadout(items: list[dict[str, Any]], *, limit: int, task_evidence: bool = False) -> dict[str, Any]:
    """Split Tencent-style loadout: stable persona vs query L1."""

    kept: list[dict[str, Any]] = []
    for item in items:
        memory_type = str(item.get("memory_type") or "").strip().lower()
        title = str(item.get("title") or "")
        summary = str(item.get("summary") or item.get("text") or "").strip()
        if memory_type in _DROP_TYPES and not (task_evidence and memory_type in {'conversation', 'task_episode'}):
            continue
        lowered = title.lower()
        if any((marker in lowered or marker in summary.lower())
               and not (task_evidence and marker == 'completed turn') for marker in _DROP_TITLE):
            continue
        if len(summary) > _MAX_ITEM_CHARS:
            item = dict(item)
            item["summary"] = summary[: _MAX_ITEM_CHARS - 1] + "…"
        kept.append(item)
    persona = [item for item in kept if str(item.get("memory_type") or "") in PERSONA_TYPES][:2]
    query_items = kept[: max(1, int(limit))]
    return {
        "items": query_items,
        "persona": persona,
        "layer": "l1",
        "loadout": "l3_persona+l1_query",
        "tools_guide": (
            "记忆不够时用 eimemory_search_l0 查原始对话，每轮最多 3 次；"
            "无结果就按已有信息回答，不要继续搜。"
        ),
    }


def render_loadout(payload: dict[str, Any], *, max_chars: int) -> str:
    lines: list[str] = []
    if payload.get('retrieval_status') == 'ambiguous':
        return '请明确所问项目，或指定全部/全局任务。'[:max(32, int(max_chars))]
    if payload.get('task_evidence_scope') == 'historical_only_latest_state_unverified':
        lines.append('以下为历史任务证据；当前执行状态尚未核验，请核对宿主任务台账。')
    for item in payload.get("persona") or []:
        summary = str(item.get("evidence_excerpt") or item.get("summary") or "").strip()
        title = str(item.get("title") or "").strip()
        if summary:
            lines.append(f"- [persona] {title}: {summary}" if title else f"- [persona] {summary}")
    for item in payload.get("items") or []:
        summary = str(item.get("evidence_excerpt") or item.get("summary") or "").strip()
        title = str(item.get("title") or "").strip()
        if not summary:
            continue
        if any(existing.endswith(summary) for existing in lines):
            continue
        citation = str(item.get('record_id') or '')
        if citation:
            summary = f"[{item.get('source_id', 'default')}:{citation}] {summary}"
        lines.append(f"- [memory] {title}: {summary}" if title else f"- [memory] {summary}")
    if not lines:
        return ""
    guide = "\n记忆不够时用 eimemory_search_l0 查原始对话，每轮最多 3 次；无结果就按已有信息回答。"
    text = "Relevant eimemory context:\n" + "\n".join(lines) + guide
    return text[: max(32, int(max_chars))]
