"""Bounds and isolation checks for external benchmark adapters (SEC-1)."""
from __future__ import annotations

from typing import Any

MAX_BENCHMARK_CASES = 5000
MAX_BENCHMARK_CHUNKS_PER_CASE = 500
MAX_BENCHMARK_CHUNK_CHARS = 65_536
MAX_BENCHMARK_SEED_RECORDS = 5000
MAX_BENCHMARK_SEED_TEXT_CHARS = 65_536


class BenchmarkIsolationError(ValueError):
    """Raised when a benchmark would write into a non-isolated runtime."""


def assert_isolated_benchmark_runtime(runtime: Any, *, adapter: str) -> None:
    """Refuse ingest into a runtime that looks like production state.

    public_benchmarks already uses a temp Runtime. Direct API callers must
    either pass an ephemeral root or set EIMEMORY_ALLOW_BENCHMARK_ON_RUNTIME=1
    for intentional lab harnesses.
    """
    import os
    from pathlib import Path

    if os.environ.get("EIMEMORY_ALLOW_BENCHMARK_ON_RUNTIME", "0") == "1":
        return
    root = Path(getattr(getattr(runtime, "store", None), "root", "") or "")
    marker = root / ".eimemory_benchmark_isolated"
    if marker.is_file():
        return
    # Temp/pytest paths are treated as isolated.
    text = str(root).replace("\\", "/").lower()
    if any(token in text for token in ("/tmp/", "/pytest-", "/temp/", "\\temp\\", "/var/folders/")):
        return
    raise BenchmarkIsolationError(
        f"{adapter}_requires_isolated_runtime"
    )


def mark_runtime_benchmark_isolated(runtime: Any) -> None:
    from pathlib import Path

    root = Path(getattr(getattr(runtime, "store", None), "root", "") or "")
    if not root:
        return
    marker = root / ".eimemory_benchmark_isolated"
    try:
        marker.write_text("1\n", encoding="utf-8")
    except OSError:
        pass


def enforce_case_budget(cases: list[Any], *, adapter: str) -> None:
    if len(cases) > MAX_BENCHMARK_CASES:
        raise ValueError(f"{adapter}_case_count_exceeds_{MAX_BENCHMARK_CASES}")


def enforce_chunk_budget(chunks: list[Any], *, adapter: str, case_id: str = "") -> None:
    if len(chunks) > MAX_BENCHMARK_CHUNKS_PER_CASE:
        raise ValueError(f"{adapter}_chunk_count_exceeds_{MAX_BENCHMARK_CHUNKS_PER_CASE}")
    for index, chunk in enumerate(chunks):
        text = ""
        if isinstance(chunk, dict):
            text = str(chunk.get("text") or chunk.get("raw_text") or "")
        elif isinstance(chunk, str):
            text = chunk
        if len(text) > MAX_BENCHMARK_CHUNK_CHARS:
            raise ValueError(
                f"{adapter}_chunk_text_exceeds_{MAX_BENCHMARK_CHUNK_CHARS}"
                + (f":{case_id or index}")
            )


def enforce_seed_budget(seed: list[Any], *, adapter: str) -> None:
    if len(seed) > MAX_BENCHMARK_SEED_RECORDS:
        raise ValueError(f"{adapter}_seed_count_exceeds_{MAX_BENCHMARK_SEED_RECORDS}")
    for index, item in enumerate(seed):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("title") or item.get("summary") or "")
        if len(text) > MAX_BENCHMARK_SEED_TEXT_CHARS:
            raise ValueError(f"{adapter}_seed_text_exceeds_{MAX_BENCHMARK_SEED_TEXT_CHARS}:{index}")
