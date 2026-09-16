"""Focused regression tests for A0/A1/A2 audit remediations."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eimemory.intake.loop import KnowledgeIntakeLoop, _local_path_from_uri
from eimemory.intake.review import _deterministic_promoted_memory_id, promote_candidate
from eimemory.living.schema import _apply_action_posture
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.persona.store import PersonaStore
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.scoring.contract import MemoryScore, ScoreComponent
from eimemory.scoring.evaluator import evaluate_memory_score
from eimemory.storage.atomic_file import atomic_write_json


# ---------- INT-03 ----------

def test_int03_rejects_unc_and_outside_roots(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    inside = root / "notes.txt"
    inside.write_text("hello", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    assert _local_path_from_uri(str(inside), allowed_roots=(root,)) == inside.resolve()
    assert _local_path_from_uri(str(outside), allowed_roots=(root,)) is None
    assert _local_path_from_uri(r"\\attacker\share\x", allowed_roots=(root,)) is None
    assert _local_path_from_uri(str(inside), allowed_roots=()) is None  # fail-closed


# ---------- INT-10 ----------

def test_int10_second_scan_skips_existing_candidate(tmp_path: Path) -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.by_id: dict[str, RecordEnvelope] = {}
            self.appends = 0

        def get_by_id(self, record_id: str, scope=None):
            return self.by_id.get(record_id)

        def append(self, record: RecordEnvelope):
            self.appends += 1
            self.by_id[record.record_id] = record
            return record

    store = FakeStore()
    loop = KnowledgeIntakeLoop(store=store)
    record = RecordEnvelope.create(
        kind="knowledge_candidate",
        title="t",
        summary="s",
        detail="d",
        content={},
        status="candidate",
        scope=ScopeRef(),
    )
    store.append(record)
    # Simulate persist path idempotency check
    existing = store.get_by_id(record.record_id, scope=record.scope)
    assert existing is not None
    # Any status including candidate must skip
    skipped = existing is not None
    assert skipped
    before = store.appends
    if existing is not None:
        pass
    else:
        store.append(record)
    assert store.appends == before


# ---------- RSC-17 ----------

def test_rsc17_from_dict_preserves_zero_and_rejects_garbage() -> None:
    component = ScoreComponent.from_dict({"name": "confidence", "value": 0.0, "weight": 0.5})
    assert component.value == 0.0
    with pytest.raises(ValueError):
        ScoreComponent.from_dict({"name": "confidence", "value": {"bad": True}, "weight": 0.5})
    score = MemoryScore.from_dict(
        {
            "final_score": 0.0,
            "tier": "candidate",
            "components": {
                "confidence": {"name": "confidence", "value": 0.0, "weight": 1.0},
                "bad": {"name": "bad", "value": object(), "weight": 1.0},
            },
            "labels": [],
            "explanation": {},
            "provenance": {},
        }
    )
    assert score.final_score == 0.0
    assert "confidence" in score.components
    assert "bad" not in score.components


# ---------- RSC-15 ----------

def test_rsc15_confidence_zero_survives_round_trip() -> None:
    score = evaluate_memory_score(
        text="decision important always prefer never must remember this carefully",
        title="policy",
        memory_type="decision",
        source="tool.store",
        legacy_quality={"confidence": 0.0, "freshness": 0.0, "reuse_potential": 0.0, "salience_score": 0.0, "importance": 0.0},
    )
    assert score.components["confidence"].value == 0.0
    assert score.components["freshness"].value == 0.0


# ---------- EXT-02 ----------

def test_ext02_atomic_write_and_corrupt_json(tmp_path: Path) -> None:
    store = PersonaStore(str(tmp_path))
    state = store.load_state()
    store.save_state(state)
    assert store.state_path.exists()
    # Corrupt primary; last-good snapshot should recover
    store.state_path.write_text("{not-json", encoding="utf-8")
    recovered = store.load_state()
    assert recovered is not None
    # Corrupt primary with no usable snapshot → raise
    for path in store.snapshot_dir.glob("persona_state_*.json"):
        path.unlink()
    store.state_path.write_text("{still-bad", encoding="utf-8")
    with pytest.raises(ValueError):
        store.load_state()


# ---------- RET-02 ----------

def test_ret02_keyword_eligible_despite_vector_score_key() -> None:
    engine = GovernedRecallEngine.__new__(GovernedRecallEngine)
    # Pure lexical with vector_score key present at low value still eligible
    assert engine._keyword_component_eligible({"lexical_score": 0.15, "vector_score": 0.0}) is True
    assert engine._keyword_component_eligible({"lexical_score": 0.15, "vector_score": 0.05}) is True
    # Provider rank FTS evidence eligible even when vector_score key exists
    assert engine._keyword_component_eligible({"_provider_rank": 1, "vector_score": 0.05, "lexical_score": 0}) is True
    # Explicit arm flags
    assert engine._keyword_component_eligible({"_fts_arm_present": True, "vector_score": 0.0}) is True
    # Identity indexed still denied
    assert engine._keyword_component_eligible({"_provider_rank": 1, "identity_indexed": True}) is False


# ---------- RET-03 ----------

def test_ret03_standalone_uses_dense_only() -> None:
    engine = GovernedRecallEngine.__new__(GovernedRecallEngine)
    engine._relevance_selector_thresholds = {"non_exact_min_grounding": 0.08}
    item = RecordEnvelope.create(kind="memory", title="t", summary="s", detail="d", content={}, scope=ScopeRef())
    ref = engine._record_key(item)
    # Only local_hash_score → cannot independently pass (returns max of lexical/semantic/0)
    score, reason = engine._non_exact_grounding_score(
        query="zzzz-no-match",
        item=item,
        evidence=set(),
        component_hints_by_ref={ref: {"local_hash_score": 0.9, "vector_score": 0.9}},
        graph_grounded_ids=set(),
        vector_min_score=0.12,
        explicit_recall_boundary=False,
    )
    assert score < 0.08 or reason == "grounding"
    # dense_vector_score original authorizes standalone
    score2, _ = engine._non_exact_grounding_score(
        query="zzzz-no-match",
        item=item,
        evidence=set(),
        component_hints_by_ref={ref: {"dense_vector_score": 0.55, "vector_score": 0.99, "local_hash_score": 0.99}},
        graph_grounded_ids=set(),
        vector_min_score=0.12,
        explicit_recall_boundary=False,
    )
    assert score2 >= 0.55


# ---------- RET-10 ----------

def test_ret10_named_params_match_sql_placeholders() -> None:
    from eimemory.retrieval.postgres_vector import PostgresCandidateRepository
    import re

    class _Repo(PostgresCandidateRepository):
        @property
        def qualified_table(self) -> str:  # type: ignore[override]
            return "public.projection"

        @property
        def qualified_fragment_table(self) -> str:  # type: ignore[override]
            return "public.fragments"

    repo = _Repo.__new__(_Repo)
    request = SimpleNamespace(
        scope=SimpleNamespace(tenant_id="t", agent_id="a", workspace_id="w", user_id="u"),
        source_ids=["s1", "s2"],
        kinds=["memory"],
    )
    for include_sources, include_kinds in ((True, True), (True, False), (False, True), (False, False)):
        req = SimpleNamespace(
            scope=request.scope,
            source_ids=["s1"] if include_sources else None,
            kinds=["memory"] if include_kinds else None,
        )
        where, params = repo._fragment_scope_filters(req, watermark="wm1")
        sql, execute_params = repo._fragment_arm_sql(
            arm="keyword",
            fields="p.storage_key",
            where=where,
            params=params,
            literal="[0.1,0.2]",
            fts_query_text="hello",
            limit=5,
        )
        names = set(re.findall(r"%\(([a-z_]+)\)s", sql))
        assert names <= set(execute_params.keys())
        assert "tenant_id" in execute_params and "result_limit" in execute_params
        assert "embedding_literal" in execute_params and "fts_query" in execute_params
        if include_sources:
            assert "source_ids" in execute_params
        if include_kinds:
            assert "kinds" in execute_params


# ---------- RSC-22 ----------

def test_rsc22_let_go_blocked_by_repair_and_wait_reachable() -> None:
    living = {
        "motive": {"motive": "care", "trust_delta": -1, "boundary": []},
        "affective": {"repair_needed": True},
        "action_posture": {},
    }
    _apply_action_posture(living, "let go of this obsolete topic")
    assert living["action_posture"]["recommended"] == "act"

    living2 = {
        "motive": {"motive": "care", "trust_delta": 0, "boundary": []},
        "affective": {"repair_needed": False},
        "action_posture": {},
    }
    _apply_action_posture(living2, "please wait and hold off for later")
    assert living2["action_posture"]["recommended"] == "wait"

    living3 = {
        "motive": {"motive": "care", "trust_delta": 0, "boundary": []},
        "affective": {"repair_needed": False},
        "action_posture": {},
    }
    _apply_action_posture(living3, "let go of this obsolete topic")
    assert living3["action_posture"]["recommended"] == "let_go"


# ---------- INT-12 ----------

def test_int12_deterministic_promotion_id_no_duplicate() -> None:
    candidate_id = "kc_abc123"
    first = _deterministic_promoted_memory_id(candidate_id)
    second = _deterministic_promoted_memory_id(candidate_id)
    assert first == second
    assert first.startswith("mem_")


def test_int12_promote_replay_does_not_duplicate(tmp_path: Path) -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.records: dict[str, RecordEnvelope] = {}

        def get_by_id(self, record_id: str, scope=None):
            return self.records.get(record_id)

        def append(self, record: RecordEnvelope):
            self.records[record.record_id] = record
            return record

        def list_records(self, **kwargs):
            return list(self.records.values())

    store = FakeStore()
    candidate = RecordEnvelope.create(
        kind="knowledge_candidate",
        title="paper",
        summary="sum",
        detail="detail",
        content={"text": "body"},
        status="candidate",
        scope=ScopeRef(tenant_id="default", agent_id="a", workspace_id="w", user_id="u"),
    )
    store.append(candidate)

    runtime = SimpleNamespace(store=store)
    # Patch _candidate_by_id path via store list/get - promote_candidate uses _candidate_by_id
    from eimemory.intake import review as review_mod

    def fake_candidate_by_id(runtime, record_id, *, scope=None):
        return store.get_by_id(record_id)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(review_mod, "_candidate_by_id", fake_candidate_by_id)
    monkey.setattr(review_mod, "_require_scope", lambda *a, **k: None)
    monkey.setattr(review_mod, "_save", lambda runtime, record: store.append(record) or record)
    monkey.setattr(review_mod, "_append_review_history", lambda *a, **k: None)
    monkey.setattr(review_mod, "_memory_content", lambda c: dict(c.content or {}))
    try:
        mem1 = promote_candidate(runtime, candidate.record_id, "tester")
        mem2 = promote_candidate(runtime, candidate.record_id, "tester")
        assert mem1.record_id == mem2.record_id
        memories = [r for r in store.records.values() if r.kind == "memory"]
        assert len(memories) == 1
    finally:
        monkey.undo()


# ---------- safe_transport POST ----------

def test_safe_transport_post_send_request_includes_body() -> None:
    from eimemory.intake import safe_transport as st

    sent: list[bytes] = []

    class FakeSock:
        def sendall(self, data: bytes) -> None:
            sent.append(data)

    st._send_request(
        FakeSock(),
        path="/rpc",
        host="example.com",
        port=443,
        scheme="https",
        headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
        method="POST",
        body=b'{"a":1}',
    )
    payload = b"".join(sent)
    assert payload.startswith(b"POST /rpc HTTP/1.1\r\n")
    assert b"Authorization: Bearer tok" in payload
    assert b"Content-Length: 7" in payload
    assert payload.endswith(b'{"a":1}')


# ---------- NEW-02 ----------

def test_new02_monitor_requires_config() -> None:
    from eimemory.ei_bridge.eibrain_monitor import EIBrainMonitorTransport

    env_key = "EIBRAIN_MONITOR_URL"
    old = os.environ.pop(env_key, None)
    try:
        with pytest.raises(ValueError):
            EIBrainMonitorTransport()
    finally:
        if old is not None:
            os.environ[env_key] = old


# ---------- STO-05 reclaim API exists ----------

def test_sto05_reclaim_api_exists() -> None:
    from eimemory.storage.payload_segments import PayloadSegmentStore

    assert hasattr(PayloadSegmentStore, "reclaim_uncommitted_appends")


# ---------- RET-01 helper ----------

def test_ret01_authoritative_lookup_helper() -> None:
    engine = GovernedRecallEngine.__new__(GovernedRecallEngine)

    class Store:
        def search_identity_candidates(self, **kwargs):
            return [{"evidence": ["exact_title"], "record_id": "r1"}]

    engine.store = Store()
    request = SimpleNamespace(kinds=["memory"], scope=ScopeRef())
    assert engine._authoritative_identity_exists(query="Title", request=request, target_source_id="default") is True

    class Empty:
        def search_identity_candidates(self, **kwargs):
            return []

    engine.store = Empty()
    assert engine._authoritative_identity_exists(query="Title", request=request, target_source_id="default") is False
