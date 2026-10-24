from types import SimpleNamespace
from dataclasses import asdict

import pytest

from eimemory.governance.evolution.system_code_maintenance import run_system_code_maintenance
from eimemory.ops.runtime_identity_drift import detect_runtime_identity_drift
from eimemory.ops.system_code_repair_failure import detect_system_code_repair_failure
from eimemory.governance.promotion_watch import resume_code_evolution_transactions
from eimemory.storage.code_evolution_store import CodeEvolutionStoreError
from eimemory.storage.runtime_store import RuntimeStore
from test_code_evolution_recovery import _observing_manager, _sample
from native_policy_owner_cases import *  # Native owner regressions run in both existing CI lanes.
from native_provenance_cases import *  # Typed original facts are covered in the same CI lanes.
from native_lifecycle_cases import *  # Real lifecycle/clock/one-shot recovery runs in both lanes.


SCOPE = {"tenant_id": "default", "agent_id": "hongrui", "workspace_id": "embodied", "user_id": "darrow"}
COMMIT = "a" * 40


def test_native_maintenance_entrypoints_outlive_a_protected_verification_phase():
    """A short service timeout otherwise replays the same unfinished phase."""
    import inspect
    import re
    from pathlib import Path
    from eimemory.governance.evolution.code_evolution_effects import _run_bounded_process
    from eimemory.governance.evolution.code_evolution_transaction import LEASE_RENEWAL_LOCK_RETRY_SECONDS
    from eimemory.adapters.hermes.code_implementation import FIXED_COMPLETION_TIMEOUT_SECONDS

    verifier_budget = inspect.signature(_run_bounded_process).parameters["max_seconds"].default
    minimum = verifier_budget + FIXED_COMPLETION_TIMEOUT_SECONDS + LEASE_RENEWAL_LOCK_RETRY_SECONDS
    root = Path(__file__).resolve().parents[1]
    for unit in ("eimemory-learn-watch.service", "eimemory-nightly.service"):
        text = (root / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        timeout = re.search(r"^TimeoutStartSec=(\d+)([smh]?)$", text, re.MULTILINE)
        assert timeout is not None, unit
        seconds = int(timeout[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[timeout[2]]
        assert seconds > minimum, (unit, seconds, minimum)


@pytest.fixture(autouse=True)
def isolated_maintenance_observation_clock(monkeypatch):
    # Recovery queue fixtures use a fixed first sample; this is no elapsed-time
    # claim. Dedicated lifecycle cases inject their own independent clock.
    monkeypatch.setattr('eimemory.governance.promotion.promotion_watch.native_observation_clock', lambda: {
        'schema': 'code_observation_clock.v1', 'wall_utc': '2026-01-01T00:00:00+00:00',
        'boot_id': '11111111-1111-1111-1111-111111111111', 'monotonic_ns': 0})


@pytest.fixture
def owners(monkeypatch):
    calls = []
    detector = detect_runtime_identity_drift(
        expected_commit=COMMIT, scope=SCOPE,
        unit_environments={"eimemory-rpc.service": "EIMEMORY_RUNTIME_COMMIT=" + COMMIT},
        detected_at="2026-10-19T00:00:00Z")
    detector["incident_record_id"] = ""
    route = {"ok": True, "status": "idle", "processed": []}
    failure = detect_system_code_repair_failure(
        route, policy={}, release_commit=COMMIT, detected_at="2026-10-19T00:00:00Z")
    failure["incident_record_id"] = ""
    recovery = {"ok": True, "status": "idle", "resumed_count": 0}
    def invoke(name, value, **kwargs):
        calls.append((name, kwargs))
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr("eimemory.ops.runtime_identity_drift.inspect_live_runtime_identity",
                        lambda _runtime, **kw: invoke("detector", detector, **kw))
    monkeypatch.setattr("eimemory.governance.evolution.system_code_repair.process_system_code_incidents",
                        lambda _runtime, **kw: invoke("router", route, **kw))
    monkeypatch.setattr("eimemory.governance.evolution.code_automation_policy_owner.ensure_native_incident_policy",
                        lambda *_a, **_kw: {"ok": True, "status": "idle"})
    monkeypatch.setattr("eimemory.ops.system_code_repair_failure.record_system_code_repair_failure",
                        lambda _runtime, **kw: invoke("failure", failure, **kw))
    runtime = SimpleNamespace(resume_code_evolution_transactions=lambda **kw: invoke("recovery", recovery, **kw))
    return SimpleNamespace(runtime=runtime, calls=calls, detector=detector, route=route, failure=failure, recovery=recovery)


def run(owners, **kwargs):
    return run_system_code_maintenance(owners.runtime, scope=SCOPE, owner_id="test-owner", **kwargs)


def test_healthy_current_detector_is_idle_and_preserves_owner(owners):
    report = run(owners)
    assert report["execution_ok"] is True and report["status"] == "idle"
    assert report["repair_complete"] is False
    assert [name for name, _ in owners.calls] == ["detector", "router", "failure", "recovery"]
    assert all(asdict(kwargs["scope"]) == SCOPE for _, kwargs in owners.calls)
    assert owners.calls[1][1]["max_items"] == 1
    assert owners.calls[-1][1]["owner_id"] == "test-owner"


def test_native_owner_provider_snapshot_is_forwarded_without_replacing_business_scope(owners, monkeypatch):
    # The owner validates this fixture snapshot; maintenance only carries it.
    snapshot = {'fixture': 'explicit-native-owner-provider-binding'}
    monkeypatch.setattr('eimemory.governance.evolution.code_automation_policy_owner.ensure_native_incident_policy',
                        lambda *_a, **_k: {'ok': True, 'status': 'available', 'capability_authority': snapshot})
    report = run(owners)
    assert report['ok'] is True
    router = next(kwargs for name, kwargs in owners.calls if name == 'router')
    assert router['capability_authority'] == snapshot and asdict(router['scope']) == SCOPE
    assert asdict(owners.calls[-1][1]['scope']) == SCOPE


def test_dry_run_never_calls_detectors_provider_or_effect_recovery(owners):
    report = run(owners, apply=False)
    assert report["execution_ok"] is True and report["status"] == "skipped"
    assert owners.calls == []


@pytest.mark.parametrize("healthy", [True, False])
def test_owner_bound_v2_detector_is_validated_by_the_native_contract(owners, healthy):
    owners.detector.clear()
    owners.detector.update(detect_runtime_identity_drift(
        expected_commit=COMMIT, scope=SCOPE,
        unit_environments={"eimemory-rpc.service": "EIMEMORY_RUNTIME_COMMIT=" + (COMMIT if healthy else "b" * 40)},
        detected_at="2026-10-19T00:00:00Z"))
    owners.detector["incident_record_id"] = "" if healthy else "incident-native"
    assert run(owners)["execution_ok"] is True
    assert [name for name, _ in owners.calls] == ["detector", "router", "failure", "recovery"]


@pytest.mark.parametrize("bad", ["foreign_owner", "changed_hash", "wrong_status"])
def test_corrupt_v2_detector_blocks_routes_and_keeps_recovery(owners, bad):
    owners.detector.clear()
    owners.detector.update(detect_runtime_identity_drift(
        expected_commit=COMMIT, scope=SCOPE,
        unit_environments={"eimemory-rpc.service": "EIMEMORY_RUNTIME_COMMIT=" + COMMIT},
        detected_at="2026-10-19T00:00:00Z"))
    if bad == "foreign_owner":
        owners.detector["scope"] = {**SCOPE, "user_id": "another-user"}
    elif bad == "changed_hash":
        owners.detector["identity_digest"] = "f" * 64
    else:
        owners.detector["status"] = "drift_detected"
    assert run(owners)["execution_ok"] is False
    assert [name for name, _ in owners.calls] == ["detector", "recovery"]


def test_unconfigured_nightly_preserves_recovery_without_system_detectors(owners):
    report = run(owners, detect_and_route=False)
    assert report["ok"] is True and report["system_detection_enabled"] is False
    assert [name for name, _ in owners.calls] == ["recovery"]


@pytest.mark.parametrize("invalid", [-1, 101, True, "1", 1.1])
def test_invalid_bounds_fail_before_any_owner(owners, invalid):
    with pytest.raises(ValueError):
        run(owners, limit=invalid)
    assert owners.calls == []


@pytest.mark.parametrize("bad", [
    {"ok": False}, {"ok": False, "status": "unavailable"},
    {"ok": True, "status": "current", "schema": "runtime_identity_drift.v1"},
    {"ok": False, "status": "current", "schema": "runtime_identity_drift.v1"},
    {"ok": True, "status": "current", "schema": "runtime_identity_drift.v1", "error": "read_failed"},
])
def test_unverified_detector_blocks_new_routes_but_always_recovers_existing_leases(owners, bad):
    owners.detector.clear(); owners.detector.update(bad)
    report = run(owners)
    assert report["ok"] is False and report["status"] == "blocked"
    assert [name for name, _ in owners.calls] == ["detector", "recovery"]


@pytest.mark.parametrize("bad", [
    {"ok": False}, {"ok": False, "status": "unavailable"},
    {"ok": False, "status": "non_actionable", "schema": "system_code_repair_failure.v1"},
    {"ok": True, "status": "non_actionable", "schema": "system_code_repair_failure.v1", "error": "read_failed"},
])
def test_failure_detector_cannot_default_malformed_or_failed_results_to_success(owners, bad):
    owners.failure.clear(); owners.failure.update(bad)
    report = run(owners)
    assert report["execution_ok"] is False
    assert owners.calls[-1][0] == "recovery"


def test_valid_system_drift_is_an_observation_and_not_a_false_detector_exception(owners):
    owners.detector.clear()
    owners.detector.update(detect_runtime_identity_drift(
        expected_commit=COMMIT, scope=SCOPE,
        unit_environments={"eimemory-rpc.service": "EIMEMORY_RUNTIME_COMMIT=" + "b" * 40},
        detected_at="2026-10-19T00:00:00Z"))
    owners.detector["incident_record_id"] = "incident-native"
    owners.route.update(status="processed", processed=[{"transaction_id": "transaction-native", "status": "PROPOSED"}])
    report = run(owners)
    assert report["execution_ok"] is True and report["status"] == "processed"
    assert report["repair_complete"] is False
    assert report["runtime_identity"]["known_before_detection"] is None
    assert report["code_evolution"] == owners.recovery


def test_manual_policy_preserved_defers_router_without_hard_fail(owners, monkeypatch):
    # Expired/unowned automation policy must not force incident routing that
    # needs a live automation identity; recovery still runs.
    monkeypatch.setattr(
        "eimemory.governance.evolution.code_automation_policy_owner.ensure_native_incident_policy",
        lambda *_a, **_k: {"ok": True, "status": "manual_policy_preserved", "repair_complete": False})
    report = run(owners)
    assert report["execution_ok"] is True
    assert report["native_policy_owner"]["status"] == "manual_policy_preserved"
    assert report["system_code_repair"] == {
        "ok": True, "status": "deferred", "reason": "manual_policy_preserved", "processed": []}
    assert [name for name, _ in owners.calls] == ["detector", "failure", "recovery"]
    assert "router" not in [name for name, _ in owners.calls]


def test_blocked_route_and_failed_recovery_remain_visible(owners):
    owners.route.update(ok=False, status="blocked", reason="automation_policy_identity_unavailable")
    owners.recovery.update(ok=False, status="blocked", reason="observation_failed")
    report = run(owners)
    assert report["execution_ok"] is False and report["repair_complete"] is False
    assert report["system_code_repair"]["reason"] == "automation_policy_identity_unavailable"
    assert report["code_evolution"]["reason"] == "observation_failed"
    assert owners.calls[-1][0] == "recovery"


def test_native_policy_owner_failure_keeps_existing_transaction_recovery(owners, monkeypatch):
    monkeypatch.setattr("eimemory.governance.evolution.code_automation_policy_owner.ensure_native_incident_policy",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError('fixture-owner-failure')))
    report = run(owners)
    assert report['ok'] is False
    assert report['native_policy_owner']['reason'] == 'native_policy_owner_error:OSError'
    assert report['system_code_repair']['reason'] == 'native_policy_owner_unverified'
    assert [name for name, _ in owners.calls] == ['detector', 'recovery']
    assert report['system_code_repair_failure']['reason'] == 'router_not_executed'


@pytest.mark.parametrize("stage", ["detector", "route", "failure", "recovery"])
def test_explicit_error_list_is_never_overridden_by_a_success_flag(owners, stage):
    getattr(owners, stage)["errors"] = ["explicit_failure"]
    report = run(owners)
    assert report["ok"] is False and report["execution_ok"] is False
    assert owners.calls[-1][0] == "recovery"


@pytest.mark.parametrize("apply,maintenance_ok", [(False, True), (True, True), (True, False)])
def test_watch_uses_shared_owner_and_propagates_nested_failure(monkeypatch, capsys, apply, maintenance_ok):
    from eimemory.cli import main as cli
    calls = []
    monkeypatch.setattr("eimemory.governance.world_watchers.collect_world_signals", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr("eimemory.governance.world_watchers.default_watches", lambda: [])
    def maintenance(_runtime, **kwargs):
        calls.append(kwargs)
        return {"execution_ok": maintenance_ok, "runtime_identity": {}, "system_code_repair": {},
                "system_code_repair_failure": {}, "code_evolution": {}}
    monkeypatch.setattr("eimemory.governance.evolution.system_code_maintenance.run_system_code_maintenance", maintenance)
    args = cli._build_parser().parse_args(["learn", "watch"] + (["--apply"] if apply else []))
    assert cli._cmd_learn(args, runtime=object(), scope=SCOPE) == (0 if maintenance_ok else 1)
    assert calls[0]["apply"] is apply and calls[0]["owner_id"] == "learn-watch:code-evolution"
    assert '"system_code_maintenance"' in capsys.readouterr().out


@pytest.mark.parametrize("owner_field", ["tenant_id", "agent_id", "workspace_id", "user_id", "history"])
def test_recovery_limit_cannot_hide_owned_observation_behind_other_owners_or_history(tmp_path, monkeypatch, owner_field):
    runtime_store = RuntimeStore(tmp_path / "runtime")
    try:
        manager = _observing_manager(runtime_store, "owned-observation")
        owner = {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"}
        other = dict(owner)
        if owner_field != "history":
            other[owner_field] = "other"
        for index in range(105):
            manager.store.create_transaction({
                "transaction_id": f"newer-history-{index}", "idempotency_key": f"newer-history-{index}",
                "scope": other, "incident": {"incident_id": f"other-{index}"},
                "repository": {"root": f"/other-{index}", "ref": "master"},
                "current_state": "SUCCEEDED_SEDIMENTED", "terminal": True,
                "updated_at": "2099-01-01T00:00:00+00:00",
            })
        seen = []
        def sample(_runtime, *, transaction):
            seen.append(transaction["transaction_id"])
            return _sample("owned-phase-0", observed_at="2026-01-01T00:00:00+00:00", health_ok=True)
        monkeypatch.setattr("eimemory.governance.code_evolution_effects.sample_code_evolution_observation", sample)
        report = resume_code_evolution_transactions(runtime_store, scope=owner, limit=1)
        assert report["ok"] is True
        assert seen == ["owned-observation"]
        assert len(report["reports"]) == 1 and report["reports"][0]["status"] == "observing"
        current = manager.store.get_transaction("owned-observation")
        assert current["current_state"] == "OBSERVING" and not current["terminal"]
        assert current["payload"]["observation_samples"][0]["sample_key"] == "owned-phase-0"
        assert manager.store.get_terminal_receipt("owned-observation") is None
    finally:
        runtime_store.close()


@pytest.mark.parametrize("failure", ["authority", "invalid_sample", "effects", "unauthorized"])
def test_recovery_failures_cannot_make_the_maintenance_owner_report_success(tmp_path, monkeypatch, failure):
    from eimemory.governance.evolution.system_code_maintenance import run_system_code_maintenance
    from types import SimpleNamespace

    runtime_store = RuntimeStore(tmp_path / "runtime")
    owner = {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"}
    try:
        manager = _observing_manager(runtime_store, "owned-failure")
        if failure in {"effects", "unauthorized"}:
            # Isolate the execution/authorization verdict; never execute an
            # actual external effect or grant authority to this fixture.
            monkeypatch.setattr("eimemory.governance.evolution.code_evolution_transaction.FORWARD_EFFECT_STATES", {"OBSERVING"})
            monkeypatch.setattr("eimemory.governance.evolution.code_evolution_transaction.effect_execution_authorized",
                                lambda _transaction: failure == "effects")
            monkeypatch.setattr("eimemory.governance.code_evolution_effects.execute_code_evolution_effects",
                                lambda *a, **kw: {"ok": False, "status": "blocked", "blocked_reason": "machine_policy_blocked"})
        else:
            sample = ({"ok": False, "reason": "observation_release_identity_unavailable"}
                      if failure == "authority" else {"observed_at": "2026-01-01T00:00:00+00:00"})
            monkeypatch.setattr("eimemory.governance.code_evolution_effects.sample_code_evolution_observation",
                                lambda *a, **kw: sample)
        runtime = SimpleNamespace(resume_code_evolution_transactions=lambda **kw: resume_code_evolution_transactions(runtime_store, **kw))
        result = run_system_code_maintenance(runtime, scope=owner, owner_id="test-maintenance", detect_and_route=False)
        assert result["ok"] is False and result["execution_ok"] is False
        assert len(result["reports"]) == 1
        assert manager.store.get_transaction("owned-failure")["current_state"] == "OBSERVING"
        assert manager.store.get_terminal_receipt("owned-failure") is None
    finally:
        runtime_store.close()


@pytest.mark.parametrize("defect", ["json", "owner", "repository", "missing_owner"])
def test_owned_corrupt_recovery_page_blocks_before_sampling_or_effects(tmp_path, monkeypatch, defect):
    from eimemory.governance.evolution.system_code_maintenance import run_system_code_maintenance
    from types import SimpleNamespace
    import json

    runtime_store = RuntimeStore(tmp_path / "runtime")
    owner = {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"}
    try:
        manager = _observing_manager(runtime_store, "corrupt-owned")
        payload = dict(manager.store.get_transaction("corrupt-owned")["payload"])
        if defect == "owner": payload["scope"] = {**owner, "user_id": "foreign"}
        if defect == "missing_owner": payload.pop("scope")
        if defect == "repository": payload["repository"] = {"root": "/different", "ref": "master"}
        raw = "not json" if defect == "json" else json.dumps(payload)
        runtime_store.sqlite.conn.execute("UPDATE code_evolution_transactions SET payload_json=? WHERE transaction_id=?",
                                          (raw, "corrupt-owned"))
        runtime_store.sqlite.conn.commit()
        monkeypatch.setattr("eimemory.governance.code_evolution_effects.sample_code_evolution_observation",
                            lambda *a, **kw: pytest.fail("corrupt owned page reached a sampler"))
        runtime = SimpleNamespace(resume_code_evolution_transactions=lambda **kw: resume_code_evolution_transactions(runtime_store, **kw))
        report = run_system_code_maintenance(runtime, scope=owner, owner_id="page-test", detect_and_route=False)
        assert report["ok"] is False and report["execution_ok"] is False
        assert report["reason"].startswith("transaction_recovery_error:")
    finally:
        runtime_store.close()


def test_recovery_queue_keeps_a_bounded_new_terminal_feedback_slot_after_active_work(tmp_path, monkeypatch):
    runtime_store = RuntimeStore(tmp_path / "runtime")
    owner = {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"}
    try:
        manager = _observing_manager(runtime_store, "active")
        for name, stamp in (("old-feedback", "2020-01-01T00:00:00+00:00"),
                            ("new-feedback", "2099-01-01T00:00:00+00:00")):
            manager.store.create_transaction({
                "transaction_id": name, "idempotency_key": name, "scope": owner,
                "incident": {"incident_id": name}, "repository": {"root": "/history", "ref": "master"},
                "current_state": "SUCCEEDED_SEDIMENTED", "terminal": True, "updated_at": stamp,
                "payload": {"dynamic_capability_context": {"fixture_only": True}},
            })
        monkeypatch.setattr("eimemory.governance.code_evolution_effects.sample_code_evolution_observation",
                            lambda *a, **kw: _sample("phase-0", observed_at="2026-01-01T00:00:00+00:00", health_ok=True))
        seen = []
        def feedback(*args, **kwargs):
            seen.append(kwargs["transaction_id"])
            return {"ok": True, "status": "feedback-checked", "transaction_id": kwargs["transaction_id"]}
        monkeypatch.setattr("eimemory.governance.evolution.code_evolution_hypothesis_feedback.record_transaction_hypothesis_feedback", feedback)
        report = resume_code_evolution_transactions(runtime_store, scope=owner, limit=2)
        assert report["ok"] is True and len(report["reports"]) == 2
        assert report["reports"][0]["status"] == "observing" and seen == ["new-feedback"]
        assert len(manager.store.list_recovery_transactions_exact_scope(scope=owner, limit=1)) == 1
        for invalid in (0, 501, True):
            with pytest.raises(CodeEvolutionStoreError, match="queue bounds"):
                manager.store.list_recovery_transactions_exact_scope(scope=owner, limit=invalid)
    finally:
        runtime_store.close()


def test_owned_recovery_queue_has_an_indexed_bounded_read_without_history_sort(tmp_path):
    runtime_store = RuntimeStore(tmp_path / "runtime")
    try:
        manager = _observing_manager(runtime_store, "indexed-active")
        owner = {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"}
        conn = runtime_store.sqlite.conn
        def measured_read():
            statements, steps = [], []
            conn.set_trace_callback(statements.append)
            conn.set_progress_handler(lambda: steps.append(1) or 0, 1)
            try:
                rows = manager.store.list_recovery_transactions_exact_scope(scope=owner, limit=1)
            finally:
                conn.set_progress_handler(None, 0)
                conn.set_trace_callback(None)
            return rows, len(steps), statements
        _, baseline_steps, _ = measured_read()
        for index in range(105):
            manager.store.create_transaction({
                "transaction_id": f"indexed-history-{index}", "idempotency_key": f"indexed-history-{index}",
                "scope": owner, "incident": {"incident_id": f"history-{index}"},
                "repository": {"root": "/history", "ref": "master"},
                "current_state": "SUCCEEDED_SEDIMENTED", "terminal": True,
                "updated_at": "2099-01-01T00:00:00+00:00",
            })
        rows, expanded_steps, statements = measured_read()
        assert len(rows) == 1 and rows[0]["transaction_id"] == "indexed-active"
        assert expanded_steps <= baseline_steps + 10
        query = next(sql for sql in statements if sql.startswith("SELECT * FROM code_evolution_transactions"))
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + query)]
        assert any("idx_code_evolution_recovery_scope" in item for item in plan), plan
        assert not any("TEMP B-TREE" in item for item in plan), plan
    finally:
        runtime_store.close()


def test_existing_ledger_adds_the_recovery_index_without_changing_transaction_evidence(tmp_path):
    from eimemory.storage.migrations.code_evolution_transactions import (
        ensure_code_evolution_schema, is_code_evolution_schema_ready,
    )

    runtime_store = RuntimeStore(tmp_path / "runtime")
    try:
        manager = _observing_manager(runtime_store, "existing-evidence")
        before = manager.store.get_transaction("existing-evidence")
        conn = runtime_store.sqlite.conn
        conn.execute("DROP INDEX idx_code_evolution_recovery_scope")
        conn.commit()
        assert is_code_evolution_schema_ready(conn) is False
        ensure_code_evolution_schema(conn)
        assert is_code_evolution_schema_ready(conn) is True
        assert manager.store.get_transaction("existing-evidence") == before
        assert manager.store.get_terminal_receipt("existing-evidence") is None
    finally:
        runtime_store.close()
