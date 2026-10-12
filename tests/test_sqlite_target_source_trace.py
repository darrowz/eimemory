"""Actual SQLite source windows plus private reader/namespace isolation."""
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.controlled_memory_benchmark import build_controlled_memory_benchmark, run_controlled_memory_benchmark
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.contracts import CandidateRef, CandidateRequest, ExactScope
from eimemory.retrieval.target_trace import (
    MAX_INSPECTED_REFS, SOURCE_SCHEMA, SOURCE_STAGES, current_source_trace, current_sqlite_trace,
    native_target_trace_scope, sqlite_hybrid_trace_scope, sqlite_reader_trace_scope,
    sqlite_source_trace_scope, target_trace_scope,
)


SCOPE = ScopeRef("synthetic", "source-audit", "isolated", "owner")


@pytest.fixture(autouse=True)
def no_remote_model(monkeypatch):
    monkeypatch.setenv("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "0")
    def prohibited(*args, **kwargs):
        raise AssertionError("Remote provider prohibited")
    monkeypatch.setattr("eimemory.llm.command_client.CommandLLMClient.complete", prohibited)


def memory(runtime, rid, title="Orchid green archive", *, meta=None, variant=None):
    record = RecordEnvelope.create(kind="memory", title=title, summary=title,
        content={"text": title, "memory_type": "fact"}, scope=SCOPE,
        source="operator.memory", source_id="hermes",
        meta={"memory_type": "fact", "force_capture": True, **(meta or {})})
    record.record_id = rid
    if variant is not None:
        record.content["fixture_variant"] = variant
    return runtime.store.append(record)


def dataset(runtime):
    return build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])


def run(runtime, data):
    return run_controlled_memory_benchmark(runtime, data, scope=SCOPE, source_ids=["hermes"])


def rows(report):
    return {row["stage"]: row for row in report["stages"]}


def request(target):
    return CandidateRequest.create(query=target.title, scope=target.scope, source_ids=[target.source_id],
        target_source_id=target.source_id, limit=5, task_context={"exact_scope_only": True})


def test_real_sqlite_source_exposes_actual_identity_collection_scoring_and_binding(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "mem_native_target")
        data = dataset(runtime)
        before = deepcopy(data)
        sample = run(runtime, data)["samples"][0]
        assert sample["passed"] is True and data == before
        source = sample["retrieval_diagnostics"]["target_trace"]["source_trace"]
        assert source["schema"] == SOURCE_SCHEMA and source["status"] == "completed"
        assert len(source["stages"]) == len(SOURCE_STAGES) == 10
        for stage in SOURCE_STAGES:
            assert rows(source)[stage]["target_presence"] == "present"
        assert rows(source)["score_ref_binding"]["binding_status"] == "bound"
        assert rows(source)["scored"]["source_score"] is not None
        assert rows(source)["scored"]["source_rank"] == 1
        serialized = json.dumps(source)
        assert all(value not in serialized for value in (target.record_id, target.title, SCOPE.user_id, "storage_key", "query_digest"))


def test_real_sqlite_bounded_miss_does_not_imply_target_index_absence(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "mem_zz_native_target", "Shared Orchid project archive")
        data = dataset(runtime)
        for index in range(48):
            memory(runtime, f"mem_aa_native_{index:03}", target.title, variant=index)
        sample = run(runtime, data)["samples"][0]
        assert sample["failure_reason"] == "known_item_missing"
        source = sample["retrieval_diagnostics"]["target_trace"]["source_trace"]
        assert source["status"] == "completed"
        assert rows(source)["identity_index_rows"]["target_presence"] == "absent_in_observed_window"
        assert rows(source)["hybrid_collected"]["target_presence"] == "present"
        assert rows(source)["score_ref_binding"]["binding_status"] == "bound"
        assert rows(source)["merged_hits"]["target_presence"] == "present"
        assert rows(source)["returned_hits"]["target_presence"] == "absent_in_observed_window"
        assert rows(source)["returned_hits"]["drop_reasons"] == ["source_return_limit"]
        assert runtime.store.get_by_exact_ref(target.record_id, scope=SCOPE, source_id="hermes") is not None


def test_trace_on_off_executes_identical_sql_and_selects_identical_records(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "mem_native_target")
        for index in range(3):
            memory(runtime, f"mem_native_other_{index}", f"Orchid archive section {index}")
        req, engine = request(target), runtime.memory.recall_engine
        engine.recall(req)
        engine.recall(req)
        sql = []
        connections = [runtime.store.sqlite.conn, *(slot.store.conn for slot in runtime.store._readers)]
        for connection in connections:
            connection.set_trace_callback(sql.append)
        plain = engine.recall(req)
        plain_sql = list(sql)
        sql.clear()
        ref = CandidateRef(target.record_id, ExactScope.from_scope(SCOPE), "hermes")
        with target_trace_scope(engine, ref, query=req.query, source_ids=req.source_ids) as trace:
            observed = engine.recall(req)
        for connection in connections:
            connection.set_trace_callback(None)
        # Remaining-deadline PRAGMAs legitimately differ with elapsed time;
        # all SQL reads, their arguments, order and counts must match exactly.
        assert [statement for statement in sql if statement.lstrip().upper().startswith(("SELECT", "WITH"))] == [
            statement for statement in plain_sql if statement.lstrip().upper().startswith(("SELECT", "WITH"))]
        assert [record.to_dict() for record in plain.items] == [record.to_dict() for record in observed.items]
        assert plain.explanation["relevance_selector"] == observed.explanation["relevance_selector"]
        assert trace.report()["source_trace"]["status"] == "completed"


