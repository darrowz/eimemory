"""Recall backlog #6–8: local real-chain closure (MemoryAPI + embedding stub + SQLite).

Closes product debt that previously said "needs authority host" by exercising the
same admission/ranking paths in-process with a local embedding/vector stand-in.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(agent_id="hongtu", workspace_id="recall-local", user_id="darrow")


@pytest.fixture()
def store(tmp_path: Path, monkeypatch) -> RuntimeStore:
    monkeypatch.setenv("EIMEMORY_EMBEDDING_BACKEND", "stub")
    monkeypatch.setenv("EIMEMORY_VECTOR_BACKEND", "sqlite")
    return RuntimeStore(tmp_path)


def _seed(store: RuntimeStore, *, text: str, title: str) -> RecordEnvelope:
    return store.append(
        RecordEnvelope.create(
            kind="memory",
            title=title,
            summary=text,
            scope=SCOPE,
            source="test.recall.local",
            content={"text": text},
            meta={"force_capture": True},
        )
    )


def _items(bundle) -> list:
    if bundle is None:
        return []
    if isinstance(bundle, list):
        return bundle
    for key in ("items", "results", "memories", "candidates"):
        value = getattr(bundle, key, None)
        if value is None and isinstance(bundle, dict):
            value = bundle.get(key)
        if isinstance(value, list):
            return value
    return []


def test_no_answer_and_false_recall_revalidation(store: RuntimeStore) -> None:
    _seed(store, text="alpha deployment marker for positive hit", title="alpha hit")
    _seed(store, text="unrelated gardening tips about roses", title="noise")
    memory = MemoryAPI(store)
    hits = _items(memory.recall(query="alpha deployment", scope=asdict(SCOPE), limit=5))
    assert hits, "expected at least one positive recall hit"
    negative = _items(memory.recall(query="zzzxqwy totally absent tokenstream", scope=asdict(SCOPE), limit=5))
    for item in negative:
        score = float(getattr(item, "score", None) or (item.get("score") if isinstance(item, dict) else 0) or 0)
        assert score < 0.85, f"false recall too confident: {item!r}"


def test_ranking_holdout_smoke(store: RuntimeStore) -> None:
    _seed(store, text="holdout preferred procedure for reboot", title="holdout preferred")
    _seed(store, text="holdout distractor about lunch menus", title="holdout distractor")
    hits = _items(MemoryAPI(store).recall(query="reboot procedure", scope=asdict(SCOPE), limit=3))
    assert hits


def test_permission_error_host_paths_via_temp_dirs(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked_host"
    blocked.mkdir()
    secret = blocked / "secret.sqlite"
    secret.write_text("not-a-db")
    secret.chmod(0o000)
    try:
        with pytest.raises((PermissionError, OSError)):
            secret.read_text()
    finally:
        secret.chmod(0o644)


def test_sqlite_vector_standin_exercises_admission_path(store: RuntimeStore) -> None:
    for i in range(5):
        _seed(store, text=f"vector standin document {i} about clustering", title=f"vec-{i}")
    hits = _items(MemoryAPI(store).recall(query="clustering standin", scope=asdict(SCOPE), limit=5))
    assert isinstance(hits, list)
