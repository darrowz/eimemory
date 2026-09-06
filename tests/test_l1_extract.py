from eimemory.knowledge.sediment import extract_l1_atoms
from eimemory.llm.command_client import LLMResult
from eimemory.llm.hermes_adapter import hermes_llm_argv


def test_l1_extract_skips_chatter() -> None:
    assert extract_l1_atoms(user_text="你好") == []
    assert extract_l1_atoms(user_text="这次帮我翻译一下") == []
    assert extract_l1_atoms(user_text="eimemory现在情况怎么样") == []


def test_l1_extract_instruction_from_standing_rule() -> None:
    atoms = extract_l1_atoms(user_text="以后回答先给结论，少解释。")
    assert len(atoms) == 1
    assert atoms[0].memory_type == "instruction"
    assert "结论" in atoms[0].text


def test_l1_extract_persona_from_style_preference() -> None:
    atoms = extract_l1_atoms(user_text="沟通风格要极简直接，讨厌废话。")
    assert len(atoms) == 1
    assert atoms[0].memory_type in {"persona", "instruction"}
    assert atoms[0].semantic_key.startswith("sk:")


def test_hermes_l1_argv_does_not_pin_model() -> None:
    argv = hermes_llm_argv()
    assert "-m" not in argv
    assert "--model" not in argv
    assert "--provider" not in argv
    assert "--safe-mode" in argv


class _FakeLLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def complete(self, *, system_prompt: str, user_prompt: str, json_mode: bool = False):
        del system_prompt, user_prompt, json_mode
        self.calls += 1
        return LLMResult(text=self.text, provider_id="hermes", model_id="hermes/configured")


def test_l1_extract_uses_injected_llm_and_does_not_name_a_vendor_model() -> None:
    fake = _FakeLLM(
        '[{"scene_name":"x","message_ids":["m1"],"memories":[{"content":"用户要求 AI 以后先给结论。","type":"instruction","priority":95}]}]'
    )
    atoms = extract_l1_atoms(
        user_text="以后回答先给结论，少解释。",
        llm=fake,
        use_llm=True,
    )
    assert fake.calls == 1
    assert len(atoms) == 1
    assert atoms[0].memory_type == "instruction"
    assert "结论" in atoms[0].text


def test_l1_extract_llm_empty_does_not_fall_back_to_heuristic() -> None:
    fake = _FakeLLM("[]")
    atoms = extract_l1_atoms(
        user_text="以后回答先给结论，少解释。",
        llm=fake,
        use_llm=True,
        fallback_heuristic=False,
    )
    assert fake.calls == 1
    assert atoms == []
