from __future__ import annotations

import json
import os
import pytest
import subprocess
import sys

from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef
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


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("\u592a\u5570\u55e6\uff0c\u4e0b\u6b21\u76f4\u63a5\u8bf4\u7ed3\u679c", "verbosity"),
        ("\u4e0d\u9519\uff0c\u8fd9\u6837\u56de\u590d\u5c31\u5f88\u597d", "reinforcement"),
        ("\u4e0d\u5bf9\uff0c\u4f60\u5f97\u5148\u67e5\u8bc1\u636e", "correctness"),
    ],
)
def test_openclaw_user_persona_feedback_records_correction(tmp_path, text: str, category: str) -> None:
    runtime = Runtime.create(root=tmp_path)

    result = OpenClawMemoryHooks(runtime).on_message_received(
        {
            "session_id": "sess-persona-feedback",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "message": {"role": "user", "content": text},
        }
    )

    assert result["persona_feedback"]["stored"] is not None
    assert result["persona_feedback"]["category"] == category
    records = runtime.store.list_records(
        kinds=["feedback"],
        scope=ScopeRef.from_dict({"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}),
        limit=10,
    )
    corrections = [record for record in records if record.source == "persona.correction"]
    assert corrections
    assert corrections[0].content["category"] == category


def test_openclaw_regular_user_message_does_not_record_persona_feedback(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    result = OpenClawMemoryHooks(runtime).on_message_received(
        {
            "session_id": "sess-persona-feedback-none",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "message": {"role": "user", "content": "帮我看一下仓库状态"},
        }
    )

    assert result["persona_feedback"] is None
    records = runtime.store.list_records(
        kinds=["feedback"],
        scope=ScopeRef.from_dict({"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}),
        limit=10,
    )
    assert [record for record in records if record.source == "persona.correction"] == []


def test_cli_openclaw_hook_reads_utf8_persona_feedback_from_pipe(tmp_path) -> None:
    text = "".join(
        chr(code)
        for code in [0x592A, 0x5570, 0x55E6, 0x4E86, 0xFF0C, 0x4E0B, 0x6B21, 0x76F4, 0x63A5, 0x8BF4, 0x7ED3, 0x679C]
    )
    payload = {
        "session_id": "sess-persona-feedback-cli",
        "agent_id": "main",
        "workspace_id": "repo-x",
        "user_id": "darrow",
        "message": {"role": "user", "content": text},
    }
    env = os.environ.copy()
    env["EIMEMORY_ROOT"] = str(tmp_path / "runtime")

    result = subprocess.run(
        [sys.executable, "-m", "eimemory.cli.main", "openclaw-hook", "message_received"],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        env=env,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    output = json.loads(result.stdout.decode("utf-8"))
    assert output["persona_feedback"]["category"] == "verbosity"
    assert output["persona_feedback"]["stored"]["source"] == "persona.correction"
