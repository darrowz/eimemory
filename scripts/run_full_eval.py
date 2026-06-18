"""Full eval v3: 32-way parallel via multiprocessing.Pool.

Each worker process gets its own Runtime instance (its own tmp dir) and
its own chunk of cases. Workers run in parallel, all 32 cores used.
Main process loads JSON once, distributes, aggregates.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("EIMEMORY_DATA_DIR") or _REPO_ROOT / "data")
LOG = os.environ.get("EIMEMORY_EVAL_LOG", "/tmp/full_eval.log")
N_WORKERS = int(os.environ.get("EIMEMORY_WORKERS", "32"))


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


def _run_chunk_lme(args):
    """Worker: run a slice of LME cases on its own Runtime."""
    import time as _t
    chunk, chunk_id, n_total = args
    t0 = _t.time()
    try:
        # Local imports to avoid pickling issues
        from eimemory.api.runtime import Runtime
        from eimemory.evaluation.longmemeval import run_longmemeval
        tmp = Path(tempfile.mkdtemp(prefix=f"eim-lme-{chunk_id}-"))
        runtime = Runtime.create(root=tmp)
        try:
            ds = {"name": "longmemeval-s-cleaned", "schema_version": 1,
                  "scope": chunk["scope"], "cases": chunk["cases"]}
            report = run_longmemeval(runtime, ds, mode="raw",
                                     granularity="session", limit=len(chunk["cases"]))
            return {"ok": True, "chunk_id": chunk_id, "n": len(chunk["cases"]),
                    "report": report, "elapsed": _t.time() - t0}
        finally:
            runtime.close()
    except Exception as e:
        return {"ok": False, "chunk_id": chunk_id, "n": len(chunk["cases"]),
                "error": f"{type(e).__name__}: {e}", "elapsed": _t.time() - t0}


def _run_chunk_loc(args):
    """Worker: run a slice of LoCoMo cases on its own Runtime."""
    import time as _t
    chunk, chunk_id, n_total = args
    t0 = _t.time()
    try:
        from eimemory.api.runtime import Runtime
        from eimemory.evaluation.locomo import run_locomo
        tmp = Path(tempfile.mkdtemp(prefix=f"eim-loc-{chunk_id}-"))
        runtime = Runtime.create(root=tmp)
        try:
            ds = {"name": "locomo10-full", "schema_version": 1,
                  "scope": chunk["scope"], "cases": chunk["cases"]}
            report = run_locomo(runtime, ds, mode="raw",
                                granularity="turn", limit=len(chunk["cases"]))
            return {"ok": True, "chunk_id": chunk_id, "n": len(chunk["cases"]),
                    "report": report, "elapsed": _t.time() - t0}
        finally:
            runtime.close()
    except Exception as e:
        return {"ok": False, "chunk_id": chunk_id, "n": len(chunk["cases"]),
                "error": f"{type(e).__name__}: {e}", "elapsed": _t.time() - t0}


def split_into_chunks(cases: list, n_workers: int) -> list[tuple[dict, int, int]]:
    """Split cases into roughly equal chunks, return (chunk, id, total) tuples."""
    n = len(cases)
    chunk_size = max(1, (n + n_workers - 1) // n_workers)
    chunks = []
    for i in range(0, n, chunk_size):
        chunk_cases = cases[i:i+chunk_size]
        chunks.append(({"scope": None, "cases": chunk_cases}, len(chunks), n))
    return chunks


def split_into_chunks_with_scope(cases: list, scope: dict, n_workers: int) -> list[tuple[dict, int, int]]:
    n = len(cases)
    chunk_size = max(1, (n + n_workers - 1) // n_workers)
    chunks = []
    for i in range(0, n, chunk_size):
        chunk_cases = cases[i:i+chunk_size]
        chunks.append(({"scope": scope, "cases": chunk_cases}, len(chunks), n))
    return chunks


def aggregate_lme_reports(results: list[dict]) -> dict:
    """Aggregate per-worker LME reports into one final report."""
    from eimemory.evaluation.metrics import mean_reciprocal_rank
    all_samples = []
    all_ranks = []
    all_latencies = []
    total_n = 0
    failed = 0
    for r in results:
        if not r["ok"]:
            failed += 1
            log(f"  [LME] chunk {r['chunk_id']} FAILED: {r.get('error')}")
            continue
        rep = r["report"]
        all_samples.extend(rep["samples"])
        for s in rep["samples"]:
            all_ranks.append(int(s["rank"]))
            all_latencies.append(float(s["latency_ms"]))
        total_n += r["n"]
        log(f"  [LME] chunk {r['chunk_id']} OK n={r['n']} R@1={rep['retrieval_recall_at_1']} R@5={rep['retrieval_recall_at_5']} ({r['elapsed']:.0f}s)")

    n = len(all_samples)
    if n == 0:
        return {"ok": False, "error": "no successful chunks", "chunks_failed": failed}

    def avg(k):
        return round(sum(s.get(k, 0.0) for s in all_samples) / n, 3)
    from statistics import mean as _mean
    def pct(xs, p):
        if not xs: return 0.0
        s = sorted(xs)
        k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
        return round(s[k], 3)

    return {
        "ok": True,
        "report_type": "longmemeval_eval_aggregated",
        "sample_count": n,
        "chunks_failed": failed,
        "retrieval_recall_at_1": avg("retrieval_recall_at_1"),
        "retrieval_recall_at_5": avg("retrieval_recall_at_5"),
        "retrieval_recall_at_10": avg("retrieval_recall_at_10"),
        "recall_any_at_5": avg("recall_any_at_5"),
        "recall_all_at_5": avg("recall_all_at_5"),
        "ndcg_at_5": avg("ndcg_at_5"),
        "mrr": round(mean_reciprocal_rank(all_ranks), 4),
        "latency_ms_avg": round(_mean(all_latencies), 3) if all_latencies else 0.0,
        "latency_ms_p95": pct(all_latencies, 95),
    }


def aggregate_loc_reports(results: list[dict]) -> dict:
    """Aggregate per-worker LoCoMo reports into one final report."""
    from eimemory.evaluation.metrics import mean_reciprocal_rank
    all_samples = []
    all_ranks = []
    all_latencies = []
    failed = 0
    for r in results:
        if not r["ok"]:
            failed += 1
            log(f"  [LoCoMo] chunk {r['chunk_id']} FAILED: {r.get('error')}")
            continue
        rep = r["report"]
        all_samples.extend(rep["samples"])
        for s in rep["samples"]:
            all_ranks.append(int(s["rank"]))
            all_latencies.append(float(s["latency_ms"]))
        log(f"  [LoCoMo] chunk {r['chunk_id']} OK n={r['n']} R@1={rep['recall_at_1']} R@5={rep['recall_at_5']} ({r['elapsed']:.0f}s)")

    n = len(all_samples)
    if n == 0:
        return {"ok": False, "error": "no successful chunks", "chunks_failed": failed}

    def avg(k):
        return round(sum(s.get(k, 0.0) for s in all_samples) / n, 3)
    from statistics import mean as _mean
    def pct(xs, p):
        if not xs: return 0.0
        s = sorted(xs)
        k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
        return round(s[k], 3)

    failures = [s for s in all_samples if not s["rank"]]

    return {
        "ok": True,
        "report_type": "locomo_eval_aggregated",
        "sample_count": n,
        "chunks_failed": failed,
        "recall_at_1": avg("recall_at_1"),
        "recall_at_5": avg("recall_at_5"),
        "recall_at_10": avg("recall_at_10"),
        "recall_any_at_5": avg("recall_any_at_5"),
        "ndcg_at_5": avg("ndcg_at_5"),
        "mrr": round(mean_reciprocal_rank(all_ranks), 4),
        "latency_ms_avg": round(_mean(all_latencies), 3) if all_latencies else 0.0,
        "latency_ms_p95": pct(all_latencies, 95),
        "failure_count": len(failures),
    }


def main() -> int:
    try:
        with open(LOG, "w", encoding="utf-8") as f:
            f.write(f"# full eval v3 parallel started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except Exception:
        pass
    log(f"DATA dir: {DATA}, N_WORKERS={N_WORKERS}, Python={sys.version.split()[0]}")
    log(f"CPU count: {mp.cpu_count()}")

    # === LME ===
    lme_path = DATA / "longmemeval_s_eimemory.json"
    log(f"LME raw: {lme_path} ({lme_path.stat().st_size/1024/1024:.1f} MB)")
    lme = json.loads(lme_path.read_text(encoding="utf-8"))
    lme_cases = lme["cases"]
    lme_scope = lme.get("scope")
    n_lme = len(lme_cases)
    log(f"LME cases: {n_lme}, splitting into chunks of ~{max(1, n_lme // N_WORKERS)}")
    chunks_lme = split_into_chunks_with_scope(lme_cases, lme_scope, N_WORKERS)
    log(f"LME chunks: {len(chunks_lme)}")

    t0 = time.time()
    with mp.Pool(processes=N_WORKERS, initializer=_worker_init_worker) as pool:
        lme_results = pool.map(_run_chunk_lme, chunks_lme)
    log(f"LME wall time: {time.time()-t0:.0f}s")
    lme_agg = aggregate_lme_reports(lme_results)
    log(f"LME AGG: {json.dumps(lme_agg, ensure_ascii=False)}")

    # === LoCoMo ===
    loc_path = DATA / "locomo10_eimemory.json"
    log(f"LoCoMo raw: {loc_path} ({loc_path.stat().st_size/1024/1024:.1f} MB)")
    loc = json.loads(loc_path.read_text(encoding="utf-8"))
    loc_cases = loc["cases"]
    loc_scope = loc.get("scope")
    n_loc = len(loc_cases)
    log(f"LoCoMo cases: {n_loc}, splitting into chunks of ~{max(1, n_loc // N_WORKERS)}")
    chunks_loc = split_into_chunks_with_scope(loc_cases, loc_scope, N_WORKERS)
    log(f"LoCoMo chunks: {len(chunks_loc)}")

    t0 = time.time()
    with mp.Pool(processes=N_WORKERS, initializer=_worker_init_worker) as pool:
        loc_results = pool.map(_run_chunk_loc, chunks_loc)
    log(f"LoCoMo wall time: {time.time()-t0:.0f}s")
    loc_agg = aggregate_loc_reports(loc_results)
    log(f"LoCoMo AGG: {json.dumps(loc_agg, ensure_ascii=False)}")

    log("=== ALL DONE ===")
    log(f"LME:    R@1={lme_agg.get('retrieval_recall_at_1')} R@5={lme_agg.get('retrieval_recall_at_5')} R@10={lme_agg.get('retrieval_recall_at_10')} MRR={lme_agg.get('mrr'):.4f} NDCG@5={lme_agg.get('ndcg_at_5'):.4f} latency_avg={lme_agg.get('latency_ms_avg'):.1f}ms p95={lme_agg.get('latency_ms_p95'):.1f}ms")
    log(f"LoCoMo: R@1={loc_agg.get('recall_at_1')} R@5={loc_agg.get('recall_at_5')} R@10={loc_agg.get('recall_at_10')} MRR={loc_agg.get('mrr'):.4f} NDCG@5={loc_agg.get('ndcg_at_5'):.4f} latency_avg={loc_agg.get('latency_ms_avg'):.1f}ms p95={loc_agg.get('latency_ms_p95'):.1f}ms")

    # Save final aggregated report to /tmp
    final = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_workers": N_WORKERS,
        "lme": lme_agg,
        "locomo": loc_agg,
    }
    try:
        with open("/tmp/full_eval_report.json", "w", encoding="utf-8") as f:
            json.dump(final, f, ensure_ascii=False, indent=2)
        log(f"Final report saved to /tmp/full_eval_report.json")
    except Exception as e:
        log(f"Failed to save report: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
