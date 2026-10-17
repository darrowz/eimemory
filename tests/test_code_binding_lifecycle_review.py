"""Independent lifecycle boundary regressions for provider replacement."""
from dataclasses import replace

import pytest

from eimemory.adapters.runtime.capability import AdapterCapabilityService
from eimemory.api.runtime import Runtime
import eimemory.capabilities.code_implementation_bootstrap as bootstrap
from eimemory.ops.code_implementation_owner import PRODUCTION_RUNTIME_SCOPE as SCOPE


STAMP = "2026-08-23T00:00:00Z"
EXPIRY = "2026-08-23T01:00:00Z"


class HealthyClient:
    def health(self, *, nonce):
        binding = bootstrap.code_implementation_binding()
        return {"ok": True, "operation": "health", "nonce": nonce,
                "provider_instance_id": binding.provider_instance_id,
                "implementation_digest": binding.implementation_digest}


@pytest.fixture
def registered(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "CodeImplementationSocketClient", HealthyClient)
    with Runtime.create(root=tmp_path) as runtime:
        runtime.apply_capability_seed_manifest(scope=SCOPE)
        assert bootstrap.register_code_implementation_v2(runtime, runtime_scope=SCOPE)["ok"]
        yield runtime


def context(runtime, binding_id):
    return runtime.capabilities.binding_context(binding_id, runtime_scope=SCOPE, capability_scope="global")


def transition(runtime, binding_id, status, *, replacement=False):
    row = context(runtime, binding_id)
    return runtime.capabilities.transition_status(entity_type="binding", entity_id=binding_id,
        entity_digest=row["entity_digest"], target_status=status, runtime_scope=SCOPE,
        capability_scope="global", expected_state_version=row["state_version"],
        expected_state_digest=row["state_digest"], effective_at=bootstrap.now_iso(),
        reason="provider_instance_replaced" if replacement else "operator_lifecycle",
        provenance={"source": "eimemory.code_implementation_bootstrap" if replacement else "operator"})


def advertise(runtime):
    return bootstrap.advertise_code_implementation_v2(runtime, runtime_scope=SCOPE,
        advertised_at=STAMP, expires_at=EXPIRY)


def test_rejected_advertisement_does_not_reactivate_previously_replaced_binding(registered, monkeypatch):
    runtime = registered
    current = bootstrap.code_implementation_binding()
    transition(runtime, current.binding_id, "stale", replacement=True)
    before = context(runtime, current.binding_id)
    monkeypatch.setattr(AdapterCapabilityService, "advertise_capabilities",
        lambda *args, **kwargs: {"ok": False, "status": "rejected", "reason": "advertisement_schema_invalid"})
    assert advertise(runtime)["ok"] is False
    after = context(runtime, current.binding_id)
    assert after["status"] == "stale"
    assert after["state_digest"] == before["state_digest"]


@pytest.mark.parametrize("field,bad_value", [
    ("ok", False),
    ("nonce", "wrong-nonce"),
    ("provider_instance_id", "another-provider"),
    ("implementation_digest", "0" * 64),
])
def test_health_must_attest_exact_binding_before_any_lifecycle_or_advertisement(
        registered, monkeypatch, field, bad_value):
    runtime = registered
    current = bootstrap.code_implementation_binding()
    previous = replace(current, binding_id="binding.health-mismatch", implementation_digest="a" * 64)
    runtime.capabilities.bind(previous, runtime_scope=SCOPE)
    transition(runtime, current.binding_id, "stale", replacement=True)
    before_current = context(runtime, current.binding_id)
    before_previous = context(runtime, previous.binding_id)
    class MismatchedHealthClient(HealthyClient):
        def health(self, *, nonce):
            return {**super().health(nonce=nonce), field: bad_value}
    monkeypatch.setattr(bootstrap, "CodeImplementationSocketClient", MismatchedHealthClient)
    advertisement_calls = []
    monkeypatch.setattr(AdapterCapabilityService, "advertise_capabilities",
        lambda *args, **kwargs: advertisement_calls.append(True) or {"ok": True})
    report = advertise(runtime)
    assert report["ok"] is False and report["reason"] == "provider_health_binding_mismatch"
    assert not advertisement_calls
    assert context(runtime, current.binding_id)["state_digest"] == before_current["state_digest"]
    assert context(runtime, previous.binding_id)["state_digest"] == before_previous["state_digest"]


@pytest.mark.parametrize("operator_status", ["stale", "disabled", "deprecated", "quarantined"])
def test_replacement_preserves_operator_lifecycle_on_previous_digest(registered, operator_status):
    runtime = registered
    current = bootstrap.code_implementation_binding()
    previous = replace(current, binding_id="binding.operator-lifecycle", implementation_digest="a" * 64)
    runtime.capabilities.bind(previous, runtime_scope=SCOPE)
    transition(runtime, previous.binding_id, operator_status)
    before = context(runtime, previous.binding_id)
    assert advertise(runtime)["ok"] is True
    assert context(runtime, previous.binding_id)["state_digest"] == before["state_digest"]


def test_concurrent_operator_transition_is_not_overwritten_by_replacement(registered, monkeypatch):
    runtime = registered
    current = bootstrap.code_implementation_binding()
    previous = replace(current, binding_id="binding.concurrent-operator", implementation_digest="a" * 64)
    runtime.capabilities.bind(previous, runtime_scope=SCOPE)
    original_transition = runtime.capabilities.transition_status
    raced = False
    def competing_transition(**kwargs):
        nonlocal raced
        if kwargs["entity_id"] == previous.binding_id and kwargs["target_status"] == "stale" and not raced:
            raced = True
            # An operator changes the same state after replacement read it.
            transition(runtime, previous.binding_id, "disabled")
        return original_transition(**kwargs)
    monkeypatch.setattr(runtime.capabilities, "transition_status", competing_transition)
    assert advertise(runtime)["ok"] is False
    assert raced
    assert context(runtime, previous.binding_id)["status"] == "disabled"


def test_overflowing_previous_binding_window_blocks_before_advertisement_write(registered, monkeypatch):
    runtime = registered
    current = bootstrap.code_implementation_binding()
    for index in range(101):
        runtime.capabilities.bind(replace(current, binding_id=f"binding.previous-window.{index}",
            implementation_digest=f"{index + 1:064x}"), runtime_scope=SCOPE)
    called = False
    def unexpected_advertisement(*args, **kwargs):
        nonlocal called
        called = True
        return {"ok": True}
    monkeypatch.setattr(AdapterCapabilityService, "advertise_capabilities", unexpected_advertisement)
    assert advertise(runtime)["ok"] is False
    assert called is False
    assert context(runtime, "binding.previous-window.0")["status"] == "active"
