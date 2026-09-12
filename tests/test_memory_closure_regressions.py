from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.knowledge.l1_queue import L1ExtractQueue, MAX_ATTEMPTS
from eimemory.models.records import ScopeRef
from eimemory.storage.atomic_file import atomic_write_json


SCOPE = {"tenant_id": "audit", "agent_id": "agent", "workspace_id": "ws", "user_id": "alice"}
REPO = Path(__file__).resolve().parents[1]


def rpc(bridge, method, **params):
    return bridge.handle({"method": method, "params": params})


@pytest.mark.parametrize("channel", ["codex", "hermes", "openclaw"])
@pytest.mark.parametrize("send_default_title", [False, True])
def test_default_adapter_remember_preserves_unrelated_facts_after_restart(tmp_path, channel, send_default_title):
    with closing(Runtime.create(root=tmp_path)) as runtime:
        bridge = EIBrainRPCBridge(runtime)
        common = dict(channel=channel, scope=SCOPE, force_capture=True)
        if send_default_title:
            common["title"] = f"{channel.title()} long-term memory"
        first = rpc(bridge, "adapter.remember", **common, event_id="database",
                    text="The project uses PostgreSQL as its primary application database.")
        second = rpc(bridge, "adapter.remember", **common, event_id="deployment",
                     text="Production deployments require explicit approval from the release manager.")
        assert first["ok"] and second["ok"]
        retry = rpc(bridge, "adapter.remember", **common, event_id="database",
                    text="The project uses PostgreSQL as its primary application database.")
        assert retry["result"]["idempotent"] is True
        assert retry["result"]["record"]["record_id"] == first["result"]["record"]["record_id"]
    with closing(Runtime.create(root=tmp_path)) as runtime:
        scope = ScopeRef.from_dict(resolve_channel_scope(channel, SCOPE))
        first_id = first["result"]["record"]["record_id"]
        assert runtime.store.get_by_exact_ref(first_id, scope=scope, source_id=channel).status == "active"
        bundle = runtime.memory.recall(query="PostgreSQL primary application database", scope=resolve_channel_scope(channel, SCOPE))
        assert first_id in {item.record_id for item in bundle.items}


@pytest.mark.parametrize("identity", ["title", "semantic_key", "nested_semantic_key"])
def test_remember_keeps_explicit_fact_replacement(tmp_path, identity):
    with closing(Runtime.create(root=tmp_path)) as runtime:
        bridge = EIBrainRPCBridge(runtime)
        common = dict(channel="codex", scope=SCOPE, force_capture=True)
        if identity == "title":
            common["title"] = "Primary application database"
        elif identity == "semantic_key":
            common["meta"] = {"semantic_key": "application-database"}
        else:
            common["meta"] = {"business_meta": {"semantic_key": "application-database"}}
        first = rpc(bridge, "adapter.remember", **common, event_id="old", text="The application database is MySQL.")
        second = rpc(bridge, "adapter.remember", **common, event_id="new", text="The application database is PostgreSQL.")
        assert first["ok"] and second["ok"]
        scope = ScopeRef.from_dict(resolve_channel_scope("codex", SCOPE))
        old = runtime.store.get_by_exact_ref(first["result"]["record"]["record_id"], scope=scope, source_id="codex")
        assert old.status == "superseded"


@pytest.mark.parametrize("action", ["replace", "remove"])
@pytest.mark.parametrize("target_mode", ["id", "old_text"])
def test_hermes_mutation_cannot_write_shared_readable_record(tmp_path, action, target_mode):
    shared = {**SCOPE, "user_id": ""}
    shared_exact = ScopeRef.from_dict(resolve_channel_scope("hermes", shared))
    with closing(Runtime.create(root=tmp_path)) as runtime:
        bridge = EIBrainRPCBridge(runtime)
        common = dict(channel="hermes", target="memory", source_id="hermes",
                      provenance={"write_origin": "hermes.memory_write"})
        content = "Shared deployment rules require all integration tests to pass."
        added = rpc(bridge, "adapter.mutate_memory", **common, scope=shared, action="add",
                    content=content, idempotency_key="shared-add")
        assert added["ok"] is True
        target = added["result"]["record"]
        lookup = ({"target_record_id": target["record_id"], "expected_revision": added["result"]["content_revision"]}
                  if target_mode == "id" else {"old_text": content})
        denied = rpc(bridge, "adapter.mutate_memory", **common, **lookup, scope=SCOPE, action=action,
                     content="Replacement integration policy." if action == "replace" else "",
                     idempotency_key="alice-write")
        assert denied["ok"] is False
        assert denied["error"] in {"mutation_target_scope_mismatch", "mutation_target_not_found"}
    with closing(Runtime.create(root=tmp_path)) as runtime:
        stored = runtime.store.get_by_exact_ref(target["record_id"], scope=shared_exact, source_id="hermes")
        assert stored.to_dict() == target
        # Exact owners still have the same replace/remove API and revision contract.
        allowed = rpc(EIBrainRPCBridge(runtime), "adapter.mutate_memory", **common, **lookup,
                      scope=shared, action=action, content="Replacement integration policy." if action == "replace" else "",
                      idempotency_key="owner-write")
        assert allowed["ok"] is True


