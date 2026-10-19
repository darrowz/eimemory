from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from eimemory.contracts.release_identity import ReleaseIdentity, release_identity_payload
from eimemory.governance.l5 import native_assessment as native
from eimemory.governance.l5.l5_assessment_v3 import build_l5_assessment_v3
from eimemory.models.records import ScopeRef
from eimemory.scheduler import jobs
from eimemory.scheduler.result_contract import _aggregate_nightly_ok, _nightly_step, nightly_result_diagnostics
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef("native-l5", "logical-agent", "workspace", "owner")
RELEASE = ReleaseIdentity("b" * 40, "synthetic-release", "receipt-native", "session-native")


def envelope(complete=False):
    return {"schema": "l5.reader.v4", "schema_version": "l5_readiness.v4", "reader_mode": "v3",
        "profile_key": "l5.default", "scope": asdict(SCOPE), "ok": complete,
        "control_plane_ok": True, "product_l5_complete": complete,
        "status": "ready" if complete else "incomplete", "completion_status": "complete" if complete else "incomplete",
        "assessment": {"assessment_id": "native-assessment", "assessment_digest": "a" * 64,
            "persisted": {"entity_type": "assessment", "entity_id": "native-assessment", "entity_digest": "a" * 64}},
        "gaps": [] if complete else ["no_qualifying_terminal_receipt"]}


