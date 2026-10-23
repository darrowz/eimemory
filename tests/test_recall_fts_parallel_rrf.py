"""F1: FTS/lexical is a normal parallel candidate arm fused with vector via RRF."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.contracts import CandidateHit, CandidateRef, ExactScope
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.postgres_vector import _merge_hits
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.storage.runtime_store import RuntimeStore


SCOPE_A = ScopeRef("tenant-f1", "agent-f1", "ws-a", "owner")
SCOPE_B = ScopeRef("tenant-f1", "agent-f1", "ws-b", "owner")


def _memory(
    store,
    *,
    record_id: str,
    text: str,
    scope: ScopeRef = SCOPE_A,
    source_id: str = "default",
    salience: float = 0.8,
):
    record = RecordEnvelope.create(
        kind="memory",
        title=text[:80],
        summary=text,
        detail=text,
        content={"text": text, "memory_type": "fact"},
        scope=scope,
        source_id=source_id,
        source="test.f1",
        meta={
            "memory_type": "fact",
            "force_capture": True,
            "quality": {"capture_decision": "accept", "salience_score": salience},
        },
    )
    record.record_id = record_id
    return store.append(record)


def test_fts_prefix_noise_without_lexical_score_is_not_keyword_eligible(tmp_path):
    """FTS can match cat→catastrophe; lexical_score 0 must not buy a keyword RRF vote."""
    with closing(RuntimeStore(tmp_path)) as store:
        source = SQLiteCandidateSource(store)
        engine = GovernedRecallEngine(store=store, candidate_source=source)
        hints = {
            "_fts_arm_present": True,
            "_provider_rank_is_hybrid": True,
            "_provider_rank": 1,
            "lexical_score": 0.0,
            "candidate_sources": ["fts", "vector"],
            "vector_score": 0.5,
        }
        assert engine._keyword_component_eligible(hints) is False
        assert engine._keyword_component_score(hints) == 0.0


def test_fts_arm_marked_and_keyword_eligible_for_hybrid_hits(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        _memory(store, record_id="mem_orchid_unique", text="orchid42 deployment marker unique phrase")
        source = SQLiteCandidateSource(store)
        from eimemory.retrieval.contracts import CandidateRequest

        batch = source.search(
            CandidateRequest.create(
                query="orchid42 unique phrase",
                scope=SCOPE_A,
                kinds=("memory",),
                limit=8,
            )
        )
        assert batch.hits
        target = next(hit for hit in batch.hits if hit.ref.record_id == "mem_orchid_unique")
        hints = target.component_dict()
        assert hints.get("_fts_arm_present") is True
        assert hints.get("_provider_rank_is_hybrid") is True
        assert "fts" in {str(s) for s in hints.get("candidate_sources") or ()}
        engine = GovernedRecallEngine(store=store, candidate_source=source)
        assert engine._keyword_component_eligible(hints) is True


def test_buried_fts_hit_survives_hybrid_quota_into_returned_pool(tmp_path):
    """Many high hybrid-score distractors must not erase a lexical FTS leader."""
    with closing(RuntimeStore(tmp_path)) as store:
        target_text = "zephyr-unique-token alpha bravo charlie"
        _memory(store, record_id="mem_zephyr_target", text=target_text, salience=0.2)
        # Distractors share generic tokens / structure so local-hash hybrid can
        # outrank the rare FTS match, but lack the unique FTS token.
        for index in range(40):
            _memory(
                store,
                record_id=f"mem_noise_{index:02d}",
                text=f"alpha bravo charlie generic filler noise item {index} padding " * 3,
                salience=0.95,
            )
        with store._lock:
            records, report = store.sqlite.search_with_diagnostics(
                query="zephyr-unique-token",
                kinds=["memory"],
                scope=SCOPE_A,
                limit=8,
                recall_filters={"_exact_scope": True},
            )
        ids = [record.record_id for record in records]
        assert "mem_zephyr_target" in ids
        scored = report.get("scored_items") or []
        target_score = next(item for item in scored if item.get("record_id") == "mem_zephyr_target")
        assert "fts" in {str(s) for s in target_score.get("candidate_sources") or ()}
        assert target_score.get("selection_reserve") == "fts_arm_quota" or ids.index(
            "mem_zephyr_target"
        ) < 8


def test_merge_hits_reserves_fts_quota_when_postgres_primary():
    scope = ExactScope.from_scope(SCOPE_A)
    sqlite_fts = [
        CandidateHit(
            CandidateRef(f"fts{i}", scope, "default"),
            i + 1,
            0.5,
            component_hints={"_fts_arm_present": True, "lexical_score": 2.0, "final_score": 0.5},
        )
        for i in range(4)
    ]
    sqlite_other = [
        CandidateHit(
            CandidateRef(f"sql{i}", scope, "default"),
            i + 10,
            0.1,
            component_hints={"vector_score": 0.1, "final_score": 0.1},
        )
        for i in range(8)
    ]
    postgres = [
        CandidateHit(
            CandidateRef(f"pg{i}", scope, "default"),
            i + 1,
            0.9,
            component_hints={"dense_vector_score": 0.9, "vector_score": 0.9},
        )
        for i in range(8)
    ]
    merged = _merge_hits(sqlite_fts + sqlite_other, postgres, limit=4, postgres_primary=True)
    ids = [hit.ref.record_id for hit in merged]
    assert len(merged) == 4
    assert any(record_id.startswith("fts") for record_id in ids), ids
    assert any(record_id.startswith("pg") for record_id in ids), ids




def test_merge_hits_reserves_fts_quota_when_fragments_off_postgres_primary():
    """Production path: evidence_fragments=False but postgres remains primary.

    Call sites now pass postgres_primary=True independently of fragments so the
    lexical reserve still fires and keyword-only memories are not starved.
    """
    scope = ExactScope.from_scope(SCOPE_A)
    sqlite_fts = [
        CandidateHit(
            CandidateRef(f"fts{i}", scope, "default"),
            i + 1,
            0.5,
            component_hints={"_fts_arm_present": True, "lexical_score": 2.0, "final_score": 0.5},
        )
        for i in range(4)
    ]
    postgres = [
        CandidateHit(
            CandidateRef(f"pg{i}", scope, "default"),
            i + 1,
            0.9,
            component_hints={"dense_vector_score": 0.9, "vector_score": 0.9},
        )
        for i in range(8)
    ]
    stats: dict[str, int] = {}
    # fragments-off production wiring: still postgres_primary=True
    merged = _merge_hits(sqlite_fts, postgres, limit=4, postgres_primary=True, stats=stats)
    ids = [hit.ref.record_id for hit in merged]
    assert len(merged) == 4
    assert any(record_id.startswith("fts") for record_id in ids), ids
    assert any(record_id.startswith("pg") for record_id in ids), ids
    assert stats.get("postgres_primary") == 1
    assert stats.get("lexical_reserve", 0) >= 1
    assert stats.get("fts_in_merged", 0) >= 1


def test_merge_hits_no_lexical_reserve_when_not_postgres_primary():
    scope = ExactScope.from_scope(SCOPE_A)
    sqlite_fts = [
        CandidateHit(
            CandidateRef(f"fts{i}", scope, "default"),
            i + 1,
            0.5,
            component_hints={"_fts_arm_present": True, "lexical_score": 2.0, "final_score": 0.5},
        )
        for i in range(4)
    ]
    postgres = [
        CandidateHit(
            CandidateRef(f"pg{i}", scope, "default"),
            i + 1,
            0.9,
            component_hints={"dense_vector_score": 0.9, "vector_score": 0.9},
        )
        for i in range(8)
    ]
    stats: dict[str, int] = {}
    merged = _merge_hits(sqlite_fts, postgres, limit=4, postgres_primary=False, stats=stats)
    assert len(merged) == 4
    assert stats.get("postgres_primary") == 0
    assert stats.get("lexical_reserve") == 0


def test_fts_scope_isolation(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        _memory(
            store,
            record_id="mem_scope_a",
            text="mangrove-orchid-token lives only in workspace a",
            scope=SCOPE_A,
        )
        _memory(
            store,
            record_id="mem_scope_b",
            text="mangrove-orchid-token lives only in workspace b",
            scope=SCOPE_B,
        )
        source = SQLiteCandidateSource(store)
        api = MemoryAPI(store, recall_engine=GovernedRecallEngine(store=store, candidate_source=source))
        bundle = api.recall(
            query="mangrove-orchid-token",
            scope=asdict(SCOPE_A),
            limit=8,
            task_context={"exact_scope_only": True, "runtime_channel": "proactive"},
        )
        ids = [item.record_id for item in bundle.items]
        assert "mem_scope_a" in ids
        assert "mem_scope_b" not in ids


def test_grounding_still_blocks_unrelated_vector_noise(tmp_path):
    """Dense-looking noise without lexical overlap must stay below grounding."""
    with closing(RuntimeStore(tmp_path)) as store:
        noise = _memory(
            store,
            record_id="mem_noise_only",
            text="completely unrelated gardening tips about tomatoes and soil",
        )
        source = SQLiteCandidateSource(store)
        engine = GovernedRecallEngine(store=store, candidate_source=source)
        score, reason = engine._non_exact_grounding_score(
            query="orchid42 deployment marker unique phrase",
            item=noise,
            evidence=set(),
            component_hints_by_ref={
                engine._record_key(noise): {
                    "_provider_rank_is_hybrid": True,
                    "_fts_arm_present": True,
                    "vector_score": 0.99,
                    "lexical_score": 0.0,
                    "lexical_signal": {"score": 0.0},
                    "candidate_sources": ["fts", "vector"],
                }
            },
            graph_grounded_ids=set(),
            vector_min_score=0.12,
            explicit_recall_boundary=False,
        )
        assert score < float(engine._relevance_selector_thresholds["non_exact_min_grounding"])
        assert reason in {"grounding", "requested_attribute_missing"} or score < 0.08


def test_end_to_end_lexical_only_memory_recalled_despite_vector_distractors(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        target = _memory(
            store,
            record_id="mem_lexical_only",
            text="quokka-unique-token project delivery note for hermes gateway",
            salience=0.25,
        )
        for index in range(60):
            _memory(
                store,
                record_id=f"mem_vec_noise_{index:02d}",
                text=f"project delivery note for hermes gateway padding {index} " * 4,
                salience=0.99,
            )
        source = SQLiteCandidateSource(store)
        api = MemoryAPI(store, recall_engine=GovernedRecallEngine(store=store, candidate_source=source))
        bundle = api.recall(
            query="quokka-unique-token",
            scope=asdict(SCOPE_A),
            limit=8,
            task_context={"exact_scope_only": True, "runtime_channel": "proactive"},
        )
        ids = [item.record_id for item in bundle.items]
        assert target.record_id in ids, (ids, bundle.explanation)
