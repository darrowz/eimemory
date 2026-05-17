from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.raw.retrieval import rerank_raw_results
from eimemory.raw.synthetic import synthetic_preference_texts


def test_raw_hybrid_recall_exposes_raw_evidence_with_explicit_preference_first(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x", user_id="darrow")
    raw_preference = RecordEnvelope.create(
        kind="memory",
        title="Raw chat chunk",
        summary="User: I prefer concise status updates before details.",
        content={
            "text": "User: I prefer concise status updates before details.",
            "raw_text": "User: I prefer concise status updates before details.",
            "speaker": "user",
            "current": True,
        },
        scope=scope,
        source="raw_chunk",
        meta={"memory_type": "raw_chunk"},
    )
    structured = runtime.memory.ingest(
        text="The user likes careful implementation notes.",
        memory_type="preference",
        title="Structured preference",
        scope={"agent_id": "main", "workspace_id": "repo-x", "user_id": "darrow"},
        force_capture=True,
    )
    runtime.store.append(raw_preference)

    bundle = runtime.memory.recall(
        query="What communication style does the user prefer?",
        scope={"agent_id": "main", "workspace_id": "repo-x", "user_id": "darrow"},
        task_context={"task_type": "chat.reply", "recall_mode": "raw_hybrid"},
        limit=5,
    )

    assert structured.record_id in {item.record_id for item in bundle.items}
    assert bundle.explanation["recall_mode"] == "raw_hybrid"
    assert bundle.explanation["raw_evidence"]
    top = bundle.explanation["raw_evidence"][0]
    assert top["record"]["record_id"] == raw_preference.record_id
    assert "I prefer concise status updates before details" in top["record"]["text"]
    assert top["base_score"] >= 0
    assert top["final_score"] >= top["base_score"]
    assert "preference_pattern" in top["boosts"]


def test_default_recall_does_not_add_raw_evidence_or_change_items(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main", "workspace_id": "repo-x"}
    runtime.memory.ingest(
        text="Default recall target should remain structured only.",
        memory_type="fact",
        title="Default structured memory",
        scope=scope,
        force_capture=True,
    )
    raw = RecordEnvelope.create(
        kind="memory",
        title="Raw only chunk",
        summary="User: I prefer yaml summaries.",
        content={"text": "User: I prefer yaml summaries.", "raw_text": "User: I prefer yaml summaries."},
        scope=ScopeRef.from_dict(scope),
        source="raw_chunk",
        meta={"memory_type": "raw_chunk"},
    )
    runtime.store.append(raw)

    bundle = runtime.memory.recall(
        query="Default recall target",
        scope=scope,
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    assert [item.title for item in bundle.items] == ["Default structured memory"]
    assert "raw_evidence" not in bundle.explanation
    assert bundle.explanation["recall_mode"] == "structured"


def test_synthetic_preference_texts_extracts_retrievable_preferences() -> None:
    texts = synthetic_preference_texts(
        "I prefer concise updates. I like source links. I don't like vague summaries. "
        "I find calendar search more reliable."
    )

    assert "User preference: prefer concise updates" in texts
    assert "User preference: like source links" in texts
    assert "User preference: do not like vague summaries" in texts
    assert "User preference: find calendar search more reliable" in texts


def test_current_new_preference_reranks_before_old_conflicting_preference() -> None:
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    old = RecordEnvelope.create(
        kind="memory",
        title="Old preference",
        summary="User: I prefer verbose explanations.",
        content={"text": "User: I prefer verbose explanations.", "occurred_at": "2026-01-01T00:00:00Z"},
        scope=scope,
        source="raw_chunk",
        meta={"memory_type": "raw_chunk"},
    )
    old.time.occurred_at = "2026-01-01T00:00:00Z"
    new = RecordEnvelope.create(
        kind="memory",
        title="Current preference",
        summary="User: I prefer concise explanations.",
        content={
            "text": "User: I prefer concise explanations.",
            "occurred_at": "2026-05-01T00:00:00Z",
            "current": True,
        },
        scope=scope,
        source="raw_chunk",
        meta={"memory_type": "raw_chunk"},
    )
    new.time.occurred_at = "2026-05-01T00:00:00Z"

    ranked = rerank_raw_results(
        query="What explanation style does the user prefer?",
        results=[
            {"record": old, "base_score": 1.0},
            {"record": new, "base_score": 1.0},
        ],
        task_context={"task_type": "chat.reply"},
    )

    assert ranked[0]["record"]["record_id"] == new.record_id
    assert ranked[0]["boosts"]["current_fact"] > ranked[1]["boosts"].get("current_fact", 0)
    assert ranked[0]["boosts"]["temporal_currentness"] > ranked[1]["boosts"].get("temporal_currentness", 0)