def bound_fixture():
    primary, reader = object(), object()
    store = SimpleNamespace(sqlite=primary)
    source = SimpleNamespace(store=store)
    engine = SimpleNamespace(store=store, candidate_source=source)
    ref = CandidateRef("mem_native_target", ExactScope.from_scope(SCOPE), "hermes")
    req = CandidateRequest.create(query="Orchid archive", scope=SCOPE, source_ids=["hermes"],
        target_source_id="hermes", kinds=["memory"], task_context={"exact_scope_only": True})
    return store, source, engine, ref, req, primary, reader


def test_reader_origin_token_and_nested_engine_cannot_borrow_parent_trace():
    store, source, engine, ref, req, primary, reader = bound_fixture()
    with target_trace_scope(engine, ref, query=req.query, source_ids=req.source_ids):
        with native_target_trace_scope(engine, req), sqlite_source_trace_scope(source, req):
            outer = current_source_trace(source)
            assert current_sqlite_trace(primary) is outer
            assert current_sqlite_trace(primary, query="different", scope=SCOPE,
                source_ids=req.source_ids, kinds=req.kinds, verify_request=True) is None
            with sqlite_reader_trace_scope(store, reader, query=req.query, scope=SCOPE,
                    source_ids=req.source_ids, kinds=req.kinds, limit=5):
                assert current_sqlite_trace(reader) is None  # no original-hybrid token
            with sqlite_hybrid_trace_scope(source, req, limit=5):
                with native_target_trace_scope(engine, req):
                    assert current_source_trace(source) is current_sqlite_trace(primary) is None
                    with sqlite_reader_trace_scope(store, reader, query=req.query, scope=SCOPE,
                            source_ids=req.source_ids, kinds=req.kinds, limit=5):
                        assert current_sqlite_trace(reader) is None
                with sqlite_reader_trace_scope(store, reader, query=req.query, scope=SCOPE,
                        source_ids=req.source_ids, kinds=req.kinds, limit=5):
                    assert current_sqlite_trace(reader) is outer
                    assert current_sqlite_trace(primary) is None
                assert current_sqlite_trace(primary) is outer


@pytest.mark.parametrize("bad", ["missing_scope", "source_separator", "source_other", "scope_other", "key_only"])
def test_source_namespace_and_key_only_windows_never_manufacture_target_ref(bad):
    store, source, engine, ref, req, _, _ = bound_fixture()
    row = {"storage_key": "opaque-native-key", "record_id": ref.record_id, "source_id": ref.source_id,
           **asdict(SCOPE)}
    if bad == "missing_scope":
        row.pop("user_id")
    elif bad == "source_separator":
        row["source_id"] = "hermes\x1fprivate"
    elif bad == "source_other":
        row["source_id"] = "private"
    elif bad == "scope_other":
        row["user_id"] = "different-owner"
    else:
        row = {"storage_key": "opaque-native-key"}
    with target_trace_scope(engine, ref, query=req.query, source_ids=req.source_ids):
        with native_target_trace_scope(engine, req), sqlite_source_trace_scope(source, req):
            active = current_source_trace(source)
            active.collect("fts", [row])
            active.observe_keys("hybrid_collected", ["opaque-native-key"])
            report = active.report()
    presence = rows(report)["hybrid_collected"]["target_presence"]
    assert presence == ("absent_in_observed_window" if bad in {"source_other", "scope_other"} else "unknown")
    assert "private" not in json.dumps(report)


def test_source_inspection_cap_and_unknown_arm_coverage_remain_sticky():
    store, source, engine, ref, req, _, _ = bound_fixture()
    row = {"storage_key": "actual-target-key", "record_id": ref.record_id,
           "source_id": ref.source_id, **asdict(SCOPE)}
    with target_trace_scope(engine, ref, query=req.query, source_ids=req.source_ids):
        with native_target_trace_scope(engine, req), sqlite_source_trace_scope(source, req):
            active = current_source_trace(source)
            active.collect("fts", [{"storage_key": "unknown"}])
            active.collect("fts", [row])
            active.observe_keys("hybrid_capped", ["other"] * MAX_INSPECTED_REFS + ["actual-target-key"])
            report = active.report()
    assert report["collection_arms"]["fts"] == "incomplete"
    assert rows(report)["hybrid_capped"]["target_presence"] == "unknown"
    assert rows(report)["hybrid_capped"]["inspected_count"] == MAX_INSPECTED_REFS
