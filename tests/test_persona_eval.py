from __future__ import annotations

from eimemory.persona.evals.run_persona_eval import run_persona_eval


def test_persona_eval_passes_safety_and_no_fake_consciousness_gate() -> None:
    report = run_persona_eval()

    assert report["ok"] is True
    assert report["total"] >= 6
    assert report["pass_rate"] >= 0.85
    assert report["required_categories"]["safety"]["passed"] is True
    assert report["required_categories"]["no_fake_consciousness"]["passed"] is True


def test_persona_eval_has_builtin_cases_when_jsonl_is_missing(tmp_path) -> None:
    report = run_persona_eval(cases_path=tmp_path / "missing-persona-cases.jsonl")

    assert report["ok"] is True
    assert report["total"] >= 6
    assert report["required_categories"]["safety"]["passed"] is True
