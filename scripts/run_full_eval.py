"""Full eval v2: single-pass per dataset, with heartbeat.

v1 sliced the dataset into 10 chunks and re-ran the adapter per slice.
Each slice re-loaded the data and re-built the Runtime state, so the
first slice alone took 12 min for 50 cases.

v2 runs the whole dataset in ONE call per dataset. It also launches a
background heartbeat thread that logs every 30s so the operator can
tail the log and see "still alive" without restarting.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("EIMEMORY_DATA_DIR") or _REPO_ROOT / "data")
LOG = os.environ.get("EIMEMORY_EVAL_LOG", "/tmp/full_eval.log")
HEARTBEAT_S = int(os.environ.get("EIMEMORY_HEARTBEAT_S", "30"))


_heartbeat_stop = threading.Event()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def heartbeat(stage: str, start: float) -> None:
    while not _heartbeat_stop.is_set():
        _heartbeat_stop.wait(HEARTBEAT_S)
        if _heartbeat_stop.is_set():
            return
        log(f"  [heartbeat] {stage} still running, elapsed {time.time()-start:.0f}s")


def run_dataset(name: str, dataset: dict, runner, **kwargs) -> dict:
    t0 = time.time()
    n = len(dataset["cases"])
    log(f"=== {name}: starting single-pass over {n} cases ===")
    _heartbeat_stop.clear()
    hb = threading.Thread(target=heartbeat, args=(name, t0), daemon=True)
    hb.start()
    try:
        report = runner(dataset, **kwargs)
    finally:
        _heartbeat_stop.set()
        hb.join(timeout=2)
    dt = time.time() - t0
    log(f"=== {name}: DONE in {dt:.1f}s ===")
    log(f"  full report: {json.dumps(report, ensure_ascii=False, default=str)}")
    return report


def main() -> int:
    try:
        with open(LOG, "w", encoding="utf-8") as f:
            f.write(f"# full eval v2 single-pass started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except Exception:
        pass
    log(f"DATA dir: {DATA}")
    log(f"Python: {sys.version.split()[0]}")
    log(f"Heartbeat every {HEARTBEAT_S}s")

    from eimemory.api.runtime import Runtime
    from eimemory.evaluation.longmemeval import run_longmemeval
    from eimemory.evaluation.locomo import run_locomo

    tmp = Path(tempfile.mkdtemp(prefix="eimemory-eval-"))
    log(f"Runtime tmp: {tmp}")
    runtime = Runtime.create(root=tmp)
    try:
        # LongMemEval
        lme_path = DATA / "longmemeval_s_eimemory.json"
        log(f"LME raw: {lme_path} ({lme_path.stat().st_size/1024/1024:.1f} MB)")
        lme = json.loads(lme_path.read_text(encoding="utf-8"))
        n_lme = len(lme["cases"])
        log(f"LME cases: {n_lme}")

        def lme_runner(ds, **kw):
            return run_longmemeval(runtime, ds, mode="raw", granularity="session", limit=len(ds["cases"]))
        lme_report = run_dataset("LME", lme, lme_runner)

        # LoCoMo
        loc_path = DATA / "locomo10_eimemory.json"
        log(f"LoCoMo raw: {loc_path} ({loc_path.stat().st_size/1024/1024:.1f} MB)")
        loc = json.loads(loc_path.read_text(encoding="utf-8"))
        n_loc = len(loc["cases"])
        log(f"LoCoMo cases: {n_loc}")

        def loc_runner(ds, **kw):
            return run_locomo(runtime, ds, mode="raw", granularity="turn", limit=len(ds["cases"]))
        loc_report = run_dataset("LoCoMo", loc, loc_runner)

        log("=== ALL DONE ===")
        log(f"LME:    R@1={lme_report.get('retrieval_recall_at_1')} R@5={lme_report.get('retrieval_recall_at_5')} MRR={lme_report.get('mrr'):.4f} NDCG@5={lme_report.get('ndcg_at_5'):.4f}")
        log(f"LoCoMo: R@1={loc_report.get('recall_at_1', loc_report.get('retrieval_recall_at_1'))} R@5={loc_report.get('recall_at_5', loc_report.get('retrieval_recall_at_5'))} MRR={loc_report.get('mrr'):.4f} NDCG@5={loc_report.get('ndcg_at_5'):.4f}")
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
