from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.ops.runtime_identity_drift import (
    CURRENT_SOURCE,
    PYTHON_RUNTIME_UNITS,
    REPORT_SCHEMA,
    current_runtime_identity_incident,
    detect_runtime_identity_drift,
    inspect_live_runtime_identity,
    runtime_identity_current_record_id,
    validate_runtime_identity_drift_report,
)


CURRENT = "a" * 40
STALE = "b" * 40
OWNER = ScopeRef(tenant_id="default", agent_id="test-owner", workspace_id="runtime-test", user_id="test-user")


def _runtime(root):
    store = RuntimeStore(root=root)
    return SimpleNamespace(store=store, close=store.close)


def _release(monkeypatch):
    monkeypatch.setattr("eimemory.governance.evidence_contract.current_release_identity",
                        lambda *_args, **_kwargs: SimpleNamespace(commit=CURRENT))


def _environment(unit: str) -> str:
    return f"EIMEMORY_RUNTIME_COMMIT={STALE if unit == 'eimemory-nightly.service' else CURRENT}"


def _inspect(runtime, *, at="2026-08-27T04:00:00Z", scope=OWNER, runner=_environment):
    return inspect_live_runtime_identity(runtime, scope=scope, detected_at=at, runner=runner)


def _stored_bytes(runtime, record_id):
    return runtime.store.read_consistent(lambda sqlite: tuple(sqlite.execute(
        "SELECT payload_json,payload_digest,payload_pointer_json FROM records "
        "WHERE record_id=? AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=?",
        (record_id, *asdict(OWNER).values()),
    ).fetchone()))


def test_detector_emits_exact_system_incident_for_stale_learning_runtime() -> None:
    report = detect_runtime_identity_drift(
        expected_commit=CURRENT,
        unit_environments={
            "eimemory-rpc.service": f"EIMEMORY_RUNTIME_COMMIT={CURRENT}",
            "eimemory-learn-watch.service": (
                f"PYTHONPATH=/opt/eimemory/current EIMEMORY_RUNTIME_COMMIT={STALE}"
            ),
        },
        detected_at="2026-08-27T04:00:00Z",
    )

    assert report["ok"] is False
    assert report["status"] == "drift_detected"
    assert report["origin"] == "system_detector"
    assert report["detector"] == "eimemory.runtime_identity_drift.v1"
    assert report["known_before_detection"] is False
    assert report["prior_user_reported"] is False
    assert report["manual_bootstrap"] is False
    assert report["observation_valid"] is True
    assert report["mismatches"] == [
        {
            "unit": "eimemory-learn-watch.service",
            "reason": "commit_mismatch",
            "observed_commit": STALE,
        }
    ]
    assert set(report["incident"]) == {
        "incident_id",
        "incident_digest",
        "incident_class",
        "title",
        "summary",
        "diagnostic_codes",
        "acceptance_requirements",
    }
    assert report["incident"]["incident_class"] == "deployment.runtime_commit_drift"


def test_detector_is_healthy_only_when_every_unit_has_one_current_commit() -> None:
    healthy = detect_runtime_identity_drift(
        expected_commit=CURRENT,
        unit_environments={
            "eimemory-rpc.service": f"EIMEMORY_RUNTIME_COMMIT={CURRENT}",
            "eimemory-nightly.service": f"EIMEMORY_RUNTIME_COMMIT={CURRENT}",
        },
        detected_at="2026-08-27T04:00:00Z",
    )
    missing = detect_runtime_identity_drift(
        expected_commit=CURRENT,
        unit_environments={"eimemory-nightly.service": "PYTHONPATH=/opt/eimemory/current"},
        detected_at="2026-08-27T04:00:00Z",
    )

    assert healthy["ok"] is True
    assert healthy["status"] == "current"
    assert healthy["incident"] is None
    assert missing["mismatches"][0]["reason"] == "runtime_commit_missing"


