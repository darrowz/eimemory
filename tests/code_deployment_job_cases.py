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
    policy = _policy()
    prior = tmp_path / "releases" / manager.store.get_transaction(TX_ID)["base_commit"]
    prior.mkdir(parents=True)
    policy["deployment"]["current_link"] = str(prior)
    class PendingAdapter(_RecordingAdapter):
        def deploy(self, *args, **kw):
            self.calls.append("deploy")
            return effects.DeploymentResult(False,"","","",{"pending":True,"job_key":"f"*64})
    adapter = PendingAdapter(tmp_path/"candidate")
    def consume(*, transaction_id, expected_digest, **_kw):
        material = {"transaction_id":transaction_id,"policy_digest":expected_digest}
        return {"ok":True, **manager.store.consume_policy(transaction_id=transaction_id,
            policy_digest=expected_digest,authorization_receipt_digest=digest_json(material),
            payload={"authorization_material":material,"authorized_policy":policy})}
    owner = effects.CodeEvolutionEffectOwner(runtime,owner_id="effect-test",adapter=adapter,
        policy_loader=lambda:policy,policy_consumer=consume)
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
    f=SimpleNamespace(runtime=runtime,manager=manager,txid=TX_ID,policy=policy,native=native,
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


def test_fixed_job_launcher_argv_matches_entry_without_request_selected_paths(monkeypatch):
    from deploy import run_code_deployment_job as entry
    key="e"*64
    seen=[]
    runtime=SimpleNamespace(close=lambda:seen.append("closed"))
    monkeypatch.setattr(entry.os,"geteuid",lambda:1001,raising=False)
    monkeypatch.setattr(entry.sys,"argv",["/fixed/controller/deploy/run_code_deployment_job.py","--job-key",key])
    monkeypatch.setattr(Runtime,"create",lambda **kw:seen.append(kw) or runtime)
    monkeypatch.setattr(jobs,"run_job",lambda rt,value:seen.append((rt,value)) or {"ok":False})
    assert entry.main()==1
    assert seen==[{"root":"/var/lib/eimemory"},(runtime,key),"closed"]
    monkeypatch.setattr(entry.sys,"argv",["entry",key])
    with pytest.raises(SystemExit):entry.main()


@pytest.mark.parametrize("business_passed,completion_lost", [(True,False),(False,False),(True,True)])
def test_worker_durable_outcome_waits_for_restoration_business_and_native_complete(
    durable_job_fixture, monkeypatch, business_passed, completion_lost,
):
    """Real ledger/lease sequencing; isolated installer/authority transports.

    This tests the crash/ordering boundary, not native installation or real
    business qualification. Those transports have separate integration gates.
    """
    f=durable_job_fixture
    tx=f.manager.store.get_transaction(f.txid)
    key=jobs.handoff(f.runtime,tx,f.policy,operation="deploy")["job_key"]
    monkeypatch.setattr(jobs,"_controller_commit",lambda:"c"*40)
    monkeypatch.setenv("INVOCATION_ID","e"*32)
    monkeypatch.setattr(effects,"_live_proposal_authority_error",lambda *a:"")
    monkeypatch.setattr(effects,"_dynamic_patch_authority_error",lambda *a,**kw:"")
    from eimemory.governance.evolution import code_automation_policy_owner
    monkeypatch.setattr(code_automation_policy_owner,"verify_native_standing_authority",
        lambda *a,**kw:{"ok":True},raising=False)
    monkeypatch.setattr(jobs,"post_resume_business_error",lambda *a:"")
    sequence=[]
    def ledger_state():
        probe=Runtime.create(root=f.runtime.store.root)
        try:return CodeEvolutionTransactionManager(probe).store.get_transaction(f.txid)["current_state"]
        finally:probe.close()
    def install(*a,**kw):
        sequence.append("install")
        return {"ok":True,"commit":tx["candidate_commit"],"version":"1.0.0","receipt_digest":"a"*64}
    monkeypatch.setattr(effects.ProductionEffectAdapter,"run_managed_installer",install)
    monkeypatch.setattr(effects.ProductionEffectAdapter,"_verify_managed_result",lambda *a,**kw:None)
    def finalize(value):
        assert value==key and ledger_state()=="HEALTHY"
        assert not (f.root/key/"result.json").exists()
        assert jobs._read(f.root/key/"technical-install.json")["result"]["ok"] is True
        sequence.append("restore")
        return {"job_key":key,"resumed":True,"phase":"resumed_awaiting_business"}
    def business(_adapter,runtime,**kw):
        assert sequence==["install","restore"] and ledger_state()=="HEALTHY"
        assert not (f.root/key/"result.json").exists()
        sequence.append("business")
        return {"ok":business_passed,"business_qualified":business_passed}
    def complete(value):
        assert value==key and ledger_state()=="OBSERVING"
        assert sequence==["install","restore","business"]
        assert not (f.root/key/"result.json").exists()
        sequence.append("complete")
        if completion_lost:raise TimeoutError("isolated_lost_ack")
        return {"job_key":key,"complete":True,"resumed":True,"phase":"completed","operational_complete":True}
    f.core.finalize_deployment_job=finalize
    f.core.complete_deployment_job=complete
    monkeypatch.setattr(effects.ProductionEffectAdapter,"run_post_resume_business",business)
    result=jobs.run_job(f.runtime,key,wait_seconds=0)
    assert jobs._read(f.root/key/"result.json")["result"]==result
    assert jobs._read(f.root/key/"technical-install.json")["result"]["ok"] is True
    assert result["ok"] is (business_passed and not completion_lost)
    assert result["operational_complete"] is (business_passed and not completion_lost)
    assert result["ledger_state"]==("OBSERVING" if business_passed else "ROLLBACK_INTENT")
    assert sequence==["install","restore","business"]+(["complete"] if business_passed else [])
    if completion_lost:
        assert result["reconciliation_required"] is True
        assert result["status"]=="native_completion_unknown"


def test_managed_healthy_without_business_proof_cannot_start_observation(durable_job_fixture):
    f=durable_job_fixture
    tx=f.manager.store.get_transaction(f.txid)
    jobs.handoff(f.runtime,tx,f.policy,operation="deploy")
    tx=f.manager.store.get_transaction(f.txid)
    # Missing provenance is rejected before importing any standing authority
    # producer or interpreting a caller's apparent success dictionary.
    assert jobs.post_resume_business_error(f.runtime,tx)=="managed_post_resume_business_required"


@pytest.mark.parametrize("fault", ["", "phase", "invocation", "original", "runtime", "controller", "current"])
def test_observation_uses_current_root_completion_not_a_success_boolean(tmp_path,monkeypatch,fault):
    body=_body();key=request_key(body)
    root=tmp_path/"jobs";jobs._directory(root/key,create=True)
    monkeypatch.setattr(jobs,"JOBS_ROOT",root)
    jobs._exclusive(root/key/"started.json",{"request_key":key,"invocation":"e"*32})
    current=tmp_path/"releases"/("d"*40 if fault=="current" else body["candidate_commit"])
    current.mkdir(parents=True)
    transaction={"payload":{"authorized_policy":{"deployment":{"current_link":str(current)}}}}
    monkeypatch.setattr(jobs,"_bound_request",lambda *a:(key,body))
    core=ModuleType("eimemory.core.runtime_manager")
    native={"manager":"system","unit":f"eimemory-code-deployment-job@{key}.service",
        "phase":"resumed_awaiting_business" if fault=="phase" else "completed",
        "original_invocation":"f"*32 if fault=="original" else "e"*32,
        "state":{"InvocationID":"f"*32 if fault=="invocation" else ""}}
    core.deployment_job_status=lambda value:native
    core.read_registry=lambda:{"runtime_commit":"d"*40 if fault=="runtime" else body["candidate_commit"],
        "controller_commit":"d"*40 if fault=="controller" else body["controller_commit"]}
    monkeypatch.setitem(sys.modules,"eimemory.core.runtime_manager",core)
    assert bool(jobs.observation_completion_error(transaction)) is bool(fault)


@pytest.mark.parametrize("phase,policy_exists", [("completed",True),("resumed_awaiting_business",False),
                                               ("resumed_awaiting_business",True)])
def test_post_resume_entry_requires_native_resume_and_calls_full_business_not_strict_preflight(
    durable_job_fixture,monkeypatch,phase,policy_exists,
):
    from eimemory.governance.release import release_closure as closure
    from eimemory.evaluation import controlled_production_acceptance as acceptance
    from eimemory.governance.l5 import l5_readiness
    from eimemory.governance.safety import prompt_safety_executor
    f=durable_job_fixture;tx=f.manager.store.get_transaction(f.txid)
    key=jobs.handoff(f.runtime,tx,f.policy,operation="deploy")["job_key"]
    for state in ("DEPLOYED_VERIFIED","HEALTHY"):f.manager.transition(f.txid,state)
    body=jobs._read(f.root/key/"request.json")
    jobs._exclusive(f.root/key/"started.json",{"request_key":key,"invocation":"e"*32})
    jobs._exclusive(f.root/key/"finalized.json",{"request_key":key,"invocation":"e"*32,
        "native":{"resumed":True,"phase":"resumed_awaiting_business"}})
    monkeypatch.setenv("INVOCATION_ID","d"*32)
    f.core.deployment_job_status=lambda value:{"manager":"system","unit":f"eimemory-code-deployment-job@{key}.service",
        "phase":phase,"original_invocation":"e"*32,"state":{"ActiveState":"activating","InvocationID":"e"*32}}
    child={"manager":"system","job_key":key,"operation":"post_resume",
        "unit":jobs._validation_unit(key,"post_resume"),"target_commit":body["target_commit"],
        "controller_commit":body["controller_commit"],"artifact_basename":"validation-post_resume-result.json",
        "parent_original_invocation":"e"*32,"original_invocation":"d"*32,"phase":phase,
        "state":{"ActiveState":"activating","InvocationID":"d"*32,
            "ControlGroup":"/system.slice/"+jobs._validation_unit(key,"post_resume")}}
    monkeypatch.setattr(jobs,"_native_child_cgroup",lambda:"0::"+child["state"]["ControlGroup"])
    f.core.deployment_validation_status=lambda *a:child
    f.core.read_registry=lambda:{"runtime_commit":body["candidate_commit"],"controller_commit":body["controller_commit"]}
    receipt={"ok":True,"commit":body["candidate_commit"],"version":"1.0.0",
        "transaction_id":f.txid,"promotion_request_id":"strict-original","release_session_id":"session-original"}
    monkeypatch.setattr(closure,"read_strict_deployment_receipt",lambda *a,**kw:receipt)
    monkeypatch.setattr(acceptance,"load_controlled_policy",lambda *a:{"purpose":"historical_business_qualification"} if policy_exists else None)
    monkeypatch.setattr(l5_readiness,"_storage_migration_status",lambda *a:{"ok":True})
    monkeypatch.setattr(prompt_safety_executor,"bind_prompt_safety_from_service",lambda *a:None)
    seen=[]
    def controlled(runtime,**kw):
        seen.append(kw)
        return {**kw["report"],"ok":False,"blocked_stage":"isolated_actual_business_transport"}
    monkeypatch.setattr(closure,"_run_controlled_operational_closure",controlled)
    result=closure.run_managed_post_resume_closure(f.runtime,job_key=key,validation_context=child)
    if phase!="resumed_awaiting_business":
        assert result["blocked_reason"]=="native_restoration_not_verified" and seen==[]
    elif not policy_exists:
        assert result["blocked_reason"]=="historical_business_policy_required" and seen==[]
    else:
        assert result["blocked_stage"]=="isolated_actual_business_transport" and len(seen)==1
        assert seen[0]["scope"]==body["scope"]
        assert seen[0]["identity_kwargs"]["deployment_receipt"] is receipt
        assert seen[0]["identity_kwargs"]["deployed_commit"]==body["candidate_commit"]
        assert result["observation_started"] is False and result["business_qualified"] is False


@pytest.mark.skipif(os.name!="posix",reason="real managed process groups require POSIX")
def test_native_json_report_keeps_actual_stderr_out_of_receipt_json(tmp_path):
    report,transport,raw,errors=effects._run_native_json_report(
        [sys.executable,"-I","-B","-c","import sys; print('{\"ok\":true}'); print('diagnostic',file=sys.stderr)"],
        cwd=tmp_path,env=dict(os.environ),heartbeat=lambda:None)
    assert report=={"ok":True} and transport["exit_status"]==0
    assert b"diagnostic" in errors and b"diagnostic" not in raw


def test_restoration_transport_renews_sql_only_on_original_thread_and_propagates_failure():
    import threading
    main=threading.get_ident(); socket_thread=[]; renewals=[]
    done=threading.Event()
    def renew(transaction_id):
        assert transaction_id=="tx" and threading.get_ident()==main
        renewals.append(transaction_id)
        if len(renewals)==2:done.set()
    def restore():
        socket_thread.append(threading.get_ident())
        assert done.wait(1)
        return {"resumed":True}
    assert jobs._after_storage_call(restore,manager=SimpleNamespace(renew_lease=renew),
        transaction_id="tx",interval=.01)=={"resumed":True}
    assert socket_thread[0]!=main and len(renewals)>=2
    def failed_transport():raise TimeoutError("bounded_native_timeout")
    with pytest.raises(TimeoutError,match="bounded_native_timeout"):
        jobs._after_storage_call(failed_transport,manager=SimpleNamespace(renew_lease=renew),transaction_id="tx",interval=.01)


def test_post_resume_uses_managed_readonly_child_instead_of_parent_interpreter(tmp_path,monkeypatch):
    release=tmp_path/"releases"/("b"*40);release.mkdir(parents=True)
    key="d"*64;root=tmp_path/"jobs";jobs._directory(root/key,create=True)
    monkeypatch.setattr(jobs,"JOBS_ROOT",root)
    transaction={**_body()["scope"],"candidate_commit":release.name,"payload":{
        "verification_receipt_digests":["6"*64,"7"*64,"8"*64]},"observation_deadline":"2030-01-01T00:00:00Z"}
    seen=[]
    def native(value,operation,**kwargs):
        seen.append((value,operation));kwargs["heartbeat"]()
        return {"ok":False,"business_qualified":False},{"exit_status":1,"manager":"system","source_readonly":True}
    monkeypatch.setattr(jobs,"managed_validation_report",native)
    monkeypatch.setattr(effects,"_run_native_json_report",lambda *a,**kw:pytest.fail("parent namespace must not execute target"))
    runtime=SimpleNamespace(store=SimpleNamespace(root=tmp_path/"runtime"))
    result=effects.ProductionEffectAdapter().run_post_resume_business(runtime,transaction=transaction,
        policy={"deployment":{"current_link":str(release)}},job_key=key,heartbeat=lambda:None)
    assert seen==[(key,"post_resume")]
    assert result["business_qualified"] is False and result["native_transport"]["exit_status"]==1


@pytest.fixture
def managed_validation_fixture(tmp_path,monkeypatch):
    from hashlib import sha256
    body=_body();key=request_key(body);operation="receipt";root=tmp_path/"jobs"
    jobs._directory(root/key,create=True);monkeypatch.setattr(jobs,"JOBS_ROOT",root)
    jobs._exclusive(root/key/"request.json",body)
    jobs._exclusive(root/key/"started.json",{"request_key":key,"invocation":"e"*32})
    context={"schema":"managed_code_deployment_validation.v1","job_key":key,"operation":operation,
        "parent_invocation":"e"*32,"invocation":"d"*32,"target_commit":body["target_commit"],
        "controller_commit":body["controller_commit"],"scope":body["scope"],"source_readonly":True}
    artifact={**context,"report":{"ok":True,"commit":body["target_commit"]},
        "target_transport":{"exit_status":0,"executed":True}}
    jobs._exclusive(root/key/"validation-receipt-started.json",context)
    jobs._exclusive(root/key/"validation-receipt-result.json",artifact)
    native={"job_key":key,"operation":operation,"manager":"system","unit":jobs._validation_unit(key,operation),
        "original_invocation":"d"*32,"parent_original_invocation":"e"*32,
        "target_commit":body["target_commit"],"controller_commit":body["controller_commit"],
        "phase":"resume_deferred_until_terminal","artifact_basename":"validation-receipt-result.json",
        "artifact_sha256":sha256(jobs.canonical_bytes(artifact)).hexdigest(),
        "state":{"LoadState":"loaded","ActiveState":"inactive","SubState":"dead","InvocationID":"d"*32,
                 "Result":"success","ExecMainStatus":"0","ControlGroup":""}}
    native["terminal_state"]=deepcopy(native["state"])
    def root_envelope():
        envelope={"schema":"eimemory.deployment_validation_seal.v1","job_key":key,"operation":operation,
            "invocation":"d"*32,"parent_invocation":"e"*32,"artifact_sha256":native["artifact_sha256"],
            "raw_hex":jobs.canonical_bytes(artifact).hex()}
        raw=jobs.canonical_bytes(envelope)
        native["sealed_artifact"]={name:value for name,value in envelope.items() if name!="raw_hex"}
        native["sealed_artifact"].update(schema="eimemory.deployment_validation_artifact.v1",
            envelope_sha256=sha256(raw).hexdigest())
        return raw
    sealed_raw=root_envelope()
    monkeypatch.setattr(jobs,"_root_validation_envelope",lambda *a:sealed_raw)
    core=ModuleType("eimemory.core.runtime_manager");calls=[];accepted=set()
    def submit(value,op):
        calls.append((value,op));accepted.add((value,op));return deepcopy(native)
    core.validate_deployment_job=submit
    core.deployment_validation_status=lambda *a:deepcopy(native)
    monkeypatch.setitem(sys.modules,"eimemory.core.runtime_manager",core)
    return SimpleNamespace(key=key,body=body,root=root,native=native,core=core,calls=calls,accepted=accepted,
        artifact=artifact,root_envelope=root_envelope)


def test_managed_validation_reads_root_sealed_child_without_duplicate_execution(managed_validation_fixture):
    f=managed_validation_fixture
    first=jobs.managed_validation_report(f.key,"receipt",heartbeat=lambda:None)
    second=jobs.managed_validation_report(f.key,"receipt",heartbeat=lambda:None)
    assert first==second and first[0]["commit"]==f.body["target_commit"]
    assert f.accepted=={(f.key,"receipt")} and f.calls==[(f.key,"receipt")]*2
    assert first[1]["invocation"]=="d"*32 and first[1]["parent_invocation"]=="e"*32


@pytest.mark.parametrize("field,value", [("parent_original_invocation","f"*32),("original_invocation","f"*32),
    ("target_commit","f"*40),("controller_commit","f"*40),("manager","user"),
    ("unit","eimemory-code-validation-receipt@other.service"),("artifact_sha256","f"*64)])
def test_managed_validation_rejects_other_child_parent_target_or_mutable_artifact(managed_validation_fixture,field,value):
    f=managed_validation_fixture;f.native[field]=value
    with pytest.raises(ValueError):jobs.managed_validation_report(f.key,"receipt",heartbeat=lambda:None)


def test_managed_validation_unknown_does_not_issue_another_key_or_rerun(managed_validation_fixture):
    f=managed_validation_fixture
    f.core.validate_deployment_job=lambda *a:(_ for _ in ()).throw(TimeoutError("original_root_request_unknown"))
    with pytest.raises(TimeoutError):jobs.managed_validation_report(f.key,"receipt",heartbeat=lambda:None)
    assert f.calls==[] and len(list(f.root.iterdir()))==1


def test_managed_validation_unit_gc_preserves_original_root_sealed_exit(managed_validation_fixture):
    f=managed_validation_fixture
    f.native["state"]={"LoadState":"not-found","ActiveState":"inactive","InvocationID":"",
        "Result":"","ExecMainStatus":"0","ControlGroup":""}
    report,transport=jobs.managed_validation_report(f.key,"receipt",heartbeat=lambda:None)
    assert report["ok"] is True and transport["native_state"]["Result"]=="success"
    assert transport["current_native_state"]["LoadState"]=="not-found"
    assert transport["invocation"]=="d"*32


@pytest.mark.parametrize("change",["replace","remove"])
def test_managed_validation_uses_root_original_after_uid_artifact_tampering(managed_validation_fixture,change):
    f=managed_validation_fixture;path=f.root/f.key/"validation-receipt-result.json"
    path.unlink()
    if change=="replace":jobs._exclusive(path,{"ok":True,"commit":"f"*40})
    report,transport=jobs.managed_validation_report(f.key,"receipt",heartbeat=lambda:None)
    assert report==f.artifact["report"] and report["commit"]==f.body["target_commit"]
    assert transport["artifact_digest"]==f.native["artifact_sha256"]


@pytest.mark.parametrize("fault",["absent","reference","root-bytes","envelope-parent","raw-bytes"])
def test_managed_validation_rejects_missing_or_mismatched_root_seal(managed_validation_fixture,monkeypatch,fault):
    from hashlib import sha256
    f=managed_validation_fixture
    if fault=="absent":f.native.pop("sealed_artifact")
    elif fault=="reference":f.native["sealed_artifact"]["invocation"]="f"*32
    else:
        raw=f.root_envelope();envelope=json.loads(raw)
        if fault=="root-bytes":raw+=b" "
        else:
            if fault=="envelope-parent":envelope["parent_invocation"]="f"*32
            else:envelope["raw_hex"]=jobs.canonical_bytes({"ok":True}).hex()
            raw=jobs.canonical_bytes(envelope)
            f.native["sealed_artifact"]["envelope_sha256"]=sha256(raw).hexdigest()
        monkeypatch.setattr(jobs,"_root_validation_envelope",lambda *a:raw)
    with pytest.raises(ValueError):jobs.managed_validation_report(f.key,"receipt",heartbeat=lambda:None)


@pytest.mark.parametrize("fault",["missing-seal","other-invocation","running-again","seal-not-terminal",
    "failed-exit","false-success","not-executed"])
def test_managed_validation_gc_never_turns_unknown_or_failed_exit_into_success(managed_validation_fixture,fault):
    f=managed_validation_fixture
    f.native["state"]={"LoadState":"not-found","ActiveState":"inactive","InvocationID":""}
    if fault=="missing-seal":f.native.pop("terminal_state")
    elif fault=="other-invocation":f.native["terminal_state"]["InvocationID"]="f"*32
    elif fault=="running-again":f.native["state"].update(ActiveState="active",InvocationID="d"*32)
    elif fault=="seal-not-terminal":f.native["terminal_state"]["ActiveState"]="active"
    elif fault=="failed-exit":
        f.native["terminal_state"].update(Result="exit-code",ExecMainStatus="1")
        report,transport=jobs.managed_validation_report(f.key,"receipt",heartbeat=lambda:None)
        assert report["ok"] is False and transport["exit_status"]==1
        return
    else:
        artifact=f.artifact
        artifact["target_transport"]["executed" if fault=="not-executed" else "exit_status"]=False if fault=="not-executed" else 1
        (f.root/f.key/"validation-receipt-result.json").unlink()
        jobs._exclusive(f.root/f.key/"validation-receipt-result.json",artifact)
        from hashlib import sha256
        f.native["artifact_sha256"]=sha256(jobs.canonical_bytes(artifact)).hexdigest()
        sealed_raw=f.root_envelope()
        # This is an explicit root-seal fault fixture; production code never
        # manufactures or replaces the protected envelope from an app file.
        # Parent tests only double the root transport/storage boundary.
        f.core.validate_deployment_job=lambda *a:deepcopy(f.native)
        from unittest.mock import patch
        with patch.object(jobs,"_root_validation_envelope",return_value=sealed_raw):
            with pytest.raises(ValueError):jobs.managed_validation_report(f.key,"receipt",heartbeat=lambda:None)
        return
    with pytest.raises(ValueError):jobs.managed_validation_report(f.key,"receipt",heartbeat=lambda:None)


@pytest.mark.parametrize("fault",["","unit","group","user-manager","other-cgroup","sub-cgroup","invocation",
    "parent","owner","target","controller","inactive","uid","gid","nnp","effective-caps","permitted-caps","ambient-caps"])
def test_validation_entry_authenticates_kernel_identity_before_product_import(fault):
    from deploy import run_code_deployment_validation as entry
    body=_body();key=request_key(body);unit=jobs._validation_unit(key,"receipt")
    group="/system.slice/"+unit
    native={"job_key":key,"operation":"receipt","manager":"system","unit":unit,
        "target_commit":body["target_commit"],"controller_commit":body["controller_commit"],
        "artifact_basename":"validation-receipt-result.json","original_invocation":"d"*32,
        "parent_original_invocation":"e"*32,
        "state":{"InvocationID":"d"*32,"ControlGroup":group,"ActiveState":"activating"}}
    status="Uid:\t1001 1001 1001 1001\nGid:\t1001 1001 1001 1001\nNoNewPrivs:\t1\nCapEff:\t0000\nCapPrm:\t0000\nCapAmb:\t0000"
    actual="0::"+group
    if fault=="unit":native["unit"]="eimemory-rpc.service"
    elif fault=="group":native["state"]["ControlGroup"]="/system.slice/eimemory-rpc.service"
    elif fault=="user-manager":native["state"]["ControlGroup"]=group.replace("system.slice","user.slice")
    elif fault=="other-cgroup":actual="0::/system.slice/eimemory-rpc.service"
    elif fault=="sub-cgroup":actual+="/unreviewed"
    elif fault=="invocation":native["original_invocation"]="f"*32
    elif fault=="parent":native["parent_original_invocation"]=None
    elif fault=="owner":native["manager"]="user"
    elif fault in {"target","controller"}:native[fault+"_commit"]="f"*40
    elif fault=="inactive":native["state"]["ActiveState"]="inactive"
    elif fault=="uid":status=status.replace("Uid:\t1001", "Uid:\t0",1)
    elif fault=="gid":status=status.replace("Gid:\t1001", "Gid:\t0",1)
    elif fault=="nnp":status=status.replace("NoNewPrivs:\t1","NoNewPrivs:\t0")
    elif fault in {"effective-caps","permitted-caps","ambient-caps"}:
        field={"effective-caps":"CapEff","permitted-caps":"CapPrm","ambient-caps":"CapAmb"}[fault]
        status=status.replace(field+":\t0000",field+":\t0001")
    if fault:
        with pytest.raises(ValueError,match="actual_execution_identity"):
            entry._assert_execution_identity(body,key,"receipt",native,"d"*32,status,actual)
    else:
        entry._assert_execution_identity(body,key,"receipt",native,"d"*32,status,actual)
        jobs._assert_child_cgroup(native,actual)


def test_validation_bootstrap_is_stdlib_only_and_precedes_all_product_imports():
    import ast
    from deploy import run_code_deployment_validation as entry
    source=Path(entry.__file__).read_text(encoding="utf8")
    tree=ast.parse(source)
    assert all(not isinstance(node,(ast.Import,ast.ImportFrom)) or
        (getattr(node,"module","") or "").split(".")[0] not in {"eimemory","deploy"} for node in tree.body)
    main=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=="main")
    identity=next(node.lineno for node in ast.walk(main) if isinstance(node,ast.Call)
        and isinstance(node.func,ast.Name) and node.func.id=="_assert_execution_identity")
    assert all(identity<node.lineno for node in ast.walk(main) if isinstance(node,ast.ImportFrom)
        and (node.module or "").split(".")[0] in {"eimemory","deploy"})
    with pytest.raises(ValueError,match="duplicate_json_key"):entry._strict_json(b'{"ok":true,"ok":false}')


