"""Full eval run: LME 500 + LoCoMo 1986 with progress logging.

Writes to stdout AND to EIMEMORY_EVAL_LOG (default /tmp/full_eval.log)
so the operator can tail progress while the script runs in background.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Same resolution as smoke test
_REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("EIMEMORY_DATA_DIR") or _REPO_ROOT / "data")
LOG = os.environ.get("EIMEMORY_EVAL_LOG", "/tmp/full_eval.log")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main() -> int:
    from eimemory.api.runtime import Runtime
    from eimemory.evaluation.longmemeval import run_longmemeval
    from eimemory.evaluation.locomo import run_locomo

    # Clear log
    try:
        with open(LOG, "w", encoding="utf-8") as f:
            f.write(f"# full eval log started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except Exception:
        pass

    log(f"DATA dir: {DATA}")
    log(f"Python: {sys.version.split()[0]}")

    tmp = Path(tempfile.mkdtemp(prefix="eimemory-eval-"))
    log(f"Runtime tmp: {tmp}")
    runtime = Runtime.create(root=tmp)
    try:
        # ===== LongMemEval =====
        log("=== LME: loading dataset ===")
        lme_path = DATA / "longmemeval_s_eimemory.json"
        log(f"LME raw file: {lme_path} ({lme_path.stat().st_size/1024/1024:.1f} MB)")
        lme = json.loads(lme_path.read_text(encoding="utf-8"))
        n_lme = len(lme["cases"])
        log(f"LME cases: {n_lme}")
        t0 = time.time()
        # report_every: hook into runner if it supports; otherwise we just wait.
        # run_longmemeval returns a report dict; we don't have built-in progress.
        # For visibility, run in slices of N cases and print R@k after each slice.
        slice_size = max(1, n_lme // 10)
        log(f"LME slicing: {slice_size} per slice, {(n_lme + slice_size - 1) // slice_size} slices")
        cumulative_lme = {"hit_at_1": 0, "hit_at_5": 0, "mrr_sum": 0.0, "ndcg_sum": 0.0}
        slices_done = 0
        for start in range(0, n_lme, slice_size):
            sub = {"name": lme.get("name"), "schema_version": lme.get("schema_version"),
                   "scope": lme.get("scope"), "cases": lme["cases"][start:start+slice_size]}
            r = run_longmemeval(runtime, sub, mode="raw", granularity="session", limit=len(sub["cases"]))
            slices_done += 1
            log(f"  LME slice {slices_done}/{(n_lme + slice_size - 1) // slice_size} "
                f"[{start}:{start+len(sub['cases'])}] "
                f"R@1={r.get('retrieval_recall_at_1')} R@5={r.get('retrieval_recall_at_5')} "
                f"MRR={r.get('mrr'):.4f} NDCG@5={r.get('ndcg_at_5'):.4f} "
                f"({time.time()-t0:.0f}s elapsed)")
        # Final aggregate from full run to be safe
        log("LME: running final aggregate over ALL cases")
        r_lme = run_longmemeval(runtime, lme, mode="raw", granularity="session", limit=n_lme)
        log(f"LME FULL: R@1={r_lme.get('retrieval_recall_at_1')} R@5={r_lme.get('retrieval_recall_at_5')} "
            f"MRR={r_lme.get('mrr'):.4f} NDCG@5={r_lme.get('ndcg_at_5'):.4f} "
            f"({time.time()-t0:.0f}s total, {n_lme} cases)")

        # ===== LoCoMo =====
        log("=== LoCoMo: loading dataset ===")
        loc_path = DATA / "locomo10_eimemory.json"
        log(f"LoCoMo raw file: {loc_path} ({loc_path.stat().st_size/1024/1024:.1f} MB)")
        loc = json.loads(loc_path.read_text(encoding="utf-8"))
        n_loc = len(loc["cases"])
        log(f"LoCoMo cases: {n_loc}")
        t1 = time.time()
        slice_loc = max(1, n_loc // 10)
        slices_loc = 0
        for start in range(0, n_loc, slice_loc):
            sub = {"name": loc.get("name"), "schema_version": loc.get("schema_version"),
                   "scope": loc.get("scope"), "cases": loc["cases"][start:start+slice_loc]}
            r = run_locomo(runtime, sub, mode="raw", granularity="turn", limit=len(sub["cases"]))
            slices_loc += 1
            log(f"  LoCoMo slice {slices_loc}/{(n_loc + slice_loc - 1) // slice_loc} "
                f"[{start}:{start+len(sub['cases'])}] "
                f"R@1={r.get('recall_at_1', r.get('retrieval_recall_at_1'))} "
                f"R@5={r.get('recall_at_5', r.get('retrieval_recall_at_5'))} "
                f"MRR={r.get('mrr'):.4f} NDCG@5={r.get('ndcg_at_5'):.4f} "
                f"({time.time()-t1:.0f}s elapsed)")
        log("LoCoMo: running final aggregate over ALL cases")
        r_loc = run_locomo(runtime, loc, mode="raw", granularity="turn", limit=n_loc)
        log(f"LoCoMo FULL: R@1={r_loc.get('recall_at_1', r_loc.get('retrieval_recall_at_1'))} "
            f"R@5={r_loc.get('recall_at_5', r_loc.get('retrieval_recall_at_5'))} "
            f"MRR={r_loc.get('mrr'):.4f} NDCG@5={r_loc.get('ndcg_at_5'):.4f} "
            f"({time.time()-t1:.0f}s total, {n_loc} cases)")

        log(f"DONE wall={time.time()-t0:.0f}s")
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
