"""Ordinary scene selection, brevity, limits, and field mapping."""
from dataclasses import fields

import pytest

from eimemory.persona.context_router import route_persona_context
from eimemory.persona.prompt import build_persona_guidance
from eimemory.persona.schema import PersonaRuntimeState, PersonaState, PersonaTraits


@pytest.mark.parametrize("text", ["business report", "research report"])
def test_report_does_not_match_repo(text):
    assert route_persona_context(text, state=PersonaState()).scene != "coding_plan"


@pytest.mark.parametrize("text", ["inspect repo", "inspect repository", "inspect repositories", "检查repo", "repo分析"])
def test_repository_words_still_select_coding(text):
    assert route_persona_context(text, state=PersonaState()).scene == "coding_plan"


@pytest.mark.parametrize("text", ["coding brief", "research short", "代码短一点", "研究别废话"])
def test_explicit_brevity_preserves_scene_and_changes_verbosity(text):
    route = route_persona_context(text, state=PersonaState())
    assert route.scene in {"coding_plan", "research"}
    assert route.verbosity == "brief"


@pytest.mark.parametrize("limit", [0, 1, 10, 50, 119, 120, 800])
def test_requested_character_limit_is_respected(limit):
    result = build_persona_guidance(text="business report", state=PersonaState(), max_chars=limit)
    assert len(result.text) <= limit


def test_emotional_adjustments_match_their_declared_field_categories():
    route = route_persona_context("tired", state=PersonaState())
    assert set(route.trait_adjustments) <= {field.name for field in fields(PersonaTraits)}
    assert set(route.runtime_adjustments) <= {field.name for field in fields(PersonaRuntimeState)}
    assert route.runtime_adjustments["warmth"] == 0.15
    assert route.to_dict()["runtime_adjustments"] == {"warmth": 0.15}


def test_cli_preserves_explicit_zero_character_limit(monkeypatch, capsys):
    from types import SimpleNamespace
    import json
    from eimemory.persona import cli

    monkeypatch.setattr(cli, "PersonaStore", lambda unused: SimpleNamespace(load_state=PersonaState))
    parsed = SimpleNamespace(persona_command="guidance", text="business report", max_chars=0)
    assert cli.handle_persona_command(parsed, SimpleNamespace(store=None), {}) == 0
    assert json.loads(capsys.readouterr().out)["text"] == ""
