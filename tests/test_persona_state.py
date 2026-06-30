from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.persona.schema import IMMUTABLE_BOUNDARIES, PersonaState
from eimemory.persona.state import apply_trait_delta, default_persona_state
from eimemory.persona.store import PersonaStore


def test_default_persona_state_keeps_hard_boundaries() -> None:
    state = default_persona_state()

    assert state.identity == "hongtu"
    assert state.boundaries.no_fake_consciousness_claims is True
    assert state.boundaries.no_plaintext_secret_storage is True
    assert state.traits.safety >= 0.8
    assert state.traits.verbosity < 0.5
    assert state.to_dict()["boundaries"]["no_fake_consciousness_claims"] is True


def test_trait_updates_are_clamped_and_cannot_disable_immutable_boundaries() -> None:
    state = default_persona_state()
    state.boundaries.no_fake_consciousness_claims = False

    updated = apply_trait_delta(state, {"verbosity": 2.0, "safety": -2.0})

    assert updated.traits.verbosity == 1.0
    assert updated.traits.safety == 0.8
    for key, expected in IMMUTABLE_BOUNDARIES.items():
        assert getattr(updated.boundaries, key) is expected


def test_persona_store_persists_state_snapshot_and_reload(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    store = PersonaStore(runtime.store)
    state = default_persona_state()
    state.relationship.user_name = "darrow"

    saved = store.save_state(state, scope={"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"})
    loaded = store.load_state()

    assert isinstance(loaded, PersonaState)
    assert loaded.relationship.user_name == "darrow"
    assert saved.source == "persona.state_snapshot"
    assert saved.kind == "reflection"
    assert (tmp_path / "state" / "persona_state.json").exists()


def test_persona_store_clamps_corrupted_state_on_load(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    store = PersonaStore(runtime.store)
    state_path = tmp_path / "state" / "persona_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        """
{
  "identity": "hongtu",
  "version": 1,
  "traits": {"verbosity": 5, "safety": -1, "precision": 2},
  "runtime_state": {"confidence": 2, "risk_alert": -1},
  "relationship": {"trust_level": 2, "familiarity": -1},
  "boundaries": {"no_fake_consciousness_claims": false, "no_plaintext_secret_storage": false}
}
""".strip(),
        encoding="utf-8",
    )

    loaded = store.load_state()

    assert loaded.traits.verbosity == 1.0
    assert loaded.traits.precision == 1.0
    assert loaded.traits.safety == 0.8
    assert loaded.runtime_state.confidence == 1.0
    assert loaded.runtime_state.risk_alert == 0.0
    assert loaded.relationship.trust_level == 1.0
    assert loaded.relationship.familiarity == 0.0
    assert loaded.boundaries.no_fake_consciousness_claims is True
    assert loaded.boundaries.no_plaintext_secret_storage is True
