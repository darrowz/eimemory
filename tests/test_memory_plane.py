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
