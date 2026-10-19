"""Isolated durable-job faults. No real unit, authority file, or production DB."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
import json
import os
import sys

import pytest

from eimemory.contracts.code_deployment_job import request_key, validate_request
from eimemory.governance.evolution import code_deployment_job as jobs
from eimemory.governance.evolution import code_evolution_effects as effects
from eimemory.api.runtime import Runtime
from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager


def _body():
    return dict(schema="eimemory.code_deployment_job.v1", operation="deploy", transaction_id="tx-native-job",
        scope=dict(tenant_id="tenant", agent_id="agent", workspace_id="channel", user_id="business"),
        base_commit="a"*40, candidate_commit="b"*40, target_commit="b"*40, controller_commit="c"*40,
        installer_digest="1"*64, policy_digest="2"*64, authorization_digest="3"*64,
        patch_digest="4"*64, candidate_tree_digest="5"*64,
        verification_receipt_digests=["6"*64,"7"*64,"8"*64], observation_deadline="2030-01-01T00:00:00+00:00")


@pytest.mark.parametrize("mutation", [
    {"scope": {"agent_id":"a", "workspace_id":"w", "user_id":"u"}},
    {"target_commit":"a"*40}, {"controller_commit":"other"}, {"env":{"HOME":"/root"}},
    {"verification_receipt_digests":["6"*64]*3}, {"observation_deadline":"2030-01-01T00:00:00"},
])
def test_managed_deployment_request_rejects_missing_owner_and_command_authority(mutation):
    with pytest.raises(ValueError):
        validate_request({**_body(), **mutation})


@pytest.fixture
def durable_job_fixture(tmp_path, monkeypatch):
    """Use the actual effect owner/receipts/consumption to reach DEPLOY_INTENT."""
    from tests.test_code_evolution_effects import _ready_manager, _RecordingAdapter, _policy, TX_ID
    from eimemory.storage.code_evolution_store import digest_json
    runtime = Runtime.create(root=tmp_path/"runtime")
    manager = _ready_manager(runtime)
    class PendingAdapter(_RecordingAdapter):
        def deploy(self, *args, **kw):
            self.calls.append("deploy")
            return effects.DeploymentResult(False,"","","",{"pending":True,"job_key":"f"*64})
    adapter = PendingAdapter(tmp_path/"candidate")
    def consume(*, transaction_id, expected_digest, **_kw):
        material = {"transaction_id":transaction_id,"policy_digest":expected_digest}
        return {"ok":True, **manager.store.consume_policy(transaction_id=transaction_id,
            policy_digest=expected_digest,authorization_receipt_digest=digest_json(material),
            payload={"authorization_material":material,"authorized_policy":_policy()})}
    owner = effects.CodeEvolutionEffectOwner(runtime,owner_id="effect-test",adapter=adapter,
        policy_loader=_policy,policy_consumer=consume)
    result=owner.execute(TX_ID)
    assert result["pending"] is True and manager.store.get_transaction(TX_ID)["current_state"]=="DEPLOY_INTENT"
    assert "rollback" not in adapter.calls
    root=tmp_path/"jobs"
    monkeypatch.setattr(jobs,"JOBS_ROOT",root)
    core=ModuleType("eimemory.core.runtime_manager")
    native={"ActiveState":"activating","InvocationID":"e"*32,"Result":"success"}
    calls=[]
    accepted=set()
    def submit(key):
        calls.append(key)
        accepted.add(key)
        return {"ok":True,"state":dict(native)}
    core.submit_deployment_job=submit
    core.deployment_job_status=lambda key:{"ok":True,"state":dict(native)}
    core.read_registry=lambda:{"controller_commit":"c"*40}
    monkeypatch.setitem(sys.modules,"eimemory.core.runtime_manager",core)
    f=SimpleNamespace(runtime=runtime,manager=manager,txid=TX_ID,policy=_policy(),native=native,
        calls=calls,accepted=accepted,root=root,core=core)
    try:yield f
    finally:runtime.close()


def test_production_adapter_handoff_survives_owner_restart_without_duplicate_job(durable_job_fixture):
    f=durable_job_fixture
    tx=f.manager.store.get_transaction(f.txid)
    adapter=effects.ProductionEffectAdapter()
    first=adapter.deploy(f.runtime,transaction=tx,policy=f.policy,
        verification_receipt_digests=tx["payload"]["verification_receipt_digests"],
        observation_deadline=tx["observation_deadline"],heartbeat=lambda:None)
    assert first.evidence["pending"] is True
    tx=f.manager.store.get_transaction(f.txid)
    key=first.evidence["job_key"]
    assert jobs._read(f.root/key/"request.json")["scope"]=={k:tx[k] for k in _body()["scope"]}
    again=adapter.deploy(f.runtime,transaction=tx,policy=f.policy,
        verification_receipt_digests=tx["payload"]["verification_receipt_digests"],
        observation_deadline=tx["observation_deadline"],heartbeat=lambda:None)
    assert again.evidence["job_key"]==key and f.accepted=={key}
    assert len(list(f.root.iterdir()))==1
    # Root-side submit is idempotent. A lost reply may be read again under
    # the SAME key; a new process cannot invent another controller/request.
    assert f.calls==[key,key]


def test_managed_job_active_recovery_requires_physical_binding_not_external_flag(durable_job_fixture):
    from eimemory.governance.evolution.code_evolution_transaction import recover_transaction
    f=durable_job_fixture; tx=f.manager.store.get_transaction(f.txid)
    handoff=jobs.handoff(f.runtime,tx,f.policy,operation="deploy")
    recovered=recover_transaction(CodeEvolutionTransactionManager(f.runtime,owner_id="restart"),f.txid,
        external_state={"managed_job_pending":True})
    assert recovered["current_state"]=="DEPLOY_INTENT"
    assert recovered["managed_job_pending"]["job_key"]==handoff["job_key"]
    # The same arbitrary flag cannot defer a terminal/missing native unit.
    f.native.update(ActiveState="failed",Result="exit-code")
    assert jobs.pending(f.runtime,f.manager.store.get_transaction(f.txid)) is None


def test_managed_job_scope_mutation_and_native_invocation_replacement_are_rejected(durable_job_fixture):
    f=durable_job_fixture;tx=f.manager.store.get_transaction(f.txid)
    evidence=jobs.handoff(f.runtime,tx,f.policy,operation="deploy");key=evidence["job_key"]
    changed=deepcopy(f.manager.store.get_transaction(f.txid));changed["tenant_id"]="other"
    with pytest.raises(ValueError,match="transaction_mismatch"):
        jobs._bound_request(changed,"deploy")
    jobs._exclusive(f.root/key/"started.json",dict(request_key=key,invocation="d"*32))
    jobs._exclusive(f.root/key/"result.json",dict(request_key=key,invocation="d"*32,result={"ok":True}))
    with pytest.raises(ValueError,match="identity_mismatch"):
        jobs.job_state(key,jobs._read(f.root/key/"request.json"))


def test_managed_job_waits_for_parent_lease_before_any_installer(durable_job_fixture,monkeypatch):
    f=durable_job_fixture; tx=f.manager.store.get_transaction(f.txid)
    evidence=jobs.handoff(f.runtime,tx,f.policy,operation="deploy");key=evidence["job_key"]
    parent=CodeEvolutionTransactionManager(f.runtime,owner_id="parent-watch")
    parent.acquire_lease(f.txid)
    monkeypatch.setattr(jobs,"_controller_commit",lambda:"c"*40)
    monkeypatch.setenv("INVOCATION_ID","e"*32)
    monkeypatch.setattr(effects.ProductionEffectAdapter,"run_managed_installer",lambda *a,**kw:pytest.fail("parent lease still held"))
    with pytest.raises(RuntimeError,match="parent_lease_not_released"):
        jobs.run_job(f.runtime,key,wait_seconds=0)
    assert not (f.root/key/"started.json").exists()
    parent.release_lease(f.txid)


def test_job_files_refuse_overwrite_and_do_not_treat_read_atime_as_mutation(tmp_path):
    root=tmp_path/"private";root.mkdir(mode=0o700)
    path=root/"intent.json"
    jobs._exclusive(path,{"original":True})
    assert jobs._read(path)==jobs._read(path)=={"original":True}
    with pytest.raises(FileExistsError):jobs._exclusive(path,{"original":False})
    assert jobs._read(path)=={"original":True}


def test_job_closes_sqlite_before_installer_and_reopens_for_same_lease_and_result(durable_job_fixture,monkeypatch):
    import sqlite3
    f=durable_job_fixture; tx=f.manager.store.get_transaction(f.txid)
    evidence=jobs.handoff(f.runtime,tx,f.policy,operation="deploy");key=evidence["job_key"]
    monkeypatch.setattr(jobs,"_controller_commit",lambda:"c"*40)
    monkeypatch.setenv("INVOCATION_ID","e"*32)
    # Isolate live provider admission, which has separate actual socket tests.
    # Everything from ledger/receipts/lease through result persistence is real.
    monkeypatch.setattr(effects,"_live_proposal_authority_error",lambda *a:"")
    monkeypatch.setattr(effects,"_dynamic_patch_authority_error",lambda *a,**kw:"")
    from eimemory.governance.evolution import code_automation_policy_owner
    monkeypatch.setattr(code_automation_policy_owner,"verify_native_standing_authority",
        lambda *a,**kw:{"ok":True,"status":"not_native"},raising=False)
    calls=[]
    def installer(_self,runtime,**kw):
        with pytest.raises(sqlite3.ProgrammingError):
            runtime.store.sqlite.conn.execute("SELECT 1")
        assert kw["job_key"]==key and kw["operation"]=="deploy"
        calls.append("installer")
        return {"ok":False,"commit":"","receipt_digest":"","reason":"isolated_installer_failure"}
    monkeypatch.setattr(effects.ProductionEffectAdapter,"run_managed_installer",installer)
    assert jobs.run_job(f.runtime,key,wait_seconds=0)["ok"] is False
    assert calls==["installer"]
    result=jobs._read(f.root/key/"result.json")
    assert result["invocation"]=="e"*32 and result["request_key"]==key
    second=Runtime.create(root=f.runtime.store.root)
    try:
        # Even an explicit restart of the same native unit cannot replay a
        # prior execution claim after a process/lease handoff.
        with pytest.raises((FileExistsError,ValueError)):jobs.run_job(second,key,wait_seconds=0)
        assert calls==["installer"]
    finally:second.close()


def test_strict_deployment_environment_preserves_all_four_owner_coordinates():
    scope=_body()["scope"]
    tx={**scope, "transaction_id":"t"}
    env=effects._deployment_environment(SimpleNamespace(root=Path("/state")),transaction=tx,
        verification_receipt_digests=[],observation_deadline="",lineage={})
    assert {k:env["EIMEMORY_DEPLOY_SCOPE_"+v] for k,v in
        (("tenant_id","TENANT"),("agent_id","AGENT"),("workspace_id","WORKSPACE"),("user_id","USER"))}==scope
