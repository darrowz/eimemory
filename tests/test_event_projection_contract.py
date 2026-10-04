"""Bounded leaf acceptance: real isolated SQLite, no Runtime or operational hooks.

Run directly with unittest so repository autouse environment/bootstrap fixtures
are not executed. Fault injection is limited to persistence boundaries; the
projection, model, exact lookup, lineage, and SQLite writes are real code.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from types import SimpleNamespace
import unittest

from eimemory.governance.learning import event_graph as graph
from eimemory.knowledge.evidence_contracts import versioned_record_ref
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage.sqlite_store import SqliteRecordStore

ISOLATED_ENTRY_OBSERVATIONS = {}

SCOPE = ScopeRef(tenant_id="projection-test", agent_id="agent", workspace_id="workspace", user_id="user")


def source_record(*, scope=SCOPE, source_id="default", status="active", record_id="trace_source"):
    return RecordEnvelope(record_id=record_id, kind="reflection", status=status, title="Outcome trace",
        summary="Synthetic bounded source marker", detail="", content={"payload": {"task_type":"fixture.task", "input_summary":"Synthetic bounded source marker"}},
        tags=[], links=[], evidence=[], source="eimemory.experience.outcome_trace", source_id=source_id,
        scope=scope, time=TimeRef("2020-01-01", "2020-01-01", "2020-01-01"), provenance={}, meta={"report_type":"outcome_trace"})


class SQLiteOwner:
    """Minimal test owner for the production callback and real SQLite transaction."""
    def __init__(self, path):
        self.sqlite = SqliteRecordStore(path, archive_writes=False)
        self.lock = RLock()
        self.sqlite.bind_runtime_lock(self.lock)
        self.memory_fault = ""

    def put(self, record):
        with self.lock:
            self.sqlite.upsert(record)

    def get_by_id(self, record_id, *, scope, exact_scope=False):
        with self.lock:
            return self.sqlite.get_by_id(record_id, scope=scope, exact_scope=exact_scope)

    def get_by_exact_ref(self, record_id, *, scope, source_id):
        with self.lock:
            return self.sqlite.get_by_exact_ref(record_id, scope=scope, source_id=source_id)

    def mutate_records_atomically(self, mutation):
        with self.lock:
            memory_stage = mutation.__name__ == "mutation"
            try:
                self.sqlite.execute("BEGIN IMMEDIATE")
                if memory_stage and self.memory_fault == "before":
                    raise OSError("fixture before write")
                result, records, edges = mutation(self.sqlite)
                self.sqlite.upsert_memory_edges(edges, commit=False)
                if memory_stage and self.memory_fault == "before_commit":
                    raise OSError("fixture before commit")
                self.sqlite.commit()
            except BaseException:
                self.sqlite.rollback()
                raise
            if memory_stage and self.memory_fault == "after_commit":
                raise OSError("fixture after commit")
            return result

    def counts(self):
        with self.lock:
            return {name:self.sqlite.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                    for name in ("records", "events", "event_outcomes", "memory_edges")}

    def close(self):
        self.sqlite.conn.close()


class Context:
    def __init__(self, store):
        self.store = store
        self.mode = ""
        self.calls = []

    def record_event(self, payload, *, scope):
        self.calls.append("event")
        if self.mode == "event_failure":
            return {"ok":False,"error":"fixture"}
        if self.mode == "event_foreign_id":
            return {"ok":True,"id":"foreign_event"}
        if self.mode == "event_no_write":
            return dict(payload)
        with self.store.lock:
            result = self.store.sqlite.record_event(payload, scope=ScopeRef.from_dict(scope))
        if self.mode == "parent_after_event":
            self.change_parent()
        if self.mode == "event_failure_after_commit":
            return {"ok":False,"error":"fixture"}
        return result

    def record_outcome(self, event_id, payload, *, scope):
        self.calls.append("outcome")
        if self.mode == "outcome_failure":
            raise OSError("fixture outcome failure")
        with self.store.lock:
            result = self.store.sqlite.record_outcome(event_id, payload, scope=ScopeRef.from_dict(scope), apply_rollbacks=False)
        if self.mode == "parent_after_outcome":
            self.change_parent()
        return result

    def change_parent(self):
        record = self.store.get_by_id("trace_source", scope=SCOPE, exact_scope=True)
        record.summary += " changed"
        self.store.put(record)


class EventProjectionContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="event-projection-contract-")
        self.store = SQLiteOwner(Path(self.temp.name)/"records.sqlite")
        self.context = Context(self.store)
        self.source = source_record()
        self.store.put(self.source)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def project(self, *, ok=True, memory_update=None, result=None, scope=SCOPE):
        args = {"result":result or {"record_id":"trace_source"},
                "eval_result":{"record_id":"trace_source","ok":ok,"primary_label":"success" if ok else "failure"},
                "scope":scope}
        if memory_update is None:
            return graph._project_event_memory_only(self.context, **args)
        return graph.project_experience_event_memory(self.context, memory_update=memory_update, **args)

    def test_01_default_active_success_and_business_failure(self):
        for ok in (True, False):
            with self.subTest(ok=ok):
                # Independent exact scopes keep both legitimate business outcomes.
                scope = ScopeRef(**{**asdict(SCOPE), "user_id":f"business-{ok}"})
                self.store.put(source_record(scope=scope))
                report = self.project(ok=ok, scope=scope)
                self.assertTrue(report["ok"], report)
                self.assertEqual(report["committed_stages"], ["policy_event","policy_outcome","event_memory"])
                memory = self.store.get_by_exact_ref(report["event_record_id"],scope=scope,source_id="default")
                self.assertEqual(memory.content["outcome"], "good" if ok else "bad")
                expected = versioned_record_ref(self.store.get_by_id("trace_source",scope=scope,exact_scope=True))
                self.assertEqual(memory.provenance["source_record_ref"],expected)
                self.assertEqual(memory.meta["source_record_ref"],expected)
                self.assertEqual(memory.source_id,"default")
                self.assertEqual(report["completion_scope"],"event_projection_only")
                self.assertFalse(report["feedback"]["verified"])

    def test_02_repeat_and_foreign_or_legacy_projection_not_adopted(self):
        first=self.project(); self.assertTrue(first["ok"],first)
        counts=self.store.counts()
        again=self.project(); self.assertTrue(again["ok"],again)
        self.assertEqual(first["event_record_id"],again["event_record_id"])
        self.assertEqual(counts,self.store.counts())
        record=self.store.get_by_id(first["event_record_id"],scope=SCOPE,exact_scope=True)
        for mode in ("legacy","foreign","tampered"):
            with self.subTest(mode=mode):
                changed=deepcopy(record)
                if mode=="legacy":
                    changed.meta.pop("source_record_ref")
                elif mode=="foreign":
                    changed.source="other.producer"
                else:
                    changed.content["text"]="foreign content"
                self.store.put(changed); before=self.store.counts(); self.context.calls.clear()
                blocked=self.project()
                self.assertFalse(blocked["ok"],blocked)
                self.assertEqual(self.context.calls,[])
                self.assertEqual(before,self.store.counts())
                self.store.put(record)

    def test_03_nondefault_zero_derived_writes(self):
        record=source_record(source_id="restricted",record_id="restricted_trace")
        self.store.put(record); before=self.store.counts()
        report=graph._project_event_memory_only(self.context,result={"record_id":record.record_id},eval_result={"ok":True},scope=SCOPE)
        self.assertEqual(report["error"],"unsupported_source_partition")
        self.assertEqual(before,self.store.counts()); self.assertEqual(self.context.calls,[])
        self.assertEqual(versioned_record_ref(self.store.get_by_exact_ref(record.record_id,scope=SCOPE,source_id="restricted")),versioned_record_ref(record))
        with self.store.lock:
            result=self.store.sqlite.search_policy("Synthetic bounded source marker",scope=SCOPE,source_ids=["default"])
        self.assertEqual(result["policy_suggestions"],[])

    def test_04_exact_scope_and_foreign_policy_ids(self):
        shared=ScopeRef(**{**asdict(SCOPE),"user_id":""})
        self.store.put(source_record(scope=shared,record_id="only_shared"))
        before=self.store.counts()
        report=graph._project_event_memory_only(self.context,result={"record_id":"only_shared"},eval_result={},scope=SCOPE)
        self.assertFalse(report["ok"]); self.assertEqual(before,self.store.counts())
        plan=graph._projection_plan(self.context,result={"record_id":"trace_source"},eval_result={"record_id":"trace_source","ok":True,"primary_label":"success"},memory_update={},scope=SCOPE, feedback_required=False)
        other=ScopeRef(**{**asdict(SCOPE),"user_id":"other"})
        with self.store.lock:
            self.store.sqlite.record_event(plan["event"],scope=other)
        before=self.store.counts(); report=self.project()
        self.assertEqual(report["error"],"policy_identity_conflict")
        self.assertEqual(report["policy_event_id"],"");self.assertEqual(before,self.store.counts())
        self.assertEqual(self.context.calls,[])
        # The same identity boundary also applies to the outcome table.
        with TemporaryDirectory(prefix="foreign-outcome-") as directory:
            store=SQLiteOwner(Path(directory)/"records.sqlite");context=Context(store);store.put(source_record())
            plan=graph._projection_plan(context,result={"record_id":"trace_source"},eval_result={"ok":True},memory_update={},scope=SCOPE, feedback_required=False)
            with store.lock:
                store.sqlite.record_outcome(plan["event"]["id"],plan["outcome"],scope=other,apply_rollbacks=False)
            before=store.counts()
            denied=graph._project_event_memory_only(context,result={"record_id":"trace_source"},eval_result={"ok":True},scope=SCOPE)
            self.assertEqual(denied["error"],"policy_identity_conflict")
            self.assertEqual(denied["event_outcome_id"],"");self.assertEqual(context.calls,[])
            self.assertEqual(before,store.counts());store.close()

    def test_05_status_changed_version_and_missing_capability(self):
        for status in ("rejected","quarantined","deprecated"):
            with self.subTest(status=status):
                changed=source_record(status=status);self.store.put(changed);before=self.store.counts()
                report=self.project();self.assertEqual(report["error"],"unsafe_source_status")
                self.assertEqual(before,self.store.counts())
        self.store.put(self.source)
        wrong_ref=versioned_record_ref(self.source);wrong_ref["version_digest"]="0"*64
        before=self.store.counts();report=self.project(result={"record_id":"trace_source","source_record_ref":wrong_ref})
        self.assertEqual(report["error"],"source_version_changed");self.assertEqual(before,self.store.counts())
        denied=graph._project_event_memory_only(self.context,result={"record_id":"other"},eval_result={"record_id":"trace_source"},scope=SCOPE)
        self.assertEqual(denied["error"],"conflicting_source_record_ids")
        self.context.record_outcome=None
        report=self.project();self.assertEqual(report["error"],"record_outcome_unavailable")
        self.assertEqual(self.context.calls,[]);self.assertEqual(before,self.store.counts())

    def test_06_writer_failures_and_no_foreign_id_adoption(self):
        for mode in ("event_failure","event_foreign_id","event_no_write"):
            with self.subTest(mode=mode):
                self.context.mode=mode;before=self.store.counts()
                report=self.project();self.assertFalse(report["ok"])
                self.assertEqual(report["policy_event_id"],"");self.assertEqual(before,self.store.counts())
        self.context.mode="event_failure_after_commit"
        report=self.project();self.assertFalse(report["ok"])
        self.assertEqual(report["status"],"partial")
        self.assertEqual(report["committed_stages"],["policy_event"])
        self.assertTrue(report["policy_event_id"])
        self.assertEqual(self.store.counts()["event_outcomes"],0)

    def test_07_stage_failures_and_parent_changes(self):
        for mode in ("outcome_failure","parent_after_event","parent_after_outcome","before","before_commit","after_commit"):
            with self.subTest(mode=mode):
                with TemporaryDirectory(prefix="event-stage-") as directory:
                    store=SQLiteOwner(Path(directory)/"records.sqlite");context=Context(store);store.put(source_record())
                    if mode in ("before","before_commit","after_commit"):
                        store.memory_fault=mode
                    else:
                        context.mode=mode
                    report=graph._project_event_memory_only(context,result={"record_id":"trace_source"},eval_result={"ok":True},scope=SCOPE)
                    self.assertFalse(report["ok"],report);self.assertEqual(report["status"],"partial",report)
                    self.assertIn("policy_event",report["committed_stages"])
                    if mode in ("parent_after_event","parent_after_outcome"):
                        self.assertEqual(report["error"],"source_version_changed",report)
                        count=store.counts();context.mode=""
                        retried=graph._project_event_memory_only(context,result={"record_id":"trace_source"},eval_result={"ok":True},scope=SCOPE)
                        self.assertFalse(retried["ok"]);self.assertEqual(count,store.counts())
                    if mode=="after_commit":
                        self.assertIn("event_memory",report["committed_stages"],report)
                        self.assertEqual(store.counts()["records"],2)
                    else:
                        self.assertEqual(store.counts()["records"],1)
                        self.assertEqual(store.counts()["memory_edges"],0)
                    store.close()

    def test_08_feedback_boundary_and_hook_guard_order(self):
        other=ScopeRef(**{**asdict(SCOPE),"user_id":"other"})
        cases=(source_record(scope=other,record_id="feedback"),source_record(source_id="restricted",record_id="feedback_source"),source_record(status="rejected",record_id="feedback_rejected"))
        for record in cases:
            with self.subTest(record=record.record_id):
                self.store.put(record);before=self.store.counts()
                report=self.project(memory_update={"record_id":record.record_id,"record_ref":versioned_record_ref(record)})
                self.assertFalse(report["ok"]);self.assertEqual(before,self.store.counts())
        feedback=source_record(record_id="valid_feedback")
        self.store.put(feedback)
        report=self.project(memory_update={"record_id":"valid_feedback","record_ref":versioned_record_ref(feedback)})
        self.assertTrue(report["ok"],report);self.assertEqual(report["edge_count"],4)
        # Static order check supplements leaf tests; the operational hook is not run.
        path=Path(__file__).resolve().parents[1]/"eimemory/governance/learning/closed_loop.py"
        tree=ast.parse(path.read_text())
        hook=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="post_experience_hook")
        calls={name:[] for name in ("_prepare_projection_source","_projection_plan","_ingest_feedback_memory","_safe_generate_learning","_safe_rl_update")}
        for node in ast.walk(hook):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id in calls:
                calls[node.func.id].append(node.lineno)
        self.assertLess(min(calls["_prepare_projection_source"]),min(calls["_ingest_feedback_memory"]))
        self.assertLess(min(calls["_projection_plan"]),min(calls["_ingest_feedback_memory"]))
        self.assertLess(min(calls["_ingest_feedback_memory"]),min(calls["_safe_generate_learning"]))

    def test_09_required_feedback_receipts_cannot_be_optional_payloads(self):
        feedback=source_record(record_id="feedback_receipt")
        self.store.put(feedback)
        invalid_receipts=[None, False, {}, {"ok":True}, {"record_id":""}, {"record_id":feedback.record_id},
            {"record_id":"", "feedback_required":False},
            {"record_id":feedback.record_id,"record_ref":{**versioned_record_ref(feedback),"version_digest":"0"*64}}]
        for receipt in invalid_receipts:
            with self.subTest(receipt=receipt):
                before=self.store.counts();self.context.calls.clear()
                report=graph.project_experience_event_memory(self.context,
                    result={"record_id":"trace_source","feedback_required":False,"feedback_mode":"event_only"},
                    eval_result={"ok":True},memory_update=receipt,scope=SCOPE)
                self.assertFalse(report["ok"],report);self.assertFalse(report["complete"])
                self.assertEqual(self.context.calls,[]);self.assertEqual(before,self.store.counts())
        receipt={"record_id":feedback.record_id,"record_ref":versioned_record_ref(feedback)}
        report=self.project(memory_update=receipt)
        self.assertTrue(report["ok"],report);self.assertTrue(report["feedback"]["verified"])
        self.assertEqual(report["feedback"]["record_ref"],receipt["record_ref"])
        self.assertEqual(report["completion_scope"],"event_projection_with_feedback")

    def test_10_isolated_original_entry_and_ingest_bodies_require_feedback(self):
        # Exact production function bodies, renamed entry; only ingest/learning/RL
        # boundaries are synthetic. No Runtime or operational hook is imported/run.
        path=Path(__file__).resolve().parents[1]/"eimemory/governance/learning/closed_loop.py"
        tree=ast.parse(path.read_text())
        selected={"post_experience_hook","evaluate_result","_ingest_feedback_memory",
                  "_safe_event_graph_projection","_stopped_experience_projection","_scope_dict",
                  "_mapping","_nested","_first_text","_string_list","_float","_json_safe"}
        nodes=[deepcopy(n) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in selected]
        for node in nodes:
            if node.name=="post_experience_hook":node.name="isolated_post_experience_body"
        module=ast.Module(body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0),*nodes],type_ignores=[])
        for mode in ("none","success_dict","not_persisted","stale_version","wrong_scope","wrong_source","source_changed","valid"):
            with self.subTest(mode=mode),TemporaryDirectory(prefix="feedback-entry-") as directory:
                store=SQLiteOwner(Path(directory)/"records.sqlite");context=Context(store);store.put(source_record())
                calls=[]
                def ingest(**kwargs):
                    calls.append("feedback")
                    if mode=="none":return None
                    if mode=="success_dict":return {"ok":True}
                    feedback=source_record(record_id="feedback_entry")
                    feedback.kind="memory";feedback.source="loop"
                    if mode=="wrong_scope":feedback.scope=ScopeRef(**{**asdict(SCOPE),"user_id":"foreign"})
                    if mode=="wrong_source":feedback.source_id="restricted"
                    if mode!="not_persisted":
                        stored=deepcopy(feedback)
                        if mode=="stale_version":stored.summary+=" newer version"
                        store.put(stored)
                    if mode=="source_changed":context.change_parent()
                    return feedback
                context.memory=SimpleNamespace(ingest=ingest)
                def project(*args,**kwargs):
                    calls.append("projection")
                    return graph.project_experience_event_memory(*args,**kwargs)
                def learn(*args,**kwargs):calls.append("learning");return {"ok":True}
                def rl(*args,**kwargs):calls.append("rl");return {"ok":True}
                ns={"ScopeRef":ScopeRef,"RecordEnvelope":RecordEnvelope,"asdict":asdict,"is_dataclass":is_dataclass,
                    "json":json,"SUCCESS_LABELS":{"success","good","passed","pass","ok"},
                    "versioned_record_ref":versioned_record_ref,"project_experience_event_memory":project,
                    "_safe_generate_learning":learn,"_safe_rl_update":rl}
                for name in ("_prepare_projection_source","_projection_plan","_projection_failure",
                             "_revalidate_projection_source","_validated_feedback_ref","_ProjectionBlocked"):
                    ns[name]=getattr(graph,name)
                exec(compile(ast.fix_missing_locations(module),str(path),"exec"),ns)
                if mode=="none":
                    # Preserve the exact previously observed None -> empty-ID shape
                    # for existing callers not requesting a proof receipt.
                    legacy=ns["_ingest_feedback_memory"](context,scope=SCOPE,title="fixture",memory_type="reflection",source="loop",evaluation={})
                    self.assertEqual(legacy,{"record_id":""});calls.clear()
                report=ns["isolated_post_experience_body"](context,{"record_id":"trace_source","ok":True},SCOPE)
                if mode=="valid":
                    self.assertTrue(report["ok"],report);self.assertTrue(report["complete"])
                    self.assertEqual(calls,["feedback","projection","learning","rl"])
                    self.assertEqual(store.counts()["records"],3)
                else:
                    self.assertFalse(report["ok"],report);self.assertFalse(report["complete"])
                    self.assertEqual(report["status"],"partial",report)
                    self.assertNotIn("learning",calls);self.assertNotIn("rl",calls)
                    self.assertEqual(store.counts()["events"],0);self.assertEqual(store.counts()["event_outcomes"],0)
                    self.assertEqual(store.counts()["memory_edges"],0)
                    if mode!="source_changed":
                        self.assertEqual(calls,["feedback"])
                        self.assertEqual(report["uncertain_stages"],["memory"])
                        self.assertEqual(report["memory"]["record_id"],"")
                ISOLATED_ENTRY_OBSERVATIONS[mode] = {
                    "ok":report["ok"], "complete":report["complete"], "status":report["status"],
                    "error":report.get("error", ""), "calls":list(calls), "counts":store.counts(),
                    "uncertain_stages":report.get("uncertain_stages", []),
                }
                store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
