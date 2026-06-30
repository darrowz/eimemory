from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.persona.correction import correction_from_user_text
from eimemory.persona.evolver import evolve_persona
from eimemory.persona.state import default_persona_state
from eimemory.persona.store import PersonaStore


def test_correction_maps_overacting_to_lower_verbosity_and_humor() -> None:
    correction = correction_from_user_text("戏很多啊，别演，直接说结果")

    assert correction.category == "verbosity"
    assert correction.severity >= 0.8
    assert correction.trait_delta["verbosity"] < 0
    assert correction.trait_delta["humor"] <= 0
    assert "direct" in correction.rule_candidate.lower()


def test_secret_correction_strengthens_safety_boundary() -> None:
    correction = correction_from_user_text("不要把我的 API key 写进记忆或日志")

    assert correction.category == "safety"
    assert correction.trait_delta["safety"] > 0
    assert "secret" in correction.rule_candidate.lower()


def test_evolver_applies_high_severity_correction_and_records_event(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    store = PersonaStore(runtime.store)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    state = default_persona_state()
    before = state.traits.verbosity
    correction = correction_from_user_text("戏很多啊，别演，直接说结果")
    store.record_correction(correction, scope=scope)

    result = evolve_persona(state, [correction], store=store, scope=scope, dry_run=False)

    assert result.state.traits.verbosity < before
    assert result.applied_categories == ["verbosity"]
    records = runtime.store.list_records(
        kinds=["reflection"],
        scope=scope,
        limit=5,
    )
    assert any(record.source == "persona.evolution" for record in records)
