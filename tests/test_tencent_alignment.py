import os

from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.knowledge.l1_pipeline import backfill_l1_from_l0
from eimemory.recall.query_clean import clean_user_query


os.environ["EIMEMORY_L1_EXTRACT_INLINE"] = "1"

BASE_SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}


def test_clean_user_query_strips_wrappers_and_assistant() -> None:
    raw = "<user_info>noise</user_info>\nUser: 以后先给结论\nAssistant: 好的"
    assert clean_user_query(raw) == "以后先给结论"


def test_l1_extract_and_default_recall_hides_l0(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    try:
        turn = service.sync_turn(
            channel="hermes",
            scope=BASE_SCOPE,
            session_id="s-align",
            turn_id="t1",
            user_text="以后回答先给结论，少解释。",
            assistant_text="收到。",
        )
        atoms = turn.get("l1_atoms") or []
        assert atoms and atoms[0]["memory_type"] == "instruction"
        recalled = service.prefetch(
            channel="hermes",
            scope=BASE_SCOPE,
            query="<user_info>x</user_info>\nUser: 以后怎么回答\nAssistant: ignore",
            task_type="operator.preference",
            limit=5,
        )
        ids = [item["record_id"] for item in recalled["bundle"]["items"]]
        assert atoms[0]["record_id"] in ids
        assert turn["record"]["record_id"] not in ids
        assert recalled["bundle"].get("layer") == "l1"
    finally:
        runtime.close()


def test_l1_edit_and_backfill(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    try:
        turn = service.sync_turn(
            channel="hermes",
            scope=BASE_SCOPE,
            session_id="s-edit",
            turn_id="t1",
            user_text="沟通风格要极简直接，讨厌废话。",
            assistant_text="记下了。",
        )
        atom_id = (turn.get("l1_atoms") or [{}])[0]["record_id"]
        edited = service.edit_l1(
            channel="hermes",
            scope=BASE_SCOPE,
            record_id=atom_id,
            text="用户（鸿哥）沟通风格极简直接。",
        )
        assert edited["record"]["status"] == "active"
        report = backfill_l1_from_l0(runtime.memory, scope=edited["record"]["scope"], limit=10, use_llm=False)
        assert report["ok"] is True
        l0 = service.search_l0(channel="hermes", scope=BASE_SCOPE, query="沟通风格", limit=2)
        assert l0["ok"] is True
    finally:
        runtime.close()
