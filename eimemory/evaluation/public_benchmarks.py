"""Public benchmark harnesses that run against isolated temporary state."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any

from eimemory.core.clock import now_iso


def run_public_memory_benchmark(
    dataset: dict | list,
    *,
    suite: str,
    mode: str = "raw",
    granularity: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    suite = str(suite or "").strip().lower()
    with tempfile.TemporaryDirectory(prefix=f"eimemory-{suite or 'benchmark'}-") as temp_root:
        from eimemory.api.runtime import Runtime

        runtime = Runtime.create(root=Path(temp_root))
        try:
            if suite in {"longmem", "longmemeval"}:
                from eimemory.evaluation.longmemeval import run_longmemeval

                report = run_longmemeval(
                    runtime,
                    dataset,
                    mode=mode,
                    granularity=granularity or "session",
                    limit=limit,
                    persist_report=False,
                )
                suite_name = "longmemeval"
            elif suite == "locomo":
                from eimemory.evaluation.locomo import run_locomo

                report = run_locomo(
                    runtime,
                    dataset,
                    mode=mode,
                    granularity=granularity or "turn",
                    limit=limit,
                )
                suite_name = "locomo"
            else:
                raise ValueError("suite must be one of: longmemeval, locomo")
        finally:
            runtime.close()
    return {
        "ok": bool(report.get("ok")),
        "schema_version": 1,
        "report_type": "public_memory_benchmark",
        "suite": suite_name,
        "generated_at": now_iso(),
        "isolated_state": True,
        "production_state_path": "/var/lib/eimemory/state/eimemory.sqlite",
        "metrics": _metrics_for_suite(report),
        "report": report,
    }


def _metrics_for_suite(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "r_at_1": report.get("recall_at_1", report.get("retrieval_recall_at_1", 0.0)),
        "r_at_5": report.get("recall_at_5", report.get("retrieval_recall_at_5", 0.0)),
        "mrr": report.get("mrr", 0.0),
        "ndcg_at_5": report.get("ndcg_at_5", 0.0),
        "latency_ms_avg": report.get("latency_ms_avg", 0.0),
        "latency_ms_p95": report.get("latency_ms_p95", 0.0),
        "failure_samples": report.get("failure_samples")
        or [sample for sample in list(report.get("samples") or []) if not sample.get("rank")][:20],
    }
