from __future__ import annotations

from eimemory.persona.schema import PersonaCorrectionEvent


def correction_from_user_text(text: str) -> PersonaCorrectionEvent:
    raw = str(text or "").strip()
    lowered = raw.lower()
    if _has_any(lowered, ("api key", "secret", "token", "cookie", "密码", "密钥", "recovery code")):
        return PersonaCorrectionEvent(
            raw_text=raw,
            category="safety",
            severity=0.9,
            trait_delta={"safety": 0.08, "precision": 0.05, "autonomy": -0.05},
            rule_candidate="Never store, quote, or log plaintext secrets; use approved secret aliases and confirmation gates.",
        )
    if _has_any(lowered, ("戏很多", "别演", "废话", "短一点", "直接说", "少说")):
        return PersonaCorrectionEvent(
            raw_text=raw,
            category="verbosity",
            severity=0.85,
            trait_delta={"verbosity": -0.15, "humor": -0.05, "latency_priority": 0.08},
            rule_candidate="When the user says the agent is overacting or verbose, answer direct result first.",
        )
    if _has_any(lowered, ("不要说做不到", "解决", "换路", "想办法")):
        return PersonaCorrectionEvent(
            raw_text=raw,
            category="resourcefulness",
            severity=0.8,
            trait_delta={"resourcefulness": 0.1, "execution": 0.05},
            rule_candidate="When the first path fails, try an alternate tool or route before reporting a blocker.",
        )
    if _has_any(lowered, ("不对", "错了", "弄错", "校验")):
        return PersonaCorrectionEvent(
            raw_text=raw,
            category="correctness",
            severity=0.8,
            trait_delta={"precision": 0.1},
            rule_candidate="Convert user corrections into replay checks before treating the behavior as fixed.",
        )
    if _has_any(lowered, ("快点", "太慢", "响应慢")):
        return PersonaCorrectionEvent(
            raw_text=raw,
            category="latency",
            severity=0.7,
            trait_delta={"latency_priority": 0.1, "verbosity": -0.08},
            rule_candidate="Prefer a short status-first response when latency pressure is explicit.",
        )
    if _has_any(lowered, ("记住", "以后", "偏好")):
        return PersonaCorrectionEvent(
            raw_text=raw,
            category="memory",
            severity=0.65,
            trait_delta={"precision": 0.03},
            rule_candidate="Treat explicit user preferences as memory candidates with clear scope.",
        )
    return PersonaCorrectionEvent(
        raw_text=raw,
        category="tone",
        severity=0.4,
        trait_delta={"empathy": 0.03},
        rule_candidate="Keep tone grounded and adapt to the user's correction.",
    )


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)
