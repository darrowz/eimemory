"""Selected dead-job recovery uses synthetic queue state only."""
from __future__ import annotations

from copy import deepcopy
import json
import multiprocessing
import os
from unittest.mock import patch

import pytest

from eimemory.knowledge.l1_queue import L1ExtractQueue, L1QueueStateError
from eimemory.storage.atomic_file import atomic_write_json, interprocess_lock


INTENT = "recover-selected-dead-l1"


def dead(job_id="dead-1", episode_id="episode-1", **changes):
    return dict(job_id=job_id, episode_id=episode_id, status="dead", attempts=5,
                channel_id="hermes", channel_scope=dict(tenant_id="synthetic", agent_id="agent",
                    workspace_id="workspace", user_id="operator"),
                created_at="2026-01-01T00:00:00+00:00", session_id="synthetic-session",
                turn_id="turn-1", user_text="SYNTHETIC_PRIVATE_USER", assistant_text="SYNTHETIC_PRIVATE_ASSISTANT",
                turn_text="SYNTHETIC_PRIVATE_TURN", last_error="SYNTHETIC_PRIVATE_EXCEPTION", **changes)


@pytest.fixture
def queue(tmp_path):
    q = L1ExtractQueue(tmp_path / "state" / "l1_extract_queue.json")
    q._save(dict(jobs=[], dead=[dead()]))
    return q


def recover(q, ids=("dead-1",), **kw):
    return q.recover_dead(list(ids), operator_intent=INTENT, **kw)


def join_child(child, expected=0):
    child.join(5)
    if child.is_alive():
        child.terminate()
        child.join(2)
        pytest.fail("synthetic_child_timeout")
    assert child.exitcode == expected


def test_inspection_and_dry_run_are_metadata_only_and_do_not_publish(queue):
    before = queue.path.read_bytes()
    report = queue.inspect_dead()
    assert report["jobs"][0]["job_id"] == "dead-1"
    assert report["jobs"][0]["attempts"] == 5
    dry = recover(queue, dry_run=True)
    assert dry["published"] == 0
    assert dry["eligible"] == 1
    assert queue.path.read_bytes() == before
    for obj in (report, dry):
        text = json.dumps(obj)
        assert "SYNTHETIC_PRIVATE" not in text
        assert "last_error" not in text
        assert "user_text" not in text
        assert "assistant_text" not in text


def test_recovery_preserves_original_and_source_order_with_separate_budget(queue):
    original = deepcopy(queue._load()["dead"])
    report = recover(queue)
    assert report["published"] == 1
    state = queue._load()
    assert state["dead"] == original
    child = state["jobs"][0]
    for key in ("episode_id", "channel_id", "channel_scope", "created_at", "session_id", "turn_id",
                "user_text", "assistant_text", "turn_text"):
        assert child[key] == original[0][key]
    assert child["job_id"] != "dead-1"
    assert child["attempts"] == 0
    assert child["status"] == "queued"
    assert child["recovery_of"] == "dead-1"
    assert state["recoveries"]["dead-1"]["original_attempts"] == 5


@pytest.mark.parametrize("ids,code", [([], "recovery_selection_invalid"), (["../x"], "recovery_selection_invalid"),
    (["x\nsecret"], "recovery_selection_invalid"), ([3], "recovery_selection_invalid"),
    (["dead-1", "dead-1"], "recovery_selection_invalid"), (["dead-1", "unknown"], "recovery_job_unknown"),
    (["x"] * 33, "recovery_selection_invalid")])
def test_invalid_selections_are_atomic(queue, ids, code):
    before = queue.path.read_bytes()
    with pytest.raises(L1QueueStateError, match=code):
        recover(queue, ids)
    assert queue.path.read_bytes() == before


def test_explicit_intent_required(queue):
    before = queue.path.read_bytes()
    with pytest.raises(L1QueueStateError, match="recovery_intent_required"):
        queue.recover_dead(["dead-1"], operator_intent="")
    assert queue.path.read_bytes() == before


def test_selected_order_uses_original_dead_order_not_operator_argument_order(queue):
    queue._save(dict(jobs=[], dead=[dead(), dead("dead-2", "episode-2")]))
    recover(queue, ["dead-2", "dead-1"])
    assert [j["episode_id"] for j in queue._load()["jobs"]] == ["episode-1", "episode-2"]


