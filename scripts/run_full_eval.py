"""Full benchmark runner used by the phase-5 eval pipeline."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("EIMEMORY_DATA_DIR") or _REPO_ROOT / "data")
LOG = os.environ.get("EIMEMORY_EVAL_LOG", "/tmp/full_eval.log")

DEFAULT_WORKERS = 32
DEFAULT_GRANULARITY = "turn"
DEFAULT_RERANKER = "auto"
ALLOWED_GRANULARITY = {"session", "turn", "chunk"}
ALLOWED_RERANKER = {"deterministic", "llm", "auto"}


def _env_value(name: str, environ: dict[str, str] | None = None) -> str | None:
    if environ is None:
        return os.environ.get(name)
    return environ.get(name)


def _as_int(value: str | None, default: int, *, min_value: int | None = None) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if min_value is not None and parsed < min_value:
        return default
    return parsed


def _as_optional_int(value: str | None, default: int | None) -> int | None:
    if value is None:
        return default
    normalized = str(value).strip()
    if not normalized:
        return default
    try:
        parsed = int(normalized)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _as_choice(value: str | None, choices: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in choices else default


def load_eval_config(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Resolve benchmark knobs from environment variables."""
    return {
        "n_workers": _as_int(_env_value("EIMEMORY_WORKERS", environ), DEFAULT_WORKERS, min_value=1),
        "lme_granularity": _as_choice(
            _env_value("EIMEMORY_LME_GRANULARITY", environ),
            ALLOWED_GRANULARITY,
            DEFAULT_GRANULARITY,
        ),
        "locomo_granularity": "turn",
        "lme_limit": _as_optional_int(_env_value("EIMEMORY_LME_LIMIT", environ), None),
        "locomo_limit": _as_optional_int(_env_value("EIMEMORY_LOCOMO_LIMIT", environ), None),
        "reranker": _as_choice(
            _env_value("EIMEMORY_RERANKER", environ),
            ALLOWED_RERANKER,
            DEFAULT_RERANKER,
        ),
    }


def apply_worker_reranker_env(reranker: str) -> None:
    """Map full-eval reranker mode to the raw retrieval worker switch."""
    mode = _as_choice(reranker, ALLOWED_RERANKER, DEFAULT_RERANKER)
    if mode == "llm":
        os.environ["EIMEMORY_RAW_RETRIEVAL_RERANK"] = "1"
        os.environ["EIMEMORY_RAW_RETRIEVAL_RERANK_ENABLED"] = "1"
        return
    os.environ["EIMEMORY_RAW_RETRIEVAL_RERANK"] = "0"
    os.environ["EIMEMORY_RAW_RETRIEVAL_RERANK_ENABLED"] = "0"


