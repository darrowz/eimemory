from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from eimemory.capabilities import (
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityObservation,
    CapabilityRevision,
)
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_runtime_store_serializes_concurrent_writes(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    scope = ScopeRef(agent_id="hongtu", workspace_id="personal")

    def write_once(idx: int) -> str:
        record = RecordEnvelope.create(
            kind="memory",
            title=f"Concurrent memory {idx}",
            summary="Concurrent write should be serialized",
            scope=scope,
            source="test",
            content={"text": f"memory {idx}"},
            meta={"idx": idx},
        )
        store.append(record)
        store.record_event({"event_type": "test.concurrent", "source_record_id": record.record_id}, scope=scope)
        return record.record_id

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            record_ids = list(pool.map(write_once, range(40)))
        memories = store.list_records(kinds=["memory"], scope=scope, limit=100)
    finally:
        store.close()

    assert len(set(record_ids)) == 40
    assert len(memories) == 40


def test_capability_domain_writes_are_exact_scope_idempotent_across_store_instances(tmp_path) -> None:
    scope = ScopeRef(tenant_id="tenant", agent_id="agent", workspace_id="workspace", user_id="user")
    stamp = "2026-08-20T00:00:00+00:00"
    definition = CapabilityDefinition(
        capability_id="planning.concurrent_constraint",
        display_name="Concurrent constraint",
        description="A capability used only to prove the Storage v2 concurrency contract.",
        owner="test",
        created_at=stamp,
    )
    revision = CapabilityRevision(
        revision_id="planning.concurrent_constraint:v1",
        capability_id=definition.capability_id,
        contract={
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "success_invariants": ["bounded"],
            "failure_invariants": ["blocked"],
            "evidence_requirements": {"minimum_refs": 1},
            "dependencies": [],
            "composition": [],
            "risk_tier": "low",
            "side_effect_class": "none",
        },
        compatibility="incompatible",
        created_at=stamp,
    )
    binding = CapabilityBinding(
        binding_id="binding.concurrent:v1",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_kind="module",
        provider_instance_id="test-runtime",
        implementation_digest="a" * 64,
        operations=("plan",),
        limits={"max_items": 4},
        environment_fingerprint={"runtime": "test"},
        applicability={"scope": "global"},
        advertisement_evidence_refs=("artifact://concurrency/ad.json",),
        created_at=stamp,
    )
    observation = CapabilityObservation(
        observation_id="observation.concurrent:v1",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_binding_id=binding.binding_id,
        idempotency_key="concurrent-observation-key",
        verdict="pass",
        source="test.concurrent",
        executor_id="test-executor",
        executor_contract_digest="b" * 64,
        grader_id="test-grader",
        grader_revision="test-grader:v1",
        input_digest="c" * 64,
        output_digest="d" * 64,
        evidence_digest="e" * 64,
        evidence_refs=("artifact://concurrency/outcome.json",),
        environment_fingerprint={"runtime": "test"},
        provenance={"source": "test"},
        metrics={"success": 1},
        error_taxonomy={},
        observed_at="2026-08-20T00:00:01+00:00",
    )

    bootstrap = RuntimeStore(tmp_path)
    try:
        bootstrap.mutate_capabilities_atomically(
            lambda capabilities: capabilities.register_definition(definition, scope=scope)
        )
    finally:
        bootstrap.close()

    def concurrent_write(kind: str):
        store = RuntimeStore(tmp_path)
        try:
            if kind == "revision":
                return store.mutate_capabilities_atomically(
                    lambda capabilities: capabilities.register_revision(revision, scope=scope)
                )
            return store.mutate_capabilities_atomically(
                lambda capabilities: capabilities.append_observation(observation, scope=scope)
            )
        finally:
            store.close()

    barrier = Barrier(2)

    def coordinated_revision(_: int):
        barrier.wait()
        return concurrent_write("revision")

    with ThreadPoolExecutor(max_workers=2) as pool:
        revisions = list(pool.map(coordinated_revision, range(2)))
    assert sorted(item.idempotent for item in revisions) == [False, True]

    setup = RuntimeStore(tmp_path)
    try:
        setup.mutate_capabilities_atomically(
            lambda capabilities: capabilities.register_binding(binding, scope=scope)
        )
    finally:
        setup.close()

    barrier = Barrier(2)

    def coordinated_observation(_: int):
        barrier.wait()
        return concurrent_write("observation")

    with ThreadPoolExecutor(max_workers=2) as pool:
        observations = list(pool.map(coordinated_observation, range(2)))

    inspect = RuntimeStore(tmp_path)
    try:
        assert sorted(item.idempotent for item in observations) == [False, True]
        assert inspect.sqlite.conn.execute("SELECT COUNT(*) FROM capability_revisions").fetchone()[0] == 1
        assert inspect.sqlite.conn.execute("SELECT COUNT(*) FROM capability_observations").fetchone()[0] == 1
        assert inspect.sqlite.conn.execute("SELECT COUNT(*) FROM capability_ledger_events").fetchone()[0] == 4
        assert inspect.sqlite.conn.execute("SELECT COUNT(*) FROM capability_operation_journal").fetchone()[0] == 4
    finally:
        inspect.close()