@pytest.fixture
def producer(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_L5_V3_PROFILE", raising=False)
    monkeypatch.setattr(native, "current_release_identity", lambda *args: RELEASE)
    monkeypatch.setattr("eimemory.governance.release.evidence_contract.current_release_identity", lambda *args: RELEASE)
    with closing(RuntimeStore(tmp_path)) as store:
        calls = []
        value = envelope()
        def build(**kwargs):
            calls.append(kwargs)
            return deepcopy(value)
        yield SimpleNamespace(store=store, build_l5_readiness_report=build, calls=calls, value=value)


def test_incomplete_native_reading_is_archived_without_certifying_product(producer):
    result = native.run_native_l5_assessment(producer, scope=asdict(SCOPE), loop_id="nightly-synthetic")
    assert not result["ok"] and result["execution_ok"] and result["control_plane_ok"]
    assert not result["product_l5_complete"] and result["status"] == "incomplete"
    assert result["archive_verified"] and producer.calls[0]["persist"] is True
    assert producer.calls[0]["reader_mode"] == "v3" and producer.calls[0]["runtime_scope"] == SCOPE
    assert producer.calls[0]["profile_key"] == "l5.default" and len(producer.calls) == 1
    saved = producer.store.get_by_id(result["record_id"])
    assert saved.scope == SCOPE and saved.content["envelope"] == producer.value
    assert saved.content["product_l5_complete"] is False
    assert all(saved.meta[k] == saved.content[k] == v for k, v in release_identity_payload(RELEASE).items())
    repeated = native.run_native_l5_assessment(producer, scope=asdict(SCOPE), loop_id="nightly-synthetic")
    assert repeated["record_id"] == result["record_id"]
    assert len(producer.store.list_records(kinds=["reflection"], scope=SCOPE)) == 1
    steps = []
    _nightly_step(steps, "native_l5_assessment", lambda: result)
    assert steps[0]["ok"] and steps[0]["evaluation_status"] == "awaiting_evidence"
    assert _aggregate_nightly_ok({"native_l5_assessment": result}, steps)
    diagnostics = nightly_result_diagnostics({"native_l5_assessment": result}, steps)
    assert diagnostics["l5_evidence_waits"] == ["native_l5_assessment"]
    assert diagnostics["evidence_waits"] == []
    assert diagnostics["native_l5_assessment"]["product_l5_complete"] is False


def test_native_product_completion_preserves_actual_true_bit(producer):
    producer.value.update(envelope(True))
    result = native.run_native_l5_assessment(producer, scope=asdict(SCOPE))
    assert result["ok"] and result["product_l5_complete"] and not result["awaiting_evidence"]


@pytest.mark.parametrize("negative", ["regression", "quarantine", "deployment", "invalid_terminal"])
def test_real_negative_evidence_is_archived_but_never_waived_as_starvation(producer, negative):
    if negative == "regression": producer.value["assessment"]["projection"] = {"snapshots": [{"maturity": "regressed"}]}
    if negative == "quarantine": producer.value["transaction_evidence"] = {"quarantined": True}
    if negative == "deployment": producer.value["assessment"]["deployment_assurance"] = {"required": True, "blocking": True}
    if negative == "invalid_terminal": producer.value["transaction_evidence"] = {"evidence_error": "terminal_receipt_identity_mismatch"}
    result = native.run_native_l5_assessment(producer, scope=asdict(SCOPE))
    assert result["execution_ok"] and result["archive_verified"] and result["evidence_failed"]
    assert result["status"] == "blocked" and not result["awaiting_evidence"] and not result["product_l5_complete"]
    steps = []
    _nightly_step(steps, "native_l5_assessment", lambda: result)
    assert not steps[0]["ok"] and not _aggregate_nightly_ok({"native_l5_assessment": result}, steps)


def test_terminal_lineage_binding_failure_agrees_in_product_archive_and_nightly(producer):
    from eimemory.governance.l5.l5_product_completion import build_product_completion

    transaction = {
        "transaction_id": "terminal-from-other-release", "terminal_receipt_digest": "c" * 64,
        "qualifying_terminal_outcome": "succeeded_sedimented", "origin": "system_detector",
        "known_before_detection": False, "prior_user_reported": False, "manual_bootstrap": False,
        "observation_valid": True, "evidence_verified": False,
        "evidence_error": "terminal_transaction_lineage_mismatch",
    }
    product = build_product_completion(
        {"ok": True, "status": "ready"},
        provider={"ready": True, "catalog_ready": True, "advertisement_fresh": True},
        transaction=transaction, current_lineage={"ok": True, "compatible": True},
    )
    assert product["gaps"] == ["terminal_transaction_lineage_mismatch"]
    assert not product["product_l5_complete"]
    producer.value.update(product, transaction_evidence=transaction)
    result = native.run_native_l5_assessment(producer, scope=asdict(SCOPE))
    assert result["execution_ok"] and result["archive_verified"] and result["evidence_failed"]
    assert result["status"] == "blocked" and not result["awaiting_evidence"]
    archived = producer.store.get_by_id(result["record_id"], scope=SCOPE, exact_scope=True)
    assert archived.content["envelope"]["gaps"] == ["terminal_transaction_lineage_mismatch"]
    assert archived.content["envelope"]["transaction_evidence"] == transaction
    steps = []
    _nightly_step(steps, "native_l5_assessment", lambda: result)
    assert not steps[0]["ok"] and not _aggregate_nightly_ok({"native_l5_assessment": result}, steps)


@pytest.mark.parametrize("fault", ["scope", "legacy", "boolean", "missing_persistence", "invalid_digest",
    "completion_without_assessment", "reader_error", "projection_exception"])
def test_unknown_or_failed_reader_cannot_be_an_evidence_wait(producer, fault):
    if fault == "scope": producer.value["scope"]["user_id"] = "foreign-owner"
    if fault == "legacy": producer.value["schema_version"] = "l5_readiness.v2"
    if fault == "boolean": producer.value["product_l5_complete"] = "true"
    if fault == "missing_persistence": producer.value["assessment"]["persisted"] = None
    if fault == "invalid_digest":
        producer.value["assessment"]["assessment_digest"] = "unknown"
        producer.value["assessment"]["persisted"]["entity_digest"] = "unknown"
    if fault == "completion_without_assessment":
        producer.value.update(envelope(True))
        producer.value["assessment"] = {}
    if fault == "reader_error": producer.value["transaction_evidence"] = {"ledger_error": "RuntimeError"}
    if fault == "projection_exception": producer.value["assessment"]["reason"] = "capability_projection_unavailable:RuntimeError"
    result = native.run_native_l5_assessment(producer, scope=asdict(SCOPE))
    steps = []
    _nightly_step(steps, "native_l5_assessment", lambda: result)
    assert not result["execution_ok"] and not steps[0]["ok"] and not result["awaiting_evidence"]
    captured_failure = fault in {"reader_error", "projection_exception"}
    assert result["archive_verified"] is captured_failure
    records = producer.store.list_records(kinds=["reflection"], scope=SCOPE)
    assert len([record for record in records if record.source == native.SOURCE]) == int(captured_failure)
    diagnostics = [record for record in records if record.source == "eimemory.scheduler.step_evidence"]
    assert len(diagnostics) == int(not captured_failure)
    if not captured_failure:
        assert result["failure_evidence"]["archive_verified"] is True
        assert diagnostics[0].content["step_report"]["execution_ok"] is False
        assert diagnostics[0].content["step_report"]["failure_stage"] == result["failure_stage"]


def test_current_release_must_exist_before_reader_or_any_write(producer, monkeypatch):
    monkeypatch.setattr(native, "current_release_identity", lambda *args: None)
    result = native.run_native_l5_assessment(producer, scope=asdict(SCOPE))
    assert result["error"] == "native_l5_current_release_unverified" and producer.calls == []


def test_release_authority_change_cannot_be_archived_as_current(producer, monkeypatch):
    releases = iter([RELEASE, replace(RELEASE, receipt_id="different-receipt")])
    monkeypatch.setattr(native, "current_release_identity", lambda *args: next(releases))
    result = native.run_native_l5_assessment(producer, scope=asdict(SCOPE))
    assert result["error"] == "native_l5_release_authority_changed"
    assert not result["execution_ok"]


def test_storage_error_fails_the_step_without_exporting_exception_text(producer, monkeypatch):
    def fail(*args, **kwargs): raise OSError("private database path")
    monkeypatch.setattr(native, "append_learning_record_once", fail)
    result = native.run_native_l5_assessment(producer, scope=asdict(SCOPE))
    assert not result["execution_ok"] and result["error_type"] == "OSError"
    assert "private" not in str(result)


@pytest.mark.parametrize("status", ["revoked", "quarantined", "archived", "unknown"])
def test_replaying_revoked_or_unknown_archive_cannot_reactivate_it(producer, status):
    first = native.run_native_l5_assessment(producer, scope=asdict(SCOPE), loop_id="same-round")
    saved = producer.store.get_by_id(first["record_id"], scope=SCOPE, exact_scope=True)
    saved.status = status
    producer.store.rewrite(saved)
    repeated = native.run_native_l5_assessment(producer, scope=asdict(SCOPE), loop_id="same-round")
    assert repeated["record_id"] == first["record_id"]
    assert not repeated["archive_verified"] and not repeated["execution_ok"]
    assert not repeated["awaiting_evidence"] and not repeated["product_l5_complete"]
    assert producer.store.get_by_id(first["record_id"], scope=SCOPE, exact_scope=True).status == status
    records = producer.store.list_records(kinds=["reflection"], scope=SCOPE)
    assert len([record for record in records if record.source == native.SOURCE]) == 1
    assert len([record for record in records if record.source == "eimemory.scheduler.step_evidence"]) == 1
    assert repeated["failure_evidence"]["archive_verified"] is True
    assert repeated["failure_stage"] == "archive_readback"


@pytest.mark.parametrize("column,value", [("status", "revoked"), ("kind", "incident"),
    ("source", "other-source"), ("source_id", "foreign-partition"), ("payload_digest", "0" * 64)])
def test_archive_sql_projection_must_match_full_native_payload(producer, monkeypatch, column, value):
    append = native.append_learning_record_once
    def corrupt_after_write(*args, **kwargs):
        record = append(*args, **kwargs)
        with producer.store._lock:
            producer.store.sqlite.conn.execute("UPDATE records SET " + column + "=? WHERE record_id=?",
                (value, record.record_id))
            producer.store.sqlite.conn.commit()
        return record
    monkeypatch.setattr(native, "append_learning_record_once", corrupt_after_write)
    result = native.run_native_l5_assessment(producer, scope=asdict(SCOPE))
    assert not result["archive_verified"] and not result["execution_ok"] and not result["awaiting_evidence"]
    assert not result["product_l5_complete"]
    assert producer.store.sqlite.conn.execute("SELECT count(*) FROM records WHERE record_id=?",
        (result["record_id"],)).fetchone()[0] == 1


def test_consistent_foreign_source_partition_cannot_qualify_an_archive(producer, monkeypatch):
    append = producer.store.append
    def write_other_partition(record):
        record.source_id = "foreign-partition"
        return append(record)
    monkeypatch.setattr(producer.store, "append", write_other_partition)
    result = native.run_native_l5_assessment(producer, scope=asdict(SCOPE))
    assert not result["archive_verified"] and not result["execution_ok"] and not result["awaiting_evidence"]
    saved = producer.store.get_by_id(result["record_id"], scope=SCOPE, exact_scope=True)
    assert saved.source_id == "foreign-partition" and saved.status == "active"


@pytest.mark.parametrize("field,value", [("execution_ok", False), ("archive_verified", False),
    ("evidence_failed", True), ("control_plane_ok", "true"), ("release_session_id", ""),
    ("envelope_digest", "unknown"), ("record_id", "")])
def test_nightly_evidence_wait_requires_verified_native_capture(producer, field, value):
    result = native.run_native_l5_assessment(producer, scope=asdict(SCOPE))
    result[field] = value
    steps = []
    _nightly_step(steps, "native_l5_assessment", lambda: result)
    assert not steps[0]["ok"] and not _aggregate_nightly_ok({"native_l5_assessment": result}, steps)


def test_nightly_shares_the_native_profile_and_maintenance_owner(monkeypatch):
    monkeypatch.setenv("EIMEMORY_L5_LOOP_ENABLED", "1")
    monkeypatch.delenv("EIMEMORY_L5_V3_PROFILE", raising=False)
    assert jobs._capability_v3_profile_key() == "l5.default"
    monkeypatch.setenv("EIMEMORY_L5_V3_PROFILE", "custom.profile")
    assert jobs._capability_v3_profile_key() == native.effective_l5_profile() == "custom.profile"
    calls = []
    monkeypatch.setattr(native, "run_native_l5_assessment", lambda runtime, **kw: calls.append(kw) or {"ok": True})
    jobs._run_native_l5_assessment(object(), scope=asdict(SCOPE), loop_id="round")
    assert calls == [{"scope": asdict(SCOPE), "loop_id": "round", "profile_key": "custom.profile", "capability_scope": "global"}]


def test_nightly_without_l5_configuration_does_not_claim_assessment_or_completion(monkeypatch):
    monkeypatch.delenv("EIMEMORY_L5_LOOP_ENABLED", raising=False)
    result = jobs._run_native_l5_assessment(object(), scope=asdict(SCOPE), loop_id="round")
    assert result["ok"] and result["enabled"] is False and result["status"] == "not_configured"
    assert not result["product_l5_complete"] and not result["archive_verified"]


@pytest.mark.parametrize("enabled,apply", [(False, False), (True, False), (True, True)])
def test_nightly_system_maintenance_uses_the_shared_owner(monkeypatch, enabled, apply):
    from eimemory.governance.evolution import system_code_maintenance as shared
    monkeypatch.setenv("EIMEMORY_L5_LOOP_ENABLED", str(int(enabled)))
    monkeypatch.setenv("EIMEMORY_L5_LOOP_APPLY", str(int(apply)))
    calls = []
    monkeypatch.setattr(shared, "run_system_code_maintenance", lambda runtime, **kw: calls.append(kw) or {"ok": True})
    assert jobs._run_code_evolution_maintenance(object(), scope=asdict(SCOPE))["ok"]
    assert calls == [{"scope": asdict(SCOPE), "owner_id": "nightly:code-evolution", "limit": 100,
                      "apply": True, "detect_and_route": enabled and apply}]


def test_native_assessment_persists_in_same_authorized_registry_as_snapshots(tmp_path, monkeypatch):
    from eimemory.capabilities.registry import CapabilityRegistry
    from eimemory.capabilities.profiles import CapabilityProfiles
    from eimemory.capabilities.observations import CapabilityObservations
    from test_capability_state_projector import _definition, _revision, _binding, _profile, _observation
    import eimemory.identity as identity
    monkeypatch.setattr(identity, "HONGTU_AGENT_ID", SCOPE.agent_id)
    monkeypatch.setattr(identity, "legacy_agent_ids", lambda: ("old-agent",))
    physical = replace(SCOPE, agent_id="old-agent")
    with closing(RuntimeStore(tmp_path)) as store:
        registry = CapabilityRegistry(store)
        definition = _definition("synthetic.capability")
        revision = _revision(definition)
        binding = _binding(definition, revision, name="native")
        profile = _profile(definition.capability_id)
        registry.register_definition(definition, runtime_scope=physical)
        registry.register_revision(revision, runtime_scope=physical)
        registry.bind(binding, runtime_scope=physical)
        CapabilityProfiles(store).register(profile, runtime_scope=physical)
        CapabilityObservations(store).append(_observation(definition, revision, binding, name="native-pass"), runtime_scope=physical)
        runtime = SimpleNamespace(store=store)
        first = build_l5_assessment_v3(runtime, profile_key=profile.profile_key, scope=SCOPE,
            persist=True, at_time="2024-08-20T00:01:00+00:00")
        assert first["persisted"] and first["assessment_id"]
        repeated = build_l5_assessment_v3(runtime, profile_key=profile.profile_key, scope=SCOPE,
            persist=True, at_time="2024-08-20T00:01:00+00:00")
        assert repeated["assessment_id"] == first["assessment_id"]
        assert repeated["projection"]["projection_digest"] == first["projection"]["projection_digest"]
        assert first["projection"]["snapshots"][0]["idempotent"] is False
        assert repeated["projection"]["snapshots"][0]["idempotent"] is True
        for table in ("capability_state_snapshots", "l5_assessments_v3"):
            rows = store.sqlite.conn.execute("SELECT agent_id,user_id FROM " + table).fetchall()
            assert len(rows) == 1 and tuple(rows[0]) == (physical.agent_id, physical.user_id)
        foreign = build_l5_assessment_v3(runtime, profile_key=profile.profile_key,
            scope=replace(SCOPE, user_id="foreign"), persist=True)
        assert not foreign["ok"] and not foreign["persisted"]
        assert store.sqlite.conn.execute("SELECT count(*) FROM l5_assessments_v3").fetchone()[0] == 1
        CapabilityObservations(store).append(_observation(definition, revision, binding,
            name="new-real-failure", verdict="fail", observed_at="2024-08-20T00:00:02+00:00"),
            runtime_scope=physical)
        changed = build_l5_assessment_v3(runtime, profile_key=profile.profile_key, scope=SCOPE,
            persist=True, at_time="2024-08-20T00:01:00+00:00")
        assert changed["projection"]["input_watermark"] != first["projection"]["input_watermark"]
        assert changed["projection"]["projection_digest"] != first["projection"]["projection_digest"]
        assert changed["assessment_id"] != first["assessment_id"]
        assert store.sqlite.conn.execute("SELECT count(*) FROM l5_assessments_v3").fetchone()[0] == 2
        # Exercise the actual v3 -> runtime v4 -> archive path. Only external
        # provider/terminal authority is unavailable in this isolated fixture.
        from eimemory.governance.l5 import l5_reader as reader
        monkeypatch.setattr(reader, "_code_evolution_evidence", lambda *a, **kw: ({}, {}, {}))
        monkeypatch.setattr(native, "current_release_identity", lambda *args: RELEASE)
        monkeypatch.setattr("eimemory.governance.release.evidence_contract.current_release_identity", lambda *a: RELEASE)
        runtime.build_l5_readiness_report = lambda **kw: reader.build_l5_effective_report(
            runtime, at_time="2024-08-20T00:01:00+00:00", **kw)
        archived = native.run_native_l5_assessment(runtime, scope=asdict(SCOPE), profile_key=profile.profile_key)
        assert archived["execution_ok"] and archived["archive_verified"]
        assert archived["assessment_persisted"] and not archived["product_l5_complete"]
        assert archived["status"] == "blocked" and not archived["awaiting_evidence"]
        assert archived["envelope"]["scope"] == asdict(SCOPE)
        assert store.get_by_id(archived["record_id"], scope=SCOPE, exact_scope=True).source == native.SOURCE
        assert store.sqlite.conn.execute("SELECT count(*) FROM code_evolution_terminal_receipts").fetchone()[0] == 0


def test_native_channel_owner_uses_only_authorized_deployment_selector(tmp_path, monkeypatch):
    from eimemory.capabilities.registry import CapabilityRegistry
    from eimemory.capabilities.profiles import CapabilityProfiles
    from eimemory.capabilities.observations import CapabilityObservations
    from eimemory.governance.l5 import l5_reader as reader
    from test_capability_state_projector import _definition, _revision, _binding, _profile, _observation
    import eimemory.identity as identity

    product = ScopeRef("native-l5", "logical-agent", "workspace", "operator")
    owner = replace(product, workspace_id="workspace::channel::hermes", user_id="channel-operator")
    physical = replace(product, agent_id="old-agent")
    monkeypatch.setenv("EIMEMORY_USER_ID", product.user_id)
    monkeypatch.setenv("EIMEMORY_USER_ALIASES", owner.user_id)
    monkeypatch.setenv("EIMEMORY_AGENT_ID", product.agent_id)
    monkeypatch.setenv("EIMEMORY_WORKSPACE_ID", product.workspace_id)
    monkeypatch.setattr(native, "load_settings", lambda: SimpleNamespace(
        default_agent_id=product.agent_id, default_workspace_id=product.workspace_id))
    monkeypatch.setattr(identity, "HONGTU_AGENT_ID", product.agent_id)
    monkeypatch.setattr(identity, "legacy_agent_ids", lambda: (physical.agent_id,))
    monkeypatch.setattr(native, "current_release_identity", lambda *a: RELEASE)
    monkeypatch.setattr("eimemory.governance.release.evidence_contract.current_release_identity", lambda *a: RELEASE)
    evidence_calls = []
    def independent_evidence(*args, **kwargs):
        evidence_calls.append(kwargs)
        return {}, {}, {}
    monkeypatch.setattr(reader, "_code_evolution_evidence", independent_evidence)
    with closing(RuntimeStore(tmp_path)) as store:
        registry = CapabilityRegistry(store)
        definition = _definition("synthetic.deployed.capability")
        revision = _revision(definition)
        binding = _binding(definition, revision, name="deployed")
        profile = _profile(definition.capability_id)
        registry.register_definition(definition, runtime_scope=physical)
        registry.register_revision(revision, runtime_scope=physical)
        registry.bind(binding, runtime_scope=physical)
        CapabilityProfiles(store).register(profile, runtime_scope=physical)
        # A channel's declared selector may legitimately resolve zero entities.
        channel_profile = replace(profile, requirements={"channel-selector": {
            "selector": {"tags_all": ["no-channel-definitions"]}, "minimum_maturity": "reliable"}})
        CapabilityProfiles(store).register(channel_profile, runtime_scope=owner)
        CapabilityObservations(store).append(_observation(definition, revision, binding,
            name="deployed-pass"), runtime_scope=physical)
        runtime = SimpleNamespace(store=store)
        runtime.build_l5_readiness_report = lambda **kw: reader.build_l5_effective_report(
            runtime, at_time="2024-08-20T00:01:00+00:00", **kw)
        result = native.run_native_l5_assessment(runtime, scope=asdict(owner), profile_key=profile.profile_key)
        assert result["execution_ok"] and result["archive_verified"] and result["assessment_persisted"]
        assert result["selector_scope"] == asdict(product) and result["scope"] == asdict(owner)
        assert result["envelope"]["assessment"]["projection"]["snapshots"][0]["maturity"] == "reliable"
        assert evidence_calls[-1]["runtime_scope"] == product and evidence_calls[-1]["evidence_scope"] == owner
        saved = store.get_by_id(result["record_id"], scope=owner, exact_scope=True)
        assert saved.scope == owner and saved.content["selector_scope"] == asdict(product)
        assert saved.content["envelope"]["scope"] == asdict(owner)
        rows = store.sqlite.conn.execute("SELECT agent_id,workspace_id,user_id FROM l5_assessments_v3").fetchall()
        assert len(rows) == 1 and tuple(rows[0]) == (physical.agent_id, physical.workspace_id, physical.user_id)
        foreign = replace(owner, user_id="foreign-user")
        CapabilityProfiles(store).register(channel_profile, runtime_scope=foreign)
        denied = native.run_native_l5_assessment(runtime, scope=asdict(foreign), profile_key=profile.profile_key)
        assert denied["selector_scope"] == asdict(foreign) and not denied["product_l5_complete"]
        assert not denied["assessment_persisted"]
        assert store.sqlite.conn.execute("SELECT count(*) FROM l5_assessments_v3").fetchone()[0] == 1
        assert store.sqlite.conn.execute("SELECT count(*) FROM code_evolution_terminal_receipts").fetchone()[0] == 0


def test_capability_scope_grants_follow_current_environment_and_revocation(monkeypatch):
    from eimemory.governance.l5.l5_scope_authority import authorized_capability_scopes
    product = ScopeRef("tenant", "agent", "workspace", "operator")
    channel = replace(product, workspace_id="workspace::channel::hermes", user_id="new-channel-alias")
    monkeypatch.setenv("EIMEMORY_USER_ID", product.user_id)
    monkeypatch.setenv("EIMEMORY_USER_ALIASES", channel.user_id)
    assert product in authorized_capability_scopes(channel)
    assert product not in authorized_capability_scopes(replace(channel, user_id="foreign"))
    monkeypatch.delenv("EIMEMORY_USER_ALIASES")
    monkeypatch.delenv("EIMEMORY_FEISHU_OPEN_ID", raising=False)
    monkeypatch.delenv("FEISHU_OPEN_ID", raising=False)
    assert product not in authorized_capability_scopes(channel)


def test_native_assessment_runs_once_after_all_nightly_maintenance(producer, monkeypatch):
    monkeypatch.setenv("EIMEMORY_L5_LOOP_ENABLED", "1")
    names = []
    def isolated_step(steps, name, fn):
        names.append(name)
        # Isolate the pipeline order from all model, network and maintenance
        # effects, while executing the actual final producer and its DB writes.
        return _nightly_step(steps, name, fn if name == "native_l5_assessment" else lambda: {"ok": True})
    monkeypatch.setattr(jobs, "_nightly_step", isolated_step)
    report = jobs.run_nightly_jobs(producer, scope=asdict(SCOPE))
    assert names[-1] == "native_l5_assessment" and names.count("native_l5_assessment") == 1
    assert names.index("code_evolution") < names.index("native_l5_assessment")
    assert names.index("quality_gap_intake") < names.index("native_l5_assessment")
    assert len(producer.calls) == 1 and producer.calls[0]["persist"] is True
    assert report["native_l5_assessment"]["archive_verified"]
    assert not report["native_l5_assessment"]["product_l5_complete"]
