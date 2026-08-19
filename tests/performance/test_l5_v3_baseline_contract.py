from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from benchmarks.l5_v3_baseline import (
    BenchmarkIsolationError,
    compare_against_profile,
    run_baseline,
)


pytestmark = pytest.mark.slow


def _all_metrics(report: dict) -> list[dict]:
    tier = report["tiers"]["small"]
    metrics = list(tier["warm"].values())
    metrics.extend(tier["runtime_cold"].values())
    metrics.extend(tier["legacy_migration"].values())
    return metrics


def test_small_baseline_isolated_and_semantically_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    production_root = tmp_path / "production-runtime"
    production_root.mkdir()
    sentinel = production_root / "sentinel.txt"
    sentinel.write_text("do-not-touch", encoding="utf-8")
    monkeypatch.setenv("EIMEMORY_ROOT", str(production_root))

    output_dir = tmp_path / "benchmark-output"
    report = run_baseline(
        output_dir=output_dir,
        tiers=("small",),
        samples=2,
        warmup=0,
    )

    assert report["schema_version"] == "l5_v3_baseline.v1"
    assert report["execution"]["isolated_state"] is True
    assert report["execution"]["production_mutated"] is False
    assert report["context"]["candidate_source"] == "sqlite_forced"
    assert report["context"]["environment_context_only"] is True
    assert report["context"]["os_cache_not_controlled"] is True
    assert sentinel.read_text(encoding="utf-8") == "do-not-touch"
    assert sorted(path.name for path in production_root.iterdir()) == ["sentinel.txt"]

    tier = report["tiers"]["small"]
    assert tier["fixture"]["spec"]["memory_records"] == 256
    assert tier["fixture"]["current_v3_storage_owner"] is False
    assert set(tier["warm"]) == {
        "candidate_recall",
        "append",
        "atomic_mutation",
        "readiness_no_release",
        "capability_ledger",
        "capability_replay_pack",
        "adapter_prefetch",
        "adapter_status",
    }
    assert set(tier["runtime_cold"]) == {"runtime_cold_startup"}
    assert set(tier["legacy_migration"]) == {"legacy_startup", "legacy_migration_batch"}
    assert tier["runtime_cold"]["runtime_cold_startup"]["os_cache_not_controlled"] is True
    replay_summary = tier["warm"]["capability_replay_pack"]["semantic_summary"]
    assert replay_summary["ok"] is True
    assert replay_summary["packs"]
    assert len(replay_summary["packs"][0]["cases"]) == 3
    adapter_prefetch_summary = tier["warm"]["adapter_prefetch"]["semantic_summary"]
    assert set(adapter_prefetch_summary) == {"codex", "hermes", "openclaw"}
    assert all(item["ok"] is True for item in adapter_prefetch_summary.values())
    assert adapter_prefetch_summary["openclaw"]["items"]
    adapter_status_summary = tier["warm"]["adapter_status"]["semantic_summary"]
    assert all(item["ok"] is True for item in adapter_status_summary.values())

    for metrics in _all_metrics(report):
        assert metrics["sample_count"] == 2
        assert metrics["p50_ms"] >= 0
        assert metrics["p95_ms"] >= metrics["p50_ms"]
        assert metrics["p99_ms"] >= metrics["p95_ms"]
        assert len(metrics["median_bootstrap_95pct_ms"]) == 2
        assert metrics["semantic_digest"]
        assert "semantic_summary" in metrics
        assert metrics["semantic_parity"] == {"ok": True, "unique_digest_count": 1}

    profile = report["budget_profile"]
    assert profile["schema_version"] == "l5_v3_budget_profile.v1"
    assert profile["policy"]["environment_is_context_only"] is True
    assert profile["tiers"]["small"]["workload_digest"] == tier["workload_digest"]
    assert Path(report["report_path"]).is_file()
    written = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
    assert written["report_digest"] == report["report_digest"]


def test_production_root_is_rejected_before_any_benchmark_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    production_root = tmp_path / "production-runtime"
    monkeypatch.setenv("EIMEMORY_ROOT", str(production_root))

    with pytest.raises(BenchmarkIsolationError):
        run_baseline(output_dir=production_root, tiers=("small",), samples=1, warmup=0)


def test_profile_marks_workload_changes_incomparable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "production-runtime"))
    report = run_baseline(
        output_dir=None,
        tiers=("small",),
        samples=1,
        warmup=0,
    )
    assert report["report_path"] is None
    candidate = copy.deepcopy(report)
    candidate["tiers"]["small"]["workload_digest"] = "different-workload"

    comparison = compare_against_profile(report["budget_profile"], candidate)

    assert comparison["status"] == "incomparable"
    assert comparison["decisions"] == [
        {"tier": "small", "status": "incomparable", "reason": "workload_digest_mismatch"}
    ]

    semantic_candidate = copy.deepcopy(report)
    semantic_candidate["tiers"]["small"]["warm"]["candidate_recall"]["semantic_digest"] = "different-semantics"
    semantic_comparison = compare_against_profile(report["budget_profile"], semantic_candidate)

    assert semantic_comparison["status"] == "semantic_mismatch"
    assert {
        "tier": "small",
        "operation": "candidate_recall",
        "status": "semantic_mismatch",
        "reason": "semantic_digest_mismatch",
    } in semantic_comparison["decisions"]
