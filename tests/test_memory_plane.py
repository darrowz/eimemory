import os

from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.cli.l1_worker import evaluate_plane


os.environ["EIMEMORY_L1_EXTRACT_INLINE"] = "1"

BASE_SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}


def test_memory_plane_eval_passes_on_standing_atoms(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    try:
        service.sync_turn(
            channel="hermes",
            scope=BASE_SCOPE,
            session_id="s-plane",
            turn_id="t1",
            user_text="以后回答先给结论，少解释。",
            assistant_text="收到。",
        )
        service.sync_turn(
            channel="hermes",
            scope=BASE_SCOPE,
            session_id="s-plane",
            turn_id="t2",
            user_text="沟通风格要极简直接，讨厌废话。",
            assistant_text="记下了。",
        )
        service.sync_turn(
            channel="hermes",
            scope=BASE_SCOPE,
            session_id="s-plane",
            turn_id="t3",
            user_text="不要再把鸿哥和钊哥两个号的记忆混在一起。",
            assistant_text="记下了。",
        )
        report = evaluate_plane(service, scope=BASE_SCOPE)
        assert report["ok"] is True, report
        assert report["failed"] == 0
        assert {case["id"] for case in report["cases"]} >= {
            "style_paraphrase_brief",
            "isolation_paraphrase",
        }
    finally:
        runtime.close()


def test_l1_queue_records_failure_and_dead_letter(tmp_path) -> None:
    from eimemory.knowledge.l1_queue import L1ExtractQueue

    queue = L1ExtractQueue(tmp_path / "l1.json")
    queue.enqueue({"episode_id": "ep-fail", "user_text": "x"})

    def _boom(job: dict) -> None:
        del job
        raise RuntimeError("extract_failed")

    first = queue.drain_report(_boom, limit=1)
    assert first["failed"] == 1
    assert first["newly_dead"] == 0
    assert first["pending"] == 1
    for _ in range(4):
        queue.drain_report(_boom, limit=1)
    assert queue.dead_count() == 1
    assert queue.recent_dead()[0]["last_error"] == "extract_failed"
    assert queue.pending_count() == 0
