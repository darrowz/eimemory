from __future__ import annotations

from eimemory.persona.context_router import route_persona_context
from eimemory.persona.state import default_persona_state


def test_router_detects_coding_plan_and_verification_guidance() -> None:
    route = route_persona_context("用 Codex 实现这个功能，补测试并部署", state=default_persona_state())

    assert route.scene == "coding_plan"
    assert route.tone == "concise_implementation_ready"
    assert route.verbosity == "medium"
    assert any("verification" in item.lower() for item in route.guidance)


def test_router_detects_high_risk_secret_request() -> None:
    route = route_persona_context("帮我保存 GitHub recovery codes 和 API key", state=default_persona_state())

    assert route.scene == "high_risk_security"
    assert route.risk_level == "high"
    assert any("plaintext secrets" in item.lower() for item in route.guidance)


def test_router_detects_resourcefulness_for_tool_failure() -> None:
    route = route_persona_context("网页打不开怎么办", state=default_persona_state())

    assert route.scene == "technical_debug"
    assert route.trait_adjustments["resourcefulness"] > 0
    assert any("switch route" in item.lower() for item in route.guidance)


def test_router_detects_emotional_companion_without_fake_emotion() -> None:
    route = route_persona_context("我觉得最近有点累", state=default_persona_state())

    assert route.scene == "emotional_companion"
    assert route.tone == "warm_grounded"
    assert all("real feeling" not in item.lower() for item in route.guidance)
