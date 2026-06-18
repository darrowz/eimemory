"""Quick smoke test: run a 5-case subset of each converted dataset through the
eimemory adapter locally to validate the format before pushing to the server.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

DATA = Path(r"E:\eimemory\data")


def main() -> int:
    from eimemory.api.runtime import Runtime
    from eimemory.evaluation.longmemeval import run_longmemeval
    from eimemory.evaluation.locomo import run_locomo

    tmp = Path(tempfile.mkdtemp(prefix="eimemory-bench-smoke-"))
    runtime = Runtime.create(root=tmp)
    try:
        # LongMemEval subset
        lme = json.loads((DATA / "longmemeval_s_eimemory.json").read_text(encoding="utf-8"))
        lme["cases"] = lme["cases"][:3]
        report = run_longmemeval(runtime, lme, mode="raw", granularity="session", limit=5)
        print(f"LME smoke 3/500: R@1={report['retrieval_recall_at_1']} R@5={report['retrieval_recall_at_5']} MRR={report['mrr']} NDCG@5={report['ndcg_at_5']}")

        # LoCoMo subset
        loc = json.loads((DATA / "locomo10_eimemory.json").read_text(encoding="utf-8"))
        loc["cases"] = loc["cases"][:5]
        report2 = run_locomo(runtime, loc, mode="raw", granularity="turn", limit=5)
        print(f"LoCoMo smoke 5/1986: R@1={report2.get('recall_at_1', report2.get('retrieval_recall_at_1'))} R@5={report2.get('recall_at_5', report2.get('retrieval_recall_at_5'))} MRR={report2['mrr']} NDCG@5={report2['ndcg_at_5']}")
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
