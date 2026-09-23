from __future__ import annotations

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation._benchmark_limits import (
    MAX_BENCHMARK_CASES,
    BenchmarkIsolationError,
)
from eimemory.evaluation.capability_catalog import (
    CapabilityEvaluationCatalog,
    CatalogResolutionError,
)
from eimemory.evaluation.longmemeval import normalize_longmemeval_dataset, run_longmemeval


def test_longmemeval_rejects_oversized_case_count() -> None:
    dataset = {
        "cases": [
            {"question": f"q{i}", "haystack_sessions": [], "answer": "a"}
            for i in range(MAX_BENCHMARK_CASES + 1)
        ]
    }
    with pytest.raises(ValueError, match="longmemeval_case_count_exceeds"):
        normalize_longmemeval_dataset(dataset)


def test_longmemeval_rejects_non_isolated_runtime(tmp_path, monkeypatch) -> None:
    # Force non-temp looking root by using a named path under workspace-like tree
    root = tmp_path / "prodlike_state"
    root.mkdir()
    runtime = Runtime.create(root=root)
    # Path may still contain /tmp/ — patch the checker to treat this root as production-like
    monkeypatch.setattr(
        "eimemory.evaluation.longmemeval.assert_isolated_benchmark_runtime",
        lambda runtime, *, adapter: (_ for _ in ()).throw(
            BenchmarkIsolationError(f"{adapter}_requires_isolated_runtime")
        ),
    )
    with pytest.raises(BenchmarkIsolationError, match="longmemeval_requires_isolated_runtime"):
        run_longmemeval(runtime, {"cases": [{"question": "q", "haystack_sessions": [], "answer": "a"}]})
    runtime.close()


def test_publish_into_refuses_sealed_destination() -> None:
    source = CapabilityEvaluationCatalog()
    destination = CapabilityEvaluationCatalog()
    destination.seal()
    with pytest.raises(CatalogResolutionError, match="capability_catalog_sealed"):
        source.publish_into(destination)