def test_same_parent_duplicate_dead_ids_fail_closed_but_cross_partition_is_valid(queue):
    queue._save(dict(jobs=[], dead=[dead(), dead("dead-2")]))
    before = queue.path.read_bytes()
    with pytest.raises(L1QueueStateError, match="recovery_parent_conflict"):
        recover(queue, ["dead-1", "dead-2"])
    assert queue.path.read_bytes() == before
    other = dead("dead-2")
    other["channel_scope"]["user_id"] = "another-operator"
    queue._save(dict(jobs=[], dead=[dead(), other]))
    assert recover(queue, ["dead-1", "dead-2"])["published"] == 2


def test_duplicate_dead_job_ids_and_pending_parent_are_rejected(queue):
    queue._save(dict(jobs=[], dead=[dead(), dead()]))
    with pytest.raises(L1QueueStateError, match="recovery_state_invalid"):
        recover(queue)
    active = dead("active")
    active.update(status="queued", attempts=0)
    queue._save(dict(jobs=[active], dead=[dead()]))
    with pytest.raises(L1QueueStateError, match="recovery_parent_conflict"):
        recover(queue)


def test_repeated_recovery_and_enqueue_after_completion_do_not_duplicate_processing(queue):
    first = recover(queue)
    assert recover(queue)["already_recovered"] == 1
    active = queue.enqueue({"episode_id": "episode-1", "channel_id": "hermes",
                            "channel_scope": dead()["channel_scope"]})
    assert active["job_id"] == first["jobs"][0]["recovery_job_id"]
    seen = []
    assert queue.drain_report(lambda job: seen.append(job["job_id"]), limit=9)["processed"] == 1
    assert recover(queue)["already_recovered"] == 1
    completed = queue.enqueue({"episode_id": "episode-1", "channel_id": "hermes",
                              "channel_scope": dead()["channel_scope"]})
    assert completed["status"] == "completed"
    assert queue.drain_report(lambda job: seen.append(job["job_id"]), limit=9)["processed"] == 0
    assert seen == [active["job_id"]]


@pytest.mark.parametrize("after_publish", [False, True])
def test_publication_faults_are_fail_closed_and_retry_is_idempotent(queue, after_publish):
    before = queue.path.read_bytes()
    def broken(path, payload):
        if after_publish:
            atomic_write_json(path, payload)
        raise OSError("SYNTHETIC_PRIVATE_FILESYSTEM_FAILURE")
    with patch("eimemory.knowledge.l1_queue.atomic_write_json", broken):
        with pytest.raises(L1QueueStateError, match="queue_unwritable"):
            recover(queue)
    if not after_publish:
        assert queue.path.read_bytes() == before
    result = recover(queue)
    assert result["published"] == (0 if after_publish else 1)
    assert len(queue._load()["jobs"]) == 1


def test_interrupted_publication_retries_do_not_duplicate(queue):
    ctx = multiprocessing.get_context("fork")
    for after in (False, True):
        queue._save(dict(jobs=[], dead=[dead()]))
        def crash():
            def interrupted(path, payload):
                if after:
                    atomic_write_json(path, payload)
                os._exit(23)
            with patch("eimemory.knowledge.l1_queue.atomic_write_json", interrupted):
                recover(queue)
        child = ctx.Process(target=crash)
        child.start()
        join_child(child, expected=23)
        assert recover(queue)["published"] == (0 if after else 1)
        assert len(queue._load()["jobs"]) == 1


def test_concurrent_processes_publish_only_once(queue):
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    results = ctx.Queue()
    def worker():
        start.wait(5)
        results.put(recover(L1ExtractQueue(queue.path))["published"])
    children = [ctx.Process(target=worker) for _ in range(2)]
    for child in children:
        child.start()
    start.set()
    for child in children:
        join_child(child)
    assert sorted(results.get(timeout=2) for _ in children) == [0, 1]
    assert len(queue._load()["jobs"]) == 1


def test_recovery_honors_consumer_lock(queue):
    with interprocess_lock(queue.consumer_lock_path):
        with pytest.raises(TimeoutError):
            recover(queue, lock_timeout=0)
    assert queue.pending_count() == 0


