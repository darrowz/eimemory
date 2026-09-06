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
        report = evaluate_plane(service, scope=BASE_SCOPE)
        assert report["ok"] is True
        assert report["failed"] == 0
    finally:
        runtime.close()
