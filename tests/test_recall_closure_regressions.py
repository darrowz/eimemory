from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from copy import deepcopy
from threading import Barrier

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import LinkRef, ScopeRef


SCOPE = {"tenant_id": "audit", "agent_id": "agent", "workspace_id": "workspace", "user_id": "alice"}


def test_concurrent_usage_retries_reward_once_after_restart(tmp_path, monkeypatch):
    with ExitStack() as stack:
        runtimes = [stack.enter_context(closing(Runtime.create(root=tmp_path))) for _ in range(4)]
        memory = runtimes[0].memory.ingest(
            text="The Orion service primary database is PostgreSQL.", title="Orion service database",
            memory_type="fact", scope=SCOPE, source_id="alpha", force_capture=True,
        )
        barrier = Barrier(len(runtimes))
        for runtime in runtimes:
            original = runtime.store.get_by_idempotency_key

            def align_read(*args, _original=original, **kwargs):
                found = _original(*args, **kwargs)
                barrier.wait(timeout=5)
                return found

            # Align the read-only fast path; writes still use real SQLite
            # transactions and separate Runtime connections.
            monkeypatch.setattr(runtime.store, "get_by_idempotency_key", align_read)

        def observe(runtime):
            return runtime.record_memory_usage(query_id="one-query", scope=SCOPE, source_id="alpha",
                                               used_record_ids=[memory.record_id]).record_id

        with ThreadPoolExecutor(max_workers=len(runtimes)) as pool:
            record_ids = list(pool.map(observe, runtimes))
        assert len(set(record_ids)) == 1
    with closing(Runtime.create(root=tmp_path)) as runtime:
        feedback = runtime.store.list_records(kinds=["feedback"], scope=SCOPE)
        assert len(feedback) == 1
        bundle = runtime.memory.recall(query="Orion service database PostgreSQL", scope=SCOPE,
                                       task_context={"source_ids": ["alpha"]})
        score = next(item for item in bundle.explanation["scoring"] if item["record_id"] == memory.record_id)
        assert score["telemetry_used_count"] == 1
        assert score["telemetry_adjustment"] == 0.08


def test_usage_retry_resolves_legacy_random_id_in_exact_namespace(tmp_path):
    with closing(Runtime.create(root=tmp_path)) as runtime:
        original = runtime.record_memory_usage(query_id="legacy-query", scope=SCOPE, source_id="alpha",
                                               used_record_ids=["original-memory"], persist=False)
        original.record_id = "feedback_legacy_random_id"
        runtime.store.append(original)
        # A readable shared record or another source partition must not become
        # the authoritative retry, even if it carries the same metadata key.
        for name, user, source in (("shared", "", "alpha"), ("other-source", "alice", "beta")):
            shadow = deepcopy(original)
            shadow.record_id = f"feedback_shadow_{name}"
            shadow.scope.user_id = user
            shadow.source_id = source
            shadow.content["used_record_ids"] = ["wrong-memory"]
            shadow.time.updated_at = "2099-01-01T00:00:00Z"
            runtime.store.append(shadow)
    with closing(Runtime.create(root=tmp_path)) as runtime:
        retry = runtime.record_memory_usage(query_id="legacy-query", scope=SCOPE, source_id="alpha",
                                            used_record_ids=["changed-retry-memory"])
        assert retry.record_id == original.record_id
        assert retry.scope == ScopeRef.from_dict(SCOPE) and retry.source_id == "alpha"
        assert retry.content["used_record_ids"] == ["original-memory"]
        assert len(runtime.store.list_records(kinds=["feedback"], scope=SCOPE)) == 3


def test_usage_outbox_failure_rolls_back_feedback_and_retry_recovers(tmp_path, monkeypatch):
    with closing(Runtime.create(root=tmp_path)) as runtime:
        enqueue = runtime.store._enqueue_record_exports

        def fail(record):
            if record.kind == "feedback":
                raise OSError("injected export enqueue failure")
            return enqueue(record)

        with monkeypatch.context() as fault:
            fault.setattr(runtime.store, "_enqueue_record_exports", fail)
            with pytest.raises(OSError, match="export enqueue"):
                runtime.record_memory_usage(query_id="atomic-query", scope=SCOPE, used_record_ids=["memory"])
        assert runtime.store.list_records(kinds=["feedback"], scope=SCOPE) == []
    with closing(Runtime.create(root=tmp_path)) as runtime:
        first = runtime.record_memory_usage(query_id="atomic-query", scope=SCOPE, used_record_ids=["memory"])
        retry = runtime.record_memory_usage(query_id="atomic-query", scope=SCOPE, used_record_ids=["memory"])
        assert first.record_id == retry.record_id
        assert len(runtime.store.list_records(kinds=["feedback"], scope=SCOPE)) == 1


@pytest.mark.parametrize("filters", [
    {"blocked_sources": ["forbidden.transcript"]},
    {"allowed_sources": ["allowed.fact", "allowed.transcript"]},
    {},
])
def test_episode_backrefs_obey_explicit_source_filters_without_losing_allowed_evidence(tmp_path, filters):
    with closing(Runtime.create(root=tmp_path)) as runtime:
        episodes = []
        for source, title in (("forbidden.transcript", "Forbidden transcript title"),
                              ("allowed.transcript", "Allowed transcript title")):
            episodes.append(runtime.memory.ingest(
                text=f"{title}: the Orion database decision was discussed.", title=title,
                memory_type="conversation", source=source, source_id="alpha", scope=SCOPE, force_capture=True,
            ))
        fact = runtime.memory.ingest(
            text="The Orion service uses PostgreSQL as its primary database.", title="Orion service database",
            memory_type="fact", source="allowed.fact", source_id="alpha", scope=SCOPE, force_capture=True,
            links=[LinkRef(relation="derived_from", target_kind="memory", target_id=item.record_id) for item in episodes],
        )
        bundle = runtime.memory.recall(query="Orion service database PostgreSQL", scope=SCOPE,
                                       task_context={"source_ids": ["alpha"], **filters})
        assert fact.record_id in {item.record_id for item in bundle.items}
        expected = {episodes[1].record_id} if filters else {item.record_id for item in episodes}
        assert {item["record_id"] for item in bundle.explanation["cascade_evidence"]} == expected
        compact_ids = {item["record_id"] for item in bundle.to_compact_dict()["evidence"]}
        assert compact_ids and compact_ids <= expected
        if filters:
            assert compact_ids == expected