def test_recovered_history_is_retained_beyond_normal_dead_cap(queue):
    original = deepcopy(queue._load()["dead"])
    recover(queue)
    state = queue._load()
    state["dead"].extend(dead(f"extra-{i}", f"episode-{i + 50}") for i in range(200))
    queue._save(state)
    def fail(_):
        raise ValueError("synthetic-failure")
    for _ in range(5):
        queue.drain_report(fail, limit=1)
    state = queue._load()
    assert state["dead"][0] == original[0]
    assert recover(queue)["already_recovered"] == 1
    assert len([j for j in state["dead"] if j.get("recovery_of") == "dead-1"]) == 1


def test_cli_metadata_only_and_safe_error_codes(queue, capsys):
    from eimemory.cli.l1_recovery import main
    root = str(queue.path.parent.parent)
    assert main(["inspect", "--root", root]) == 0
    assert "SYNTHETIC_PRIVATE" not in capsys.readouterr().out
    assert main(["recover", "--root", root, "--job-id", "dead-1", "--dry-run",
                 "--operator-intent", INTENT]) == 0
    assert queue.pending_count() == 0
    assert "SYNTHETIC_PRIVATE" not in capsys.readouterr().out
    with patch.object(L1ExtractQueue, "inspect_dead", side_effect=OSError("SYNTHETIC_PRIVATE_ERROR")):
        assert main(["inspect", "--root", root]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error == {"ok": False, "error": "recovery_io_failed", "outcome": "unconfirmed"}


def test_missing_queue_is_not_an_empty_success_or_a_created_root(tmp_path):
    q = L1ExtractQueue(tmp_path / "missing-root" / "state" / "l1_extract_queue.json")
    with pytest.raises(L1QueueStateError, match="recovery_queue_missing"):
        q.inspect_dead()
    assert not q.path.parent.exists()
    with pytest.raises(L1QueueStateError, match="recovery_queue_missing"):
        recover(q)
    assert not q.path.parent.exists()


@pytest.mark.parametrize("recoveries", [None, {}])
def test_orphan_recovery_marker_cannot_bypass_ledger(queue, recoveries):
    orphan = dead("orphan", "orphan-parent")
    orphan.update(recovery_of="missing-original", status="queued")
    state = dict(jobs=[orphan], dead=[dead()])
    if recoveries is not None:
        state["recoveries"] = recoveries
    queue._save(state)
    before = queue.path.read_bytes()
    with pytest.raises(L1QueueStateError, match="recovery_state_invalid"):
        queue.inspect_dead()
    assert queue.path.read_bytes() == before


def test_malformed_recovery_ledger_fails_before_enqueue_or_recovery(queue):
    recover(queue)
    state = queue._load()
    state["recoveries"]["dead-1"]["original_attempts"] = 0
    queue._save(state)
    before = queue.path.read_bytes()
    for action in (lambda: recover(queue), lambda: queue.enqueue({"episode_id": "other"})):
        with pytest.raises(L1QueueStateError, match="recovery_state_invalid"):
            action()
        assert queue.path.read_bytes() == before


def test_inspection_is_bounded_and_selected_ids_are_not_truncated(queue):
    queue._save(dict(jobs=[], dead=[dead(f"dead-{i}", f"episode-{i}") for i in range(40)]))
    report = queue.inspect_dead(limit=5)
    assert len(report["jobs"]) == 5 and report["truncated"]
    assert [row["job_id"] for row in queue.inspect_dead(job_ids=["dead-1", "dead-3"], limit=1)["jobs"]] == ["dead-1", "dead-3"]


@pytest.mark.parametrize("status,expected", [("completed", "l1_recovery_already_completed"),
                                            ("dead", "l1_recovery_exhausted")])
def test_sync_turn_does_not_report_finished_recovery_as_queued(tmp_path, monkeypatch, status, expected):
    from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
    from eimemory.api.runtime import Runtime
    monkeypatch.setenv("EIMEMORY_L1_FORCE_QUEUE", "1")
    runtime = Runtime.create(root=tmp_path)
    try:
        service = AgentRuntimeMemoryService(runtime)
        monkeypatch.setattr(service, "_l1_queue", lambda: type("CompletedQueue", (), {
            "enqueue": lambda self, job: {"job_id": "recovery-completed", "status": status}})())
        result = service.sync_turn(channel="hermes", scope={"user_id": "synthetic-owner"},
                                   session_id="synthetic-session", turn_id="synthetic-turn",
                                   user_text="synthetic fixture", assistant_text="synthetic reply")
        assert result["l1_queued"] is False
        assert result["l1_extraction_reason"] == expected
    finally:
        runtime.close()


def test_recovery_ack_interruption_reuses_real_l1_completion_and_source_time(tmp_path, monkeypatch):
    from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
    from eimemory.api.runtime import Runtime
    from eimemory.metadata import business_metadata
    monkeypatch.setenv("EIMEMORY_L1_FORCE_QUEUE", "1")
    runtime = Runtime.create(root=tmp_path)
    try:
        service = AgentRuntimeMemoryService(runtime)
        captured = service.sync_turn(channel="hermes", scope={"user_id": "synthetic-owner"},
            session_id="synthetic-session", turn_id="synthetic-turn",
            user_text="以后回答先给结论，少解释。", assistant_text="synthetic reply")
        q = service._l1_queue()
        state = q._load()
        original = state["jobs"].pop()
        original.update(status="dead", attempts=5, last_error="synthetic prior outage")
        state["dead"] = [deepcopy(original)]
        q._save(state)
        q.recover_dead([original["job_id"]], operator_intent=INTENT)
        written = []
        def handle(job):
            written.append(service._extract_l1_inline(**{key: job.get(key, "") for key in (
                "user_text", "assistant_text", "turn_text", "episode_id", "channel_id", "channel_scope",
                "session_id", "turn_id")}))
        save = q._save
        def fail_ack(payload):
            if not payload["jobs"]:
                raise L1QueueStateError("queue_unwritable", retryable=True)
            save(payload)
        with patch.object(q, "_save", fail_ack):
            with pytest.raises(L1QueueStateError, match="queue_unwritable"):
                q.drain_report(handle, limit=1)
        assert written[0]
        assert q.drain_report(handle, limit=1)["processed"] == 1
        assert written[1] == []
        record = captured["record"]
        rows = runtime.store.list_records(kinds=["memory"], limit=100)
        atoms = [row for row in rows if business_metadata(row.meta).get("memory_layer") == "l1"]
        assert len(atoms) == len(written[0]) == 1
        assert atoms[0].time.occurred_at == record["time"]["occurred_at"]
        assert atoms[0].source_id == "hermes"
        assert q._load()["dead"] == [original]
        assert q.inspect_dead(job_ids=[original["job_id"]])["jobs"][0]["recovery_status"] == "completed"
    finally:
        runtime.close()


def test_ack_fault_after_publication_does_not_reprocess_completed_recovery(queue):
    recover(queue)
    seen = []
    save = queue._save
    def fail_after_ack(payload):
        save(payload)
        if not payload["jobs"]:
            raise L1QueueStateError("queue_unwritable", retryable=True)
    with patch.object(queue, "_save", fail_after_ack):
        with pytest.raises(L1QueueStateError, match="queue_unwritable"):
            queue.drain_report(lambda job: seen.append(job["job_id"]), limit=1)
    assert recover(queue)["already_recovered"] == 1
    assert queue.inspect_dead()["jobs"][0]["recovery_status"] == "completed"
    assert queue.drain_report(lambda job: seen.append(job["job_id"]), limit=1)["processed"] == 0
    assert len(seen) == 1


def test_enqueue_and_operator_recovery_race_cannot_publish_duplicate_parent(queue):
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    results = ctx.Queue()
    def producer():
        start.wait(5)
        q = L1ExtractQueue(queue.path)
        result = q.enqueue({"episode_id": "episode-1", "channel_id": "hermes",
                            "channel_scope": dead()["channel_scope"]})
        results.put(result["job_id"])
    def operator():
        start.wait(5)
        try:
            results.put(recover(L1ExtractQueue(queue.path))["published"])
        except L1QueueStateError as exc:
            results.put(exc.code)
    children = [ctx.Process(target=producer), ctx.Process(target=operator)]
    for child in children:
        child.start()
    start.set()
    for child in children:
        join_child(child)
    outcomes = [results.get(timeout=2) for _ in children]
    assert 1 in outcomes or "recovery_parent_conflict" in outcomes
    assert len(queue._load()["jobs"]) == 1


def test_operator_interface_does_not_follow_symlink_queue(queue, tmp_path):
    linked = tmp_path / "linked.json"
    linked.symlink_to(queue.path)
    q = L1ExtractQueue(linked)
    with pytest.raises(L1QueueStateError, match="recovery_state_invalid"):
        q.inspect_dead()
    with pytest.raises(L1QueueStateError, match="recovery_state_invalid"):
        recover(q)