def test_validation_entry_cannot_rewrite_its_own_authority_even_with_exact_allow_pin():
    from eimemory.governance.evolution.code_evolution_path_policy import path_allowed_for_evolution
    path="deploy/run_code_deployment_validation.py"
    for custom_denies in (None,()):
        assert path_allowed_for_evolution(path,allowed_path_globs=("**",),exact_allow_files=(path,),
            denied_path_globs=(),deny_self_paths=custom_denies)==(False,"deny_self")


@pytest.mark.skipif(os.name!="posix",reason="actual directory fsync/no-follow requires POSIX")
def test_new_job_directory_syncs_each_new_parent_and_rejects_symlink(tmp_path,monkeypatch):
    observed=[]
    real=os.fsync
    def sync(fd):
        observed.append(os.readlink(f"/proc/self/fd/{fd}"))
        real(fd)
    monkeypatch.setattr(jobs.os,"fsync",sync)
    path=tmp_path/"new"/"key"
    jobs._directory(path,create=True)
    assert str(tmp_path) in observed and str(tmp_path/"new") in observed
    linked=tmp_path/"linked";linked.symlink_to(path,target_is_directory=True)
    with pytest.raises((ValueError,OSError)):jobs._directory(linked/"child",create=True)
    assert not (path/"child").exists()
