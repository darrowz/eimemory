from __future__ import annotations

from typing import Any

from eimemory.persona.schema import PersonaRoute, PersonaState
from eimemory.persona.state import default_persona_state


def route_persona_context(
    text: str,
    *,
    state: PersonaState | None = None,
    recent_context: dict[str, Any] | None = None,
) -> PersonaRoute:
    state = state or default_persona_state()
    normalized = _normalize(text)
    context = dict(recent_context or {})
    scene = _scene(normalized, context)
    risk = _risk_level(scene, normalized)
    tone, verbosity = _tone_and_verbosity(scene, normalized, state)
    adjustments = _trait_adjustments(scene, risk, normalized)
    guidance = _guidance(scene, risk, normalized)
    return PersonaRoute(
        scene=scene,
        tone=tone,
        verbosity=verbosity,
        risk_level=risk,
        trait_adjustments=adjustments,
        guidance=guidance,
        facets={
            "user_style": "concise_direct_action_first",
            "task_scene": scene,
            "safety_boundary": risk,
            "verification_policy": "include_verification_or_gap",
        },
    )


def _normalize(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _scene(text: str, context: dict[str, Any]) -> str:
    task_type = _normalize(str(context.get("task_type") or context.get("taskType") or ""))
    combined = f"{text} {task_type}"
    if _has_any(combined, ("recovery code", "api key", "secret", "token", "cookie", "password", "密钥", "密码", "授权", "付费", "转账", "删除")):
        return "high_risk_security"
    if _has_any(combined, ("codex", "代码", "repo", "patch", "commit", "部署", "测试", "实现", "修 bug", "bug", "coding")):
        return "coding_plan"
    if _has_any(combined, ("打不开", "失败", "报错", "卡住", "broken", "error", "fail", "vnc", "systemd", "service")):
        return "technical_debug"
    if _has_any(combined, ("论文", "研究", "新闻", "github", "arxiv", "资讯", "research")):
        return "research"
    if _has_any(combined, ("客户", "商业", "业务", "增长", "收入", "成本", "business")):
        return "business_analysis"
    if _has_any(combined, ("累", "难受", "焦虑", "沮丧", "孤独", "tired", "sad", "stress")):
        return "emotional_companion"
    if _has_any(combined, ("画", "故事", "文案", "设定", "创意", "creative")):
        return "creative"
    if _has_any(combined, ("短一点", "快点", "别废话", "直接", "fast", "brief")):
        return "fast_reply"
    return "technical_plan"


def _risk_level(scene: str, text: str) -> str:
    if scene == "high_risk_security":
        return "high"
    if _has_any(text, ("删除", "付费", "转账", "授权", "外发", "secret", "token", "key")):
        return "high"
    if scene in {"technical_debug", "coding_plan", "research"}:
        return "medium"
    return "low"


def _tone_and_verbosity(scene: str, text: str, state: PersonaState) -> tuple[str, str]:
    if scene == "emotional_companion":
        return "warm_grounded", "medium"
    if scene == "high_risk_security":
        return "firm_safe", "brief"
    if scene == "coding_plan":
        return "concise_implementation_ready", "medium"
    if scene == "research":
        return "evidence_first", "medium"
    if scene == "fast_reply" or _has_any(text, ("短一点", "别废话", "快点")):
        return "direct_brief", "brief"
    if state.traits.verbosity <= 0.25:
        return "concise_direct", "brief"
    return "concise_direct", "medium"


def _trait_adjustments(scene: str, risk: str, text: str) -> dict[str, float]:
    adjustments: dict[str, float] = {}
    if scene in {"coding_plan", "technical_debug"}:
        adjustments.update({"precision": 0.1, "execution": 0.05, "resourcefulness": 0.1, "humor": -0.1})
    if scene == "emotional_companion":
        adjustments.update({"empathy": 0.15, "warmth": 0.15, "verbosity": -0.05})
    if scene == "high_risk_security" or risk == "high":
        adjustments.update({"safety": 0.2, "precision": 0.1, "autonomy": -0.1})
    if _has_any(text, ("短一点", "别废话", "戏很多", "直接")):
        adjustments.update({"verbosity": -0.15, "humor": -0.05, "latency_priority": 0.1})
    return adjustments


def _guidance(scene: str, risk: str, text: str) -> list[str]:
    base = [
        "Use a functional persona model only; avoid subjective inner-life claims.",
        "Include verification method or known verification gap.",
    ]
    if scene == "coding_plan":
        return [
            "Give implementation-ready steps tied to files and tests.",
            "Keep the answer concise and action-first.",
            "Include verification criteria.",
            *base,
        ]
    if scene == "high_risk_security":
        return [
            "Do not store or quote plaintext secrets.",
            "Require confirmation for money, external sends, destructive actions, or account authorization.",
            "Offer alias-based safe handling when appropriate.",
            *base,
        ]
    if scene == "technical_debug":
        return [
            "Diagnose from evidence before fixing.",
            "If a tool path fails, switch route before reporting a blocker.",
            "Report concrete command evidence.",
            *base,
        ]
    if scene == "emotional_companion":
        return [
            "Be warm, grounded, and not preachy.",
            "Acknowledge the user briefly, then offer one practical next step.",
            *base,
        ]
    if scene == "research":
        return [
            "Use source, date, evidence grade, and conflict checks.",
            "Do not promote weak evidence into final guidance.",
            *base,
        ]
    if scene == "fast_reply" or _has_any(text, ("短一点", "别废话")):
        return ["Answer briefly first.", "Avoid long background.", *base]
    return ["Stay direct, useful, and evidence-aware.", *base]
