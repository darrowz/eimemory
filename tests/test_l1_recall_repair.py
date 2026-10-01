from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.memory import MemoryAPI
from eimemory.api.runtime import Runtime
from eimemory.knowledge.l1_pipeline import (
    L1_EXTRACT_VERSION,
    backfill_l1_from_l0,
    persist_l1_atoms,
)
from eimemory.knowledge.sediment import L1Atom, L1ExtractorUnavailable, extract_l1_atoms
from eimemory.llm.command_client import LLMResult
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.raw.retrieval import rerank_raw_results
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "synthetic-owner",
}


class FactLLM:
    def __init__(self, text: str):
        self.text = text

    def complete(self, **kwargs):
        del kwargs
        return LLMResult(
            text=self.text,
            provider_id="test",
            model_id="test",
        )


def test_l1_llm_can_extract_reusable_fact():
    atoms = extract_l1_atoms(
        user_text="团队事项分工：系统类故障交甲；项目相关人员信息找乙；项目信息查询找丙。",
        source_message_ids=["ep-1"],
        use_llm=True,
        fallback_heuristic=False,
        llm=FactLLM(
            '[{"scene_name":"division","message_ids":["ep-1"],"memories":['
            '{"content":"团队事项分工：系统类故障交甲；项目相关人员信息找乙；项目信息查询找丙。",'
            '"type":"fact","priority":90,"source_message_ids":["ep-1"]}]}]'
        ),
    )
    assert len(atoms) == 1
    assert atoms[0].memory_type == "fact"


def test_missing_production_llm_is_not_a_successful_empty_extract(monkeypatch):
    monkeypatch.setattr(
        "eimemory.llm.hermes_adapter.resolve_l1_llm_client",
        lambda: None,
    )
    with pytest.raises(L1ExtractorUnavailable):
        extract_l1_atoms(
            user_text="团队事项分工：系统类故障交甲。",
            source_message_ids=["ep-2"],
            use_llm=True,
            fallback_heuristic=False,
        )


def test_queue_drain_marks_l0_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("EIMEMORY_L1_FORCE_QUEUE", "1")
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    try:
        turn = service.sync_turn(
            channel="hermes",
            scope=SCOPE,
            session_id="queue-complete",
            turn_id="1",
            user_text="以后回答先给结论，少解释。",
            assistant_text="收到。",
        )
        assert service.drain_l1_queue(limit=1) == 1
        parent = runtime.store.get_by_exact_ref(
            turn["record"]["record_id"],
            scope=ScopeRef.from_dict(turn["record"]["scope"]),
            source_id="hermes",
        )
        meta = business_metadata(parent.meta)
        assert meta["l1_extract_version"] == L1_EXTRACT_VERSION
        assert meta["l1_extraction_status"] in {"stored", "no_change", "no_memory"}
        assert "l1_atom_count" in meta
    finally:
        runtime.close()


