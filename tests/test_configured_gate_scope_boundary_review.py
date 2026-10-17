"""Independent adversarial review of the shared configured-gate scope owner."""
from copy import deepcopy

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.scheduler.memory_benchmark_context import resolve_production_recall_evaluation_scope
from test_logical_scope_pin_alias import HOST, LEGACY, SCOPE, bindings, isolate_identity, legacy_pin


def dataset_for(scope):
    # These metadata claims are deliberately untrusted. Only the private pin
    # and the caller can authorize the shared resolver's returned identity.
    return {
        "scope": deepcopy(scope),
        "operator_context": {"ok": True, "scope": deepcopy(scope),
                             "resolution": {"channel_source": "operator_binding", "matching_channel_count": 1}},
        "scope_resolution": {"rewritten": True},
        "formal_business_evidence": True,
    }


@pytest.mark.parametrize("field", ["tenant_id", "agent_id", "workspace_id", "user_id"])
def test_dataset_claims_never_extend_a_real_private_pin(tmp_path, monkeypatch, field):
    bindings(tmp_path, monkeypatch, [legacy_pin()])
    caller = deepcopy(SCOPE)
    declared = resolve_channel_scope("hermes", SCOPE)
    declared[field] = "private-other-" + field
    dataset = dataset_for(declared)
    original = deepcopy((caller, dataset))

    actual = resolve_production_recall_evaluation_scope(scope=caller, dataset=dataset)

    assert actual == SCOPE
    assert (caller, dataset) == original


@pytest.mark.parametrize("field", ["tenant_id", "agent_id", "workspace_id", "user_id"])
def test_private_pin_cannot_be_borrowed_by_another_caller(tmp_path, monkeypatch, field):
    bindings(tmp_path, monkeypatch, [legacy_pin()])
    caller = {**SCOPE, field: "unrelated-" + field}
    dataset = dataset_for(resolve_channel_scope("hermes", SCOPE))

    assert resolve_production_recall_evaluation_scope(scope=caller, dataset=dataset) == caller


@pytest.mark.parametrize("boundary", ["missing", "ambiguous", "legacy_dataset", "explicit_channel", "explicit_user"])
def test_untrusted_claims_do_not_replace_missing_or_different_authority(tmp_path, monkeypatch, boundary):
    pins = [legacy_pin()]
    caller = deepcopy(SCOPE)
    declared = resolve_channel_scope("hermes", SCOPE)
    if boundary == "ambiguous":
        pins.append(resolve_channel_scope("codex", SCOPE))
    elif boundary == "legacy_dataset":
        declared["agent_id"] = LEGACY
    elif boundary == "explicit_channel":
        caller = resolve_channel_scope("codex", SCOPE)
    elif boundary == "explicit_user":
        caller["user_id"] = "explicit-private-user"
    if boundary != "missing":
        bindings(tmp_path, monkeypatch, pins)

    assert resolve_production_recall_evaluation_scope(scope=caller, dataset=dataset_for(declared)) == caller


@pytest.mark.parametrize("caller", [SCOPE, HOST])
def test_one_exact_private_pin_aligns_only_its_channel_without_mutating_inputs(tmp_path, monkeypatch, caller):
    bindings(tmp_path, monkeypatch, [legacy_pin()])
    exact = resolve_channel_scope("hermes", SCOPE)
    request = deepcopy(caller)
    dataset = dataset_for(exact)
    original = deepcopy((request, dataset))

    assert resolve_production_recall_evaluation_scope(scope=request, dataset=dataset) == exact
    assert (request, dataset) == original
