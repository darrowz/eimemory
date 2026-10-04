from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from eimemory.governance.learning.learning_state import complete_learning_loop, mark_step
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


def test_native_stale_progress_and_terminal_are_exact(tmp_path):
    store = RuntimeStore(tmp_path)
    try:
        runtime = SimpleNamespace(store=store)
        personal = RecordEnvelope.create(kind="learning_loop", title="local loop", status="running", scope=ScopeRef(user_id="person"), content={"steps": []})
        shared = RecordEnvelope.create(kind="learning_loop", title="local loop", status="running", scope=ScopeRef(), content={"steps": []})
        personal.record_id = shared.record_id = "loop_exact"
        store.append(personal)
        store.append(shared)
        mark_step(runtime, personal, step_name="first", status="completed")
        mark_step(runtime, personal, step_name="second", status="completed")
        get = lambda: store.get_by_exact_ref(personal.record_id, scope=personal.scope, source_id=personal.source_id)
        assert get().status == "running"
        assert [x["step_name"] for x in get().content["steps"]] == ["first", "second"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda _: complete_learning_loop(runtime, personal), range(2)))
        assert rows[0].to_dict() == rows[1].to_dict()
        saved = get().to_dict()
        mark_step(runtime, personal, step_name="late", status="running")
        assert get().to_dict() == saved
        with pytest.raises(ValueError, match="already finalized"):
            complete_learning_loop(runtime, personal, status="failed")
        assert store.get_by_exact_ref(shared.record_id, scope=shared.scope, source_id=shared.source_id).status == "running"
    finally:
        store.close()