def test_live_detector_persists_one_idempotent_system_incident(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    scope = {
        "tenant_id": "default",
        "agent_id": "hongtu",
        "workspace_id": "embodied",
        "user_id": "darrow",
    }
    monkeypatch.setattr(
        "eimemory.governance.evidence_contract.current_release_identity",
        lambda *_args, **_kwargs: SimpleNamespace(commit=CURRENT),
    )

    def environment(unit: str) -> str:
        commit = STALE if unit == "eimemory-nightly.service" else CURRENT
        return f"EIMEMORY_RUNTIME_COMMIT={commit}"

    try:
        first = inspect_live_runtime_identity(
            runtime,
            scope=scope,
            detected_at="2026-08-27T05:00:00Z",
            runner=environment,
        )
        retry = inspect_live_runtime_identity(
            runtime,
            scope=scope,
            detected_at="2026-08-27T04:00:00Z",
            runner=environment,
        )
        incidents = runtime.store.list_records(kinds=["incident"], limit=10)
    finally:
        runtime.close()

    assert len(PYTHON_RUNTIME_UNITS) > 5
    assert first["incident_record_id"] == retry["incident_record_id"]
    assert len(incidents) == 1
    assert incidents[0].provenance["origin"] == "system_detector"
    assert incidents[0].meta["observation_valid"] is True


def test_inspect_live_runtime_identity_ignores_units_not_loaded_by_systemd(
    tmp_path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path / "runtime")
    scope = {
        "tenant_id": "default",
        "agent_id": "hongtu",
        "workspace_id": "embodied",
        "user_id": "darrow",
    }
    monkeypatch.setattr(
        "eimemory.governance.evidence_contract.current_release_identity",
        lambda *_args, **_kwargs: SimpleNamespace(commit=CURRENT),
    )

    def environment(unit: str) -> str | None:
        if unit == "eimemory-console.service":
            return None
        return f"EIMEMORY_RUNTIME_COMMIT={CURRENT}"

    try:
        report = inspect_live_runtime_identity(
            runtime,
            scope=scope,
            detected_at="2026-08-29T09:00:00Z",
            runner=environment,
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["status"] == "current"
    assert report["incident_record_id"] == ""


def test_v2_identity_is_stable_across_observation_time_and_environment_order() -> None:
    first = detect_runtime_identity_drift(expected_commit=CURRENT, scope=OWNER,
        unit_environments={"b.service": f"X=y EIMEMORY_RUNTIME_COMMIT={STALE}",
                           "a.service": "PYTHONPATH=/unused"}, detected_at="2026-08-27T04:00:00Z")
    later = detect_runtime_identity_drift(expected_commit=CURRENT.upper(), scope=asdict(OWNER),
        unit_environments={"a.service": "LANG=en", "b.service": f"EIMEMORY_RUNTIME_COMMIT={STALE.upper()} X=z"},
        detected_at="2026-08-28T09:00:00+01:00")
    assert first["schema"] == REPORT_SCHEMA
    assert first["incident"] == later["incident"]
    assert first["identity"] == later["identity"]
    assert first["detected_at"] != later["detected_at"]
    assert "detected_at" not in first["identity"]
    assert validate_runtime_identity_drift_report(first, scope=OWNER)
    assert validate_runtime_identity_drift_report(later, scope=OWNER)


@pytest.mark.parametrize("field", ["tenant_id", "agent_id", "workspace_id", "user_id"])
def test_v2_identity_binds_every_exact_owner_field(field) -> None:
    other = {**asdict(OWNER), field: "other"}
    first = detect_runtime_identity_drift(expected_commit=CURRENT, scope=OWNER,
        unit_environments={"a.service": f"EIMEMORY_RUNTIME_COMMIT={STALE}"}, detected_at="2026-08-27T04:00:00Z")
    second = detect_runtime_identity_drift(expected_commit=CURRENT, scope=other,
        unit_environments={"a.service": f"EIMEMORY_RUNTIME_COMMIT={STALE}"}, detected_at="2026-08-27T04:00:00Z")
    assert first["incident"]["incident_digest"] != second["incident"]["incident_digest"]
    assert not validate_runtime_identity_drift_report(first, scope=other)


@pytest.mark.parametrize("tamper", ["identity", "incident", "mismatches", "scope", "identity_digest", "time"])
def test_v2_validator_rejects_tampered_or_legacy_material(tamper) -> None:
    report = detect_runtime_identity_drift(expected_commit=CURRENT, scope=OWNER,
        unit_environments={"a.service": f"EIMEMORY_RUNTIME_COMMIT={STALE}"}, detected_at="2026-08-27T04:00:00Z")
    damaged = deepcopy(report)
    if tamper == "identity":
        damaged["identity"]["scope"]["agent_id"] = "foreign"
    elif tamper == "incident":
        damaged["incident"]["incident_digest"] = "f" * 64
    elif tamper == "mismatches":
        damaged["mismatches"].append(deepcopy(damaged["mismatches"][0]))
    elif tamper == "scope":
        damaged["scope"]["user_id"] = "foreign"
    elif tamper == "time":
        damaged["detected_at"] = "2026-08-27T04:00:00"
    else:
        damaged["identity_digest"] = "f" * 64
    assert not validate_runtime_identity_drift_report(damaged, scope=OWNER)
    legacy = detect_runtime_identity_drift(expected_commit=CURRENT,
        unit_environments={"a.service": f"EIMEMORY_RUNTIME_COMMIT={STALE}"}, detected_at="2026-08-27T04:00:00Z")
    assert legacy["schema"] == "runtime_identity_drift.v1"
    assert not validate_runtime_identity_drift_report(legacy, scope=OWNER)


def test_native_repeat_preserves_incident_bytes_and_first_origin_facts(tmp_path, monkeypatch) -> None:
    _release(monkeypatch)
    runtime = _runtime(tmp_path)
    try:
        first = _inspect(runtime)
        original = runtime.store.get_by_id(first["incident_record_id"], scope=OWNER, exact_scope=True)
        # A previously recorded first-observation fact must never be reset by a
        # new native observation; the router separately applies its trust gates.
        original.provenance.update(known_before_detection=True, prior_user_reported=True)
        runtime.store.rewrite(original)
        before = runtime.store.get_by_id(original.record_id, scope=OWNER, exact_scope=True).to_dict()
        raw_before = _stored_bytes(runtime, original.record_id)
        second = _inspect(runtime, at="2026-08-27T05:00:00Z")
        after = runtime.store.get_by_id(original.record_id, scope=OWNER, exact_scope=True).to_dict()
        assert second["incident_record_id"] == first["incident_record_id"]
        assert before == after
        assert raw_before == _stored_bytes(runtime, original.record_id)
        assert len(runtime.store.list_records(kinds=["incident"], limit=10)) == 1
        assert current_runtime_identity_incident(runtime.store, scope=OWNER, expected_commit=CURRENT) == first["incident"]["incident_digest"]
    finally:
        runtime.close()


def test_native_owner_isolation_does_not_borrow_shared_or_alias_incident(tmp_path, monkeypatch) -> None:
    _release(monkeypatch)
    runtime = _runtime(tmp_path)
    try:
        first = _inspect(runtime)
        other = ScopeRef(**{**asdict(OWNER), "agent_id": "other"})
        second = _inspect(runtime, scope=other)
        assert first["incident_record_id"] != second["incident_record_id"]
        assert len(runtime.store.list_records(kinds=["incident"], limit=10)) == 2
        unknown = ScopeRef(**{**asdict(OWNER), "agent_id": "unknown"})
        assert current_runtime_identity_incident(runtime.store, scope=unknown, expected_commit=CURRENT) is None
    finally:
        runtime.close()


def test_native_recovery_and_recurrence_leave_first_incident_immutable(tmp_path, monkeypatch) -> None:
    _release(monkeypatch)
    runtime = _runtime(tmp_path)
    try:
        first = _inspect(runtime)
        before = runtime.store.get_by_id(first["incident_record_id"], scope=OWNER, exact_scope=True).to_dict()
        healthy = _inspect(runtime, at="2026-08-27T05:00:00Z",
                           runner=lambda _unit: f"EIMEMORY_RUNTIME_COMMIT={CURRENT}")
        assert healthy["ok"] is True
        assert current_runtime_identity_incident(runtime.store, scope=OWNER, expected_commit=CURRENT) == ""
        # A slower old detector cannot resurrect the no-longer-current incident.
        stale = _inspect(runtime, at="2026-08-27T04:30:00Z")
        assert stale["reason"] == "runtime_identity_observation_superseded"
        assert current_runtime_identity_incident(runtime.store, scope=OWNER, expected_commit=CURRENT) == ""
        recurrence = _inspect(runtime, at="2026-08-27T06:00:00Z")
        assert recurrence["incident_record_id"] == first["incident_record_id"]
        assert before == runtime.store.get_by_id(first["incident_record_id"], scope=OWNER, exact_scope=True).to_dict()
        assert len(runtime.store.list_records(kinds=["incident"], limit=10)) == 1
    finally:
        runtime.close()


def test_native_changed_fault_retires_old_digest_without_rewriting_it(tmp_path, monkeypatch) -> None:
    _release(monkeypatch)
    runtime = _runtime(tmp_path)
    try:
        first = _inspect(runtime)
        second = _inspect(runtime, at="2026-08-27T05:00:00Z", runner=lambda unit: (
            "" if unit == "eimemory-rpc.service" else f"EIMEMORY_RUNTIME_COMMIT={CURRENT}"))
        assert first["incident_record_id"] != second["incident_record_id"]
        assert current_runtime_identity_incident(runtime.store, scope=OWNER, expected_commit=CURRENT) == second["incident"]["incident_digest"]
        assert len(runtime.store.list_records(kinds=["incident"], limit=10)) == 2
    finally:
        runtime.close()


def test_native_same_time_conflict_is_atomic_and_current_damage_fails_closed(tmp_path, monkeypatch) -> None:
    _release(monkeypatch)
    runtime = _runtime(tmp_path)
    try:
        first = _inspect(runtime)
        with pytest.raises(ValueError, match="same_time_observation_conflict"):
            _inspect(runtime, runner=lambda _unit: f"EIMEMORY_RUNTIME_COMMIT={CURRENT}")
        assert len(runtime.store.list_records(kinds=["incident"], limit=10)) == 1
        assert current_runtime_identity_incident(runtime.store, scope=OWNER, expected_commit=CURRENT) == first["incident"]["incident_digest"]
        pointer = runtime.store.get_by_id(runtime_identity_current_record_id(OWNER, CURRENT), scope=OWNER, exact_scope=True)
        pointer.content["report_digest"] = "f" * 64
        runtime.store.rewrite(pointer)
        assert current_runtime_identity_incident(runtime.store, scope=OWNER, expected_commit=CURRENT) is None
        with pytest.raises(ValueError, match="current_observation_conflict"):
            _inspect(runtime, at="2026-08-27T05:00:00Z")
    finally:
        runtime.close()


def test_native_equal_instant_different_timezone_spelling_is_idempotent(tmp_path, monkeypatch) -> None:
    _release(monkeypatch)
    runtime = _runtime(tmp_path)
    try:
        first = _inspect(runtime)
        second = _inspect(runtime, at="2026-08-27T05:00:00+01:00")
        assert first == second
        assert len(runtime.store.list_records(kinds=["incident"], limit=10)) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("target,field,value", [
    ("pointer", "source", "foreign.producer"),
    ("pointer", "status", "revoked"),
    ("incident", "source", "foreign.producer"),
    ("incident", "status", "revoked"),
])
def test_native_physical_projection_corruption_is_not_overwritten(tmp_path, monkeypatch, target, field, value) -> None:
    _release(monkeypatch)
    runtime = _runtime(tmp_path)
    try:
        first = _inspect(runtime)
        record_id = first["current_observation_record_id" if target == "pointer" else "incident_record_id"]
        # Deliberately corrupt only an isolated scratch DB's physical projection.
        def corrupt(sqlite):
            sqlite.execute(f"UPDATE records SET {field}=? WHERE record_id=?", (value, record_id))
            return None, [], []
        runtime.store.mutate_records_atomically(corrupt)
        raw_before = _stored_bytes(runtime, record_id)
        with pytest.raises((ValueError, RuntimeError)):
            _inspect(runtime, at="2026-08-27T05:00:00Z")
        assert raw_before == _stored_bytes(runtime, record_id)
        projection = runtime.store.read_consistent(lambda sqlite: sqlite.execute(
            f"SELECT {field} FROM records WHERE record_id=?", (record_id,)).fetchone()[0])
        assert projection == value
    finally:
        runtime.close()


def test_native_incident_identity_collision_rolls_back_current_projection(tmp_path, monkeypatch) -> None:
    _release(monkeypatch)
    runtime = _runtime(tmp_path)
    try:
        first = _inspect(runtime)
        record = runtime.store.get_by_id(first["incident_record_id"], scope=OWNER, exact_scope=True)
        record.content["title"] = "untrusted replacement"
        runtime.store.rewrite(record)
        pointer_before = runtime.store.get_by_id(first["current_observation_record_id"], scope=OWNER, exact_scope=True).to_dict()
        with pytest.raises(ValueError, match="incident_identity_conflict"):
            _inspect(runtime, at="2026-08-27T05:00:00Z")
        assert pointer_before == runtime.store.get_by_id(first["current_observation_record_id"], scope=OWNER, exact_scope=True).to_dict()
    finally:
        runtime.close()


def test_native_two_processes_create_one_incident_across_timestamps(tmp_path, monkeypatch) -> None:
    _release(monkeypatch)
    root = tmp_path / "store"
    runtime = _runtime(root)
    runtime.close()
    start = tmp_path / "start"
    script = '''
import json, sys, time
from pathlib import Path
from types import SimpleNamespace
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.ops.runtime_identity_drift import inspect_live_runtime_identity
import eimemory.governance.evidence_contract as contract
contract.current_release_identity = lambda *_a, **_k: SimpleNamespace(commit="a"*40)
store = RuntimeStore(root=Path(sys.argv[1]))
try:
    Path(sys.argv[4]).write_text("ready")
    deadline = time.monotonic() + 20
    while not Path(sys.argv[5]).exists():
        if time.monotonic() > deadline:
            raise TimeoutError("test barrier")
        time.sleep(.01)
    report = inspect_live_runtime_identity(SimpleNamespace(store=store), scope=json.loads(sys.argv[3]),
        detected_at=sys.argv[2], runner=lambda unit: "EIMEMORY_RUNTIME_COMMIT=" + ("b"*40 if unit == "eimemory-nightly.service" else "a"*40))
    print(json.dumps({"id":report.get("incident_record_id"), "status":report["status"]}))
finally:
    store.close()
'''
    children = []
    try:
        for number, moment in enumerate(("2026-08-27T04:00:00Z", "2026-08-27T05:00:00Z")):
            ready = tmp_path / f"ready-{number}"
            child = subprocess.Popen([sys.executable, "-B", "-c", script, str(root), moment,
                json.dumps(asdict(OWNER)), str(ready), str(start)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            children.append(child)
        deadline = time.monotonic() + 25
        while not all((tmp_path / f"ready-{number}").exists() for number in range(2)):
            assert all(child.poll() is None for child in children), "child failed before barrier"
            assert time.monotonic() < deadline, "children did not reach test barrier"
            time.sleep(.01)
        start.write_text("go")
        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            assert child.returncode == 0, stderr
            results.append(json.loads(stdout))
        assert results[0]["id"] == results[1]["id"]
        assert all(result["status"] == "drift_detected" for result in results)
        runtime = _runtime(root)
        try:
            assert len(runtime.store.list_records(kinds=["incident"], limit=10)) == 1
            pointers = runtime.store.list_records_by_sources_exact_scope(
                scope=OWNER, sources=[CURRENT_SOURCE], kinds=["reflection"], limit=10)
            assert len(pointers) == 1
            assert pointers[0].content["report"]["detected_at"] == "2026-08-27T05:00:00Z"
        finally:
            runtime.close()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
