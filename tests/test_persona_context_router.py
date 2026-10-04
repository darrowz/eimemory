from __future__ import annotations

import pytest

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


@pytest.mark.parametrize("context_key", ["task_type", "taskType"])
def test_router_normalizes_context_task_type(context_key) -> None:
    context = {context_key: "  CODING\n"}

    route = route_persona_context("", recent_context=context)

    assert route.scene == "coding_plan"
    assert route.tone == "concise_implementation_ready"
    assert context == {context_key: "  CODING\n"}


@pytest.mark.parametrize("text", ["", "  \t\n"])
def test_router_empty_text_uses_default_scene(text) -> None:
    route = route_persona_context(text)

    assert route.scene == "technical_plan"


@pytest.mark.parametrize(
    ("text", "expected_verbosity"),
    [
        ("coding brief", "brief"),
        ("CODING\nBRIEFLY", "brief"),
        ("coding concise", "brief"),
        ("coding short", "brief"),
        ("coding shortly", "medium"),
        ("coding briefcase", "medium"),
    ],
)
def test_router_brief_request_uses_whole_words(text, expected_verbosity) -> None:
    route = route_persona_context(text)

    assert route.scene == "coding_plan"
    assert route.tone == "concise_implementation_ready"
    assert route.verbosity == expected_verbosity
