import json
import pytest
from eimemory.knowledge.l1_queue import L1ExtractQueue
from eimemory.cli.l1_worker import drain_l1


@pytest.mark.parametrize("raw", [b"{", b"[]", b'{"jobs": false}', b'{"dead": [1]}', b"\xff"])
def test_corrupt_queue_is_never_replaced_with_empty_state(tmp_path, raw):
    path = tmp_path / "queue.json"
    path.write_bytes(raw)
    queue = L1ExtractQueue(path)
    with pytest.raises(RuntimeError):
        queue.enqueue({"episode_id": "fixture"})
    assert path.read_bytes() == raw


def test_queue_failure_after_handler_is_not_reported_as_empty_success(tmp_path):
    path = tmp_path / "queue.json"
    queue = L1ExtractQueue(path)
    queue.enqueue({"episode_id": "fixture"})
    def handler(job):
        path.write_text("{")
    with pytest.raises(RuntimeError) as captured:
        queue.drain_report(handler)
    assert captured.value.context["handler_completed"] is True
    assert captured.value.context["completion_recorded"] is False
    assert path.read_text() == "{"


def test_worker_reports_unavailable_state_without_overwriting_queue(tmp_path):
    root = tmp_path / "runtime"
    path = root / "state" / "l1_extract_queue.json"
    path.parent.mkdir(parents=True)
    path.write_text("{")
    result = drain_l1(root=str(root))
    assert result["ok"] is False and result["queue_state"] == "unavailable"
    assert result["pending"] is None and result["dead"] is None
    assert path.read_text() == "{"
