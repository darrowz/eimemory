from __future__ import annotations

from eimemory.embeddings.local import _embed_text_cached, embed_text


def test_embed_text_uses_lru_cache_and_returns_copy() -> None:
    _embed_text_cached.cache_clear()

    first = embed_text("repeatable recall query")
    first[0] = 999.0
    second = embed_text("repeatable recall query")
    info = _embed_text_cached.cache_info()

    assert info.hits == 1
    assert info.misses == 1
    assert second[0] != 999.0


def test_embed_text_cache_is_partitioned_by_vector_size() -> None:
    _embed_text_cached.cache_clear()

    assert len(embed_text("same query", size=16)) == 16
    assert len(embed_text("same query", size=32)) == 32
    info = _embed_text_cached.cache_info()

    assert info.misses == 2


def test_embed_text_does_not_retain_oversized_documents_in_process_cache() -> None:
    _embed_text_cached.cache_clear()
    document = "large-memory-document " * 500

    embed_text(document)
    embed_text(document)

    info = _embed_text_cached.cache_info()
    assert info.currsize == 0
    assert info.misses == 0