def _effective_limit(requested: int | None, case_count: int, chunk_count: int) -> int:
    if requested is not None:
        return requested
    if case_count <= 0:
        return 0
    chunk_count = max(1, int(chunk_count or 1))
    return max(1, (int(case_count) + chunk_count - 1) // chunk_count)


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _worker_init_worker():
    """Suppress noisy child-process logs and stay quiet unless error."""
    import logging

    logging.basicConfig(level=logging.WARNING)


def _chunk_limit(requested: int | None, chunk_size: int) -> int:
    return chunk_size if requested is None else requested


def _run_chunk_lme(args):
    """Worker: run a slice of LME cases on its own Runtime."""
    import time as _t

    chunk, chunk_id, n_total, granularity, limit, reranker = args
    t0 = _t.time()
    try:
        from eimemory.api.runtime import Runtime
        from eimemory.evaluation.longmemeval import run_longmemeval

        apply_worker_reranker_env(reranker)
        tmp = Path(tempfile.mkdtemp(prefix=f"eim-lme-{chunk_id}-"))
        runtime = Runtime.create(root=tmp)
        try:
            ds = {
                "name": "longmemeval-s-cleaned",
                "schema_version": 1,
                "scope": chunk["scope"],
                "cases": chunk["cases"],
            }
            resolved_limit = _chunk_limit(limit, len(chunk["cases"]))
            report = run_longmemeval(
                runtime,
                ds,
                mode="raw",
                granularity=granularity,
                limit=resolved_limit,
            )
            return {
                "ok": True,
                "chunk_id": chunk_id,
                "n": len(chunk["cases"]),
                "report": report,
                "elapsed": _t.time() - t0,
                "limit": resolved_limit,
                "granularity": granularity,
                "reranker": reranker,
            }
        finally:
            runtime.close()
    except Exception as e:
        return {
            "ok": False,
            "chunk_id": chunk_id,
            "n": len(chunk["cases"]),
            "error": f"{type(e).__name__}: {e}",
            "elapsed": _t.time() - t0,
            "limit": _chunk_limit(limit, len(chunk["cases"])),
        }


def _run_chunk_loc(args):
    """Worker: run a slice of LoCoMo cases on its own Runtime."""
    import time as _t

    chunk, chunk_id, n_total, granularity, limit, reranker = args
    t0 = _t.time()
    try:
        from eimemory.api.runtime import Runtime
        from eimemory.evaluation.locomo import run_locomo

        apply_worker_reranker_env(reranker)
        tmp = Path(tempfile.mkdtemp(prefix=f"eim-loc-{chunk_id}-"))
        runtime = Runtime.create(root=tmp)
        try:
            ds = {"name": "locomo10-full", "schema_version": 1, "scope": chunk["scope"], "cases": chunk["cases"]}
            resolved_limit = _chunk_limit(limit, len(chunk["cases"]))
            report = run_locomo(
                runtime,
                ds,
                mode="raw",
                granularity=granularity,
                limit=resolved_limit,
            )
            return {
                "ok": True,
                "chunk_id": chunk_id,
                "n": len(chunk["cases"]),
                "report": report,
                "elapsed": _t.time() - t0,
                "limit": resolved_limit,
                "granularity": granularity,
                "reranker": reranker,
            }
        finally:
            runtime.close()
    except Exception as e:
        return {
            "ok": False,
            "chunk_id": chunk_id,
            "n": len(chunk["cases"]),
            "error": f"{type(e).__name__}: {e}",
            "elapsed": _t.time() - t0,
            "limit": _chunk_limit(limit, len(chunk["cases"])),
        }


def split_into_chunks(cases: list, n_workers: int) -> list[tuple[dict, int, int]]:
    """Split cases into roughly equal chunks, return (chunk, id, total) tuples."""
    n = len(cases)
    chunk_size = max(1, (n + n_workers - 1) // n_workers)
    chunks = []
    for i in range(0, n, chunk_size):
        chunk_cases = cases[i : i + chunk_size]
        chunks.append(({"scope": None, "cases": chunk_cases}, len(chunks), n))
    return chunks


def split_into_chunks_with_scope(cases: list, scope: dict, n_workers: int) -> list[tuple[dict, int, int]]:
    n = len(cases)
    chunk_size = max(1, (n + n_workers - 1) // n_workers)
    chunks = []
    for i in range(0, n, chunk_size):
        chunk_cases = cases[i : i + chunk_size]
        chunks.append(({"scope": scope, "cases": chunk_cases}, len(chunks), n))
    return chunks


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def _scorecard(label: str, report: dict, *, metric_names: list[str]) -> str:
    return f"{label}: " + ", ".join(
        f"{name}={report.get(name)}" for name in metric_names if report.get(name) is not None
    )


def aggregate_lme_reports(
    results: list[dict],
    *,
    granularity: str = "turn",
    worker_count: int = 1,
    limit: int | None = None,
    reranker: str = "auto",
) -> dict:
    """Aggregate per-worker LME reports into one final report."""
    from eimemory.evaluation.metrics import mean_reciprocal_rank

    all_samples = []
    all_ranks: list[int] = []
    all_latencies = []
    failed = 0

    for r in results:
        if not r.get("ok"):
            failed += 1
            log(f"  [LME] chunk {r['chunk_id']} FAILED: {r.get('error')}")
            continue
        report = r.get("report") or {}
        samples = list(report.get("samples") or [])
        all_samples.extend(samples)
        for sample in samples:
            all_ranks.append(_safe_int(sample.get("rank"), default=0))
            latency_ms = _safe_float(sample.get("latency_ms"))
            if latency_ms is not None:
                all_latencies.append(latency_ms)
        log(
            f"  [LME] chunk {r['chunk_id']} OK n={r['n']} "
            f"R@1={report.get('retrieval_recall_at_1')} "
            f"R@5={report.get('retrieval_recall_at_5')} "
            f"({r['elapsed']:.0f}s)"
        )

    n = len(all_samples)
    if n == 0:
        return {"ok": False, "error": "no successful chunks", "chunks_failed": failed}

    def avg(metric: str) -> float:
        return round(sum(sample.get(metric, 0.0) for sample in all_samples) / n, 3)

    from statistics import mean as _mean

    effective_limit = limit if limit is not None else n
    return {
        "ok": True,
        "report_type": "longmemeval_eval_aggregated",
        "sample_count": n,
        "chunks_failed": failed,
        "chunks_processed": len(results) - failed,
        "granularity": granularity,
        "limit": effective_limit,
        "worker_count": worker_count,
        "reranker": reranker,
        "retrieval_recall_at_1": avg("retrieval_recall_at_1"),
        "retrieval_recall_at_5": avg("retrieval_recall_at_5"),
        "retrieval_recall_at_10": avg("retrieval_recall_at_10"),
        "recall_any_at_5": avg("recall_any_at_5"),
        "recall_all_at_5": avg("recall_all_at_5"),
        "ndcg_at_5": avg("ndcg_at_5"),
        "mrr": round(mean_reciprocal_rank(all_ranks), 4),
        "latency_ms_avg": round(_mean(all_latencies), 3) if all_latencies else 0.0,
        "latency_ms_p95": _pct(all_latencies, 95),
    }


def aggregate_loc_reports(
    results: list[dict],
    *,
    granularity: str = "turn",
    worker_count: int = 1,
    limit: int | None = None,
    reranker: str = "auto",
) -> dict:
    """Aggregate per-worker LoCoMo reports into one final report."""
    from eimemory.evaluation.metrics import mean_reciprocal_rank

    all_samples = []
    all_ranks: list[int] = []
    all_latencies = []
    failed = 0

    for r in results:
        if not r.get("ok"):
            failed += 1
            log(f"  [LoCoMo] chunk {r['chunk_id']} FAILED: {r.get('error')}")
            continue
        report = r.get("report") or {}
        samples = list(report.get("samples") or [])
        all_samples.extend(samples)
        for sample in samples:
            all_ranks.append(_safe_int(sample.get("rank"), default=0))
            latency_ms = _safe_float(sample.get("latency_ms"))
            if latency_ms is not None:
                all_latencies.append(latency_ms)
        log(
            f"  [LoCoMo] chunk {r['chunk_id']} OK n={r['n']} "
            f"R@1={report.get('recall_at_1')} "
            f"R@5={report.get('recall_at_5')} "
            f"({r['elapsed']:.0f}s)"
        )

    n = len(all_samples)
    if n == 0:
        return {"ok": False, "error": "no successful chunks", "chunks_failed": failed}

    def avg(metric: str) -> float:
        return round(sum(sample.get(metric, 0.0) for sample in all_samples) / n, 3)

    from statistics import mean as _mean
    failures = [sample for sample in all_samples if not _safe_int(sample.get("rank"), default=0)]
    effective_limit = limit if limit is not None else n
    return {
        "ok": True,
        "report_type": "locomo_eval_aggregated",
        "sample_count": n,
        "chunks_failed": failed,
        "chunks_processed": len(results) - failed,
        "granularity": granularity,
        "limit": effective_limit,
        "worker_count": worker_count,
        "reranker": reranker,
        "recall_at_1": avg("recall_at_1"),
        "recall_at_5": avg("recall_at_5"),
        "recall_at_10": avg("recall_at_10"),
        "recall_any_at_5": avg("recall_any_at_5"),
        "ndcg_at_5": avg("ndcg_at_5"),
        "mrr": round(mean_reciprocal_rank(all_ranks), 4),
        "latency_ms_avg": round(_mean(all_latencies), 3) if all_latencies else 0.0,
        "latency_ms_p95": _pct(all_latencies, 95),
        "failure_count": len(failures),
    }


def build_full_eval_report(
    *,
    lme_agg: dict,
    loc_agg: dict,
    config: dict[str, Any],
    lme_cases: int,
    locomo_cases: int,
    lme_chunk_count: int,
    locomo_chunk_count: int,
    lme_name: str,
    locomo_name: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    lme_limit = _effective_limit(config["lme_limit"], lme_cases, lme_chunk_count)
    locomo_limit = _effective_limit(config["locomo_limit"], locomo_cases, locomo_chunk_count)
    return {
        "generated_at": generated_at or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_workers": config["n_workers"],
        "worker_count": config["n_workers"],
        "reranker": config["reranker"],
        "candidate_metadata": {
            "lme": {
                "dataset_name": lme_name,
                "dataset_case_count": lme_cases,
                "chunk_count": lme_chunk_count,
                "requested_limit": config["lme_limit"],
                "granularity": config["lme_granularity"],
            },
            "locomo": {
                "dataset_name": locomo_name,
                "dataset_case_count": locomo_cases,
                "chunk_count": locomo_chunk_count,
                "requested_limit": config["locomo_limit"],
                "granularity": config["locomo_granularity"],
            },
        },
        "lme": {
            **lme_agg,
            "granularity": config["lme_granularity"],
            "limit": lme_limit,
            "worker_count": config["n_workers"],
            "reranker": config["reranker"],
        },
        "locomo": {
            **loc_agg,
            "granularity": config["locomo_granularity"],
            "limit": locomo_limit,
            "worker_count": config["n_workers"],
            "reranker": config["reranker"],
        },
    }


def main() -> int:
    config = load_eval_config()
    lme_granularity = config["lme_granularity"]
    locomo_granularity = config["locomo_granularity"]

    try:
        with open(LOG, "w", encoding="utf-8") as f:
            f.write(f"# full eval v3 parallel started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except Exception:
        pass

    log(
        f"DATA dir: {DATA}, "
        f"N_WORKERS={config['n_workers']}, "
        f"LME_granularity={lme_granularity}, "
        f"LoCoMo_granularity={locomo_granularity}, "
        f"reranker={config['reranker']}, "
        f"Python={sys.version.split()[0]}"
    )
    log(f"CPU count: {mp.cpu_count()}")

    # === LME ===
    lme_path = DATA / "longmemeval_s_eimemory.json"
    log(f"LME raw: {lme_path} ({lme_path.stat().st_size/1024/1024:.1f} MB)")
    lme = json.loads(lme_path.read_text(encoding="utf-8"))
    lme_cases = lme["cases"]
    lme_scope = lme.get("scope")
    n_lme = len(lme_cases)
    chunks_lme = split_into_chunks_with_scope(lme_cases, lme_scope, config["n_workers"])
    log(f"LME cases: {n_lme}, splitting into chunks of ~{max(1, n_lme // config['n_workers'])}")
    log(f"LME chunks: {len(chunks_lme)}")
    lme_payload = [
        (chunk, chunk_id, total, lme_granularity, config["lme_limit"], config["reranker"])
        for chunk, chunk_id, total in chunks_lme
    ]

    t0 = time.time()
    with mp.Pool(processes=config["n_workers"], initializer=_worker_init_worker) as pool:
        lme_results = pool.map(_run_chunk_lme, lme_payload)
    log(f"LME wall time: {time.time()-t0:.0f}s")
    lme_agg = aggregate_lme_reports(
        lme_results,
        granularity=lme_granularity,
        worker_count=config["n_workers"],
        limit=config["lme_limit"],
        reranker=config["reranker"],
    )
    log(f"LME AGG: {json.dumps(lme_agg, ensure_ascii=False)}")

    # === LoCoMo ===
    loc_path = DATA / "locomo10_eimemory.json"
    log(f"LoCoMo raw: {loc_path} ({loc_path.stat().st_size/1024/1024:.1f} MB)")
    loc = json.loads(loc_path.read_text(encoding="utf-8"))
    loc_cases = loc["cases"]
    loc_scope = loc.get("scope")
    n_loc = len(loc_cases)
    chunks_loc = split_into_chunks_with_scope(loc_cases, loc_scope, config["n_workers"])
    log(f"LoCoMo cases: {n_loc}, splitting into chunks of ~{max(1, n_loc // config['n_workers'])}")
    log(f"LoCoMo chunks: {len(chunks_loc)}")
    loc_payload = [
        (chunk, chunk_id, total, locomo_granularity, config["locomo_limit"], config["reranker"])
        for chunk, chunk_id, total in chunks_loc
    ]

    t0 = time.time()
    with mp.Pool(processes=config["n_workers"], initializer=_worker_init_worker) as pool:
        loc_results = pool.map(_run_chunk_loc, loc_payload)
    log(f"LoCoMo wall time: {time.time()-t0:.0f}s")
    loc_agg = aggregate_loc_reports(
        loc_results,
        granularity=locomo_granularity,
        worker_count=config["n_workers"],
        limit=config["locomo_limit"],
        reranker=config["reranker"],
    )
    log(f"LoCoMo AGG: {json.dumps(loc_agg, ensure_ascii=False)}")

    log("=== ALL DONE ===")
    log(_scorecard("LME", lme_agg, metric_names=["retrieval_recall_at_1", "retrieval_recall_at_5", "retrieval_recall_at_10", "mrr", "ndcg_at_5", "latency_ms_avg", "latency_ms_p95"]))
    log(_scorecard("LoCoMo", loc_agg, metric_names=["recall_at_1", "recall_at_5", "recall_at_10", "mrr", "ndcg_at_5", "latency_ms_avg", "latency_ms_p95"]))

    final = build_full_eval_report(
        lme_agg=lme_agg,
        loc_agg=loc_agg,
        config=config,
        lme_cases=n_lme,
        locomo_cases=n_loc,
        lme_chunk_count=len(chunks_lme),
        locomo_chunk_count=len(chunks_loc),
        lme_name="longmemeval-s-cleaned",
        locomo_name="locomo10-full",
    )

    try:
        with open("/tmp/full_eval_report.json", "w", encoding="utf-8") as f:
            json.dump(final, f, ensure_ascii=False, indent=2)
        log("Final report saved to /tmp/full_eval_report.json")
    except Exception as exc:
        log(f"Failed to save report: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