def test_queue_recovers_crashed_worker_and_fences_previous_claim(tmp_path):
    path = tmp_path / "queue.json"
    queue = L1ExtractQueue(path)
    queue.enqueue({"episode_id": "crashed-episode"})
    code = ("import os,sys; from eimemory.knowledge.l1_queue import L1ExtractQueue; "
            "L1ExtractQueue(sys.argv[1]).drain_report(lambda job: os._exit(17), limit=1)")
    result = subprocess.run([sys.executable, "-c", code, str(path)], cwd=REPO, timeout=10)
    assert result.returncode == 17
    previous = json.loads(path.read_text())["jobs"][0]
    restarted = L1ExtractQueue(path)
    handled = []

    def handle(current):
        handled.append(current)
        assert current["claim_token"] != previous["claim_token"]
        assert restarted._complete(previous["job_id"], claim_token=previous["claim_token"]) is False
        restarted._fail(previous["job_id"], "late failure", claim_token=previous["claim_token"])
        persisted = json.loads(path.read_text())["jobs"][0]
        assert persisted["status"] == "running" and persisted["claim_token"] == current["claim_token"]

    report = restarted.drain_report(handle, limit=1)
    assert report["processed"] == 1 and report["pending"] == 0 and report["dead"] == 0
    assert len(handled) == 1 and handled[0]["attempts"] == 2


@pytest.mark.parametrize("attempts", [1, MAX_ATTEMPTS])
def test_queue_recovers_legacy_running_jobs_and_bounds_crash_retries(tmp_path, attempts):
    path = tmp_path / "queue.json"
    atomic_write_json(path, {"jobs": [{"job_id": "legacy", "episode_id": "legacy",
                                      "status": "running", "attempts": attempts}], "dead": []})
    handled = []
    report = L1ExtractQueue(path).drain_report(lambda job: handled.append(job), limit=1)
    assert report["pending"] == 0
    assert report["processed"] == len(handled) == (1 if attempts < MAX_ATTEMPTS else 0)
    assert report["newly_dead"] == report["dead"] == (1 if attempts == MAX_ATTEMPTS else 0)


def test_queue_does_not_steal_live_worker_or_block_enqueue(tmp_path):
    path = tmp_path / "queue.json"
    started, release, output, attempting = (tmp_path / name for name in ("started", "release", "second.json", "attempting"))
    queue = L1ExtractQueue(path)
    queue.enqueue({"episode_id": "live"})
    first_code = """
import sys, time
from pathlib import Path
from eimemory.knowledge.l1_queue import L1ExtractQueue
def handle(job):
    Path(sys.argv[2]).write_text('started')
    deadline = time.monotonic() + 10
    while not Path(sys.argv[3]).exists():
        if time.monotonic() > deadline:
            raise RuntimeError('test release timed out')
        time.sleep(0.01)
L1ExtractQueue(sys.argv[1]).drain_report(handle, limit=1)
"""
    second_code = """
import sys, json
from pathlib import Path
from eimemory.knowledge.l1_queue import L1ExtractQueue
Path(sys.argv[3]).write_text('attempting')
report = L1ExtractQueue(sys.argv[1]).drain_report(lambda job: None, limit=1)
Path(sys.argv[2]).write_text(json.dumps(report))
"""
    first = subprocess.Popen([sys.executable, "-c", first_code, str(path), str(started), str(release)], cwd=REPO)
    second = None
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        second = subprocess.Popen([sys.executable, "-c", second_code, str(path), str(output), str(attempting)], cwd=REPO)
        deadline = time.monotonic() + 5
        while not attempting.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert attempting.exists()
        with pytest.raises(subprocess.TimeoutExpired):
            second.wait(timeout=0.2)
        assert not output.exists()
        # Enqueue deduplicates against the active job without awaiting the handler.
        assert queue.enqueue({"episode_id": "live"})["status"] == "running"
        release.write_text("release")
        assert first.wait(timeout=5) == second.wait(timeout=5) == 0
        assert json.loads(output.read_text())["processed"] == 0
        assert queue.pending_count() == 0
    finally:
        release.touch()
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
