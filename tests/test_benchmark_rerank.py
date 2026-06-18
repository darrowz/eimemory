from __future__ import annotations

from eimemory.raw.retrieval import _maybe_rerank_with_llm, _resolve_reranker


def test_llm_rerank_returns_ordered_items_without_diagnostics() -> None:
    ranked = [
        {"record": {"record_id": "first", "text": "less relevant"}, "final_score": 0.4},
        {"record": {"record_id": "second", "text": "more relevant"}, "final_score": 0.3},
    ]

    def fake_reranker(items, **_kwargs):
        return ["second", "first"]

    reordered = _maybe_rerank_with_llm(
        ranked=ranked,
        query="target",
        task_context={"llm_reranker": fake_reranker},
        limit=2,
        diagnostics=False,
    )

    assert [item["record"]["record_id"] for item in reordered] == ["second", "first"]


def test_llm_rerank_requires_explicit_external_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("EIMEMORY_RAW_RERANK_ENDPOINT", raising=False)
    monkeypatch.delenv("EIMEMORY_RAW_RETRIEVAL_RERANK_PROVIDER", raising=False)

    assert _resolve_reranker(task_context={"rerank_with_llm": True}) is None

    monkeypatch.setenv("EIMEMORY_RAW_RERANK_ENDPOINT", "https://rerank.example.test")
    assert _resolve_reranker(task_context={"rerank_with_llm": True}) is not None