def test_conflict_update_retires_old_fact_atomically(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        old = api.ingest(
            text="鸿欣账号属于小马哥。",
            memory_type="fact",
            title="鸿欣账号归属",
            scope=SCOPE,
            source="hermes.l1",
            source_id="hermes",
            force_capture=True,
            meta={"memory_layer": "l1", "runtime_channel": "hermes"},
        )

        class Judge:
            def complete(self, **kwargs):
                del kwargs
                return LLMResult(
                    text=(
                        '[{"record_id":"new-0","action":"update",'
                        f'"target_ids":["{old.record_id}"],'
                        '"merged_content":"更正：鸿欣账号属于钊哥，不属于小马哥。",'
                        '"merged_type":"fact"}]'
                    ),
                    provider_id="test",
                    model_id="test",
                )

        atom = L1Atom(
            text="更正：鸿欣账号属于钊哥，不属于小马哥。",
            title="鸿欣账号归属更正",
            memory_type="fact",
            semantic_key="fact:hongxin-owner-corrected",
            category="fact",
            source_message_ids=(),
        )
        written = persist_l1_atoms(
            api,
            atoms=[atom],
            episode_id="",
            scope=SCOPE,
            channel_id="hermes",
            llm=Judge(),
            strict_conflict=True,
        )
        assert len(written) == 1
        refreshed_old = store.get_by_exact_ref(
            old.record_id, scope=ScopeRef.from_dict(SCOPE), source_id="hermes"
        )
        assert refreshed_old.status == "superseded"
        assert business_metadata(refreshed_old.meta)["superseded_by"] == written[0]["record_id"]


def test_conflict_judge_cannot_supersede_unrelated_record(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        api.ingest(
            text="鸿欣账号属于小马哥。",
            memory_type="fact",
            title="鸿欣账号归属",
            scope=SCOPE,
            source="hermes.l1",
            source_id="hermes",
            force_capture=True,
            meta={"memory_layer": "l1", "runtime_channel": "hermes"},
        )

        class BadJudge:
            def complete(self, **kwargs):
                del kwargs
                return LLMResult(
                    text='[{"record_id":"new-0","action":"update","target_ids":["mem_not_in_pool"]}]',
                    provider_id="test",
                    model_id="test",
                )

        atom = L1Atom(
            text="更正：鸿欣账号属于钊哥。",
            title="鸿欣账号归属更正",
            memory_type="fact",
            semantic_key="fact:hongxin-owner-corrected",
            category="fact",
            source_message_ids=(),
        )
        with pytest.raises(Exception, match="unauthorized_target"):
            persist_l1_atoms(
                api,
                atoms=[atom],
                episode_id="",
                scope=SCOPE,
                channel_id="hermes",
                llm=BadJudge(),
                strict_conflict=True,
            )


def test_raw_chinese_correction_outranks_stale_old_statement():
    scope = ScopeRef.from_dict(SCOPE)
    old = RecordEnvelope.create(
        kind="memory",
        title="旧判断",
        summary="鸿欣账号属于小马哥。",
        content={"text": "鸿欣账号属于小马哥。", "memory_type": "conversation"},
        scope=scope,
        source="hermes.turn",
        source_id="hermes",
        meta={"memory_type": "conversation"},
    )
    new = RecordEnvelope.create(
        kind="memory",
        title="纠错",
        summary="更正：刚才串档了，鸿欣账号属于钊哥，不属于小马哥。",
        content={"text": "更正：刚才串档了，鸿欣账号属于钊哥，不属于小马哥。", "memory_type": "conversation"},
        scope=scope,
        source="hermes.turn",
        source_id="hermes",
        meta={"memory_type": "conversation"},
    )
    old.time.occurred_at = "2026-09-01T00:00:00Z"
    new.time.occurred_at = "2026-09-02T00:00:00Z"
    ranked = rerank_raw_results(
        query="鸿欣账号归属",
        results=[
            {"record": old, "base_score": 1.0},
            {"record": new, "base_score": 0.10},
        ],
    )
    assert ranked[0]["record"]["record_id"] == new.record_id
    assert ranked[0]["boosts"]["correction_marker"] == pytest.approx(0.95)


def test_backfill_can_retry_legacy_zero_atom_completion(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        l0 = api.ingest(
            text="User: 团队事项分工：系统类故障交甲；项目信息查询找丙。\nAssistant: 收到。",
            memory_type="conversation",
            title="Hermes completed turn",
            scope=SCOPE,
            source="hermes.turn",
            source_id="hermes",
            force_capture=True,
            meta={
                "memory_layer": "l0",
                "capture_origin": "turn_sync",
                "session_id": "legacy",
                "turn_id": "1",
            },
        )
        l0.meta = {
            **dict(l0.meta),
            "l1_extracted_at": "1",
            "l1_atom_count": 0,
        }
        with store._lock:
            store.sqlite.upsert(l0)
        llm = FactLLM(
            '[{"scene_name":"division","message_ids":["x"],"memories":['
            '{"content":"团队事项分工：系统类故障交甲；项目信息查询找丙。",'
            '"type":"fact","priority":90,"source_message_ids":[]}]}]'
        )
        report = backfill_l1_from_l0(
            api,
            scope=SCOPE,
            limit=10,
            use_llm=True,
            llm=llm,
            retry_legacy=True,
        )
        assert report["legacy_retried"] == 1
        assert report["extracted"] == 1
        refreshed = store.get_by_exact_ref(
            l0.record_id, scope=l0.scope, source_id=l0.source_id
        )
        assert business_metadata(refreshed.meta)["l1_extract_version"] == L1_EXTRACT_VERSION
