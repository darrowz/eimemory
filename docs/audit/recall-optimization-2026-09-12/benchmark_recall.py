"""Paired local recall benchmark; no production data or external services.

Run from the repository root:
  python docs/audit/recall-optimization-2026-09-12/benchmark_recall.py

Only contracts.py's Mapping and bounded-freeze implementation alternate.
All other code, records, query, and SQLite connection stay identical.
"""
from __future__ import annotations

import argparse
import ast
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict
import json
import os
from pathlib import Path
import platform
from statistics import median
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Mapping as TypingMapping

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval import contracts
from eimemory.storage.runtime_store import RuntimeStore


def stable_scoring(value):
    # Provenance timestamps change on each recall; all score values must agree.
    if isinstance(value, dict):
        return {key: stable_scoring(item) for key, item in value.items() if key != "generated_at"}
    if isinstance(value, list):
        return [stable_scoring(item) for item in value]
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=1500)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--baseline", default="e40ec88")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.records < 1 or args.rounds < 2:
        parser.error("records must be positive and rounds must be at least two")

    baseline_source = subprocess.check_output(
        ["git", "show", f"{args.baseline}:eimemory/retrieval/contracts.py"],
        cwd=ROOT, text=True, encoding="utf-8",
    )
    baseline_node = next(node for node in ast.parse(baseline_source).body
                         if isinstance(node, ast.FunctionDef) and node.name == "_freeze_bounded")
    baseline_namespace = {**vars(contracts), "Mapping": TypingMapping}
    exec(compile(ast.Module(body=[baseline_node], type_ignores=[]), "baseline-contracts.py", "exec"),
         baseline_namespace)
    original_freeze, original_mapping = contracts._freeze_bounded, contracts.Mapping
    implementations = {
        "baseline": (baseline_namespace["_freeze_bounded"], TypingMapping),
        "optimized": (original_freeze, Mapping),
    }
    scope = ScopeRef(tenant_id="bench", agent_id="bench", workspace_id="bench", user_id="bench")
    query = "Borealis deployment tests before release"
    timings: dict[str, list[float]] = {name: [] for name in implementations}
    expected = None
    disabled_features = (
        "EIMEMORY_POSTGRES_VECTOR_ENABLED", "EIMEMORY_RERANKER_ENABLED",
        "EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED",
    )
    previous_environment = {key: os.environ.get(key) for key in disabled_features}
    os.environ.update({key: "0" for key in disabled_features})
    try:
        with TemporaryDirectory(prefix="eimemory-recall-bench-") as root, closing(RuntimeStore(root)) as store:
            for index in range(args.records):
                topic = ("Borealis", "Orion", "Vega", "Atlas", "Apollo")[index % 5]
                text = (
                    f"Project {topic} deployment requirement: run integration tests, verify build artifacts "
                    f"and check health before release. Owner for checklist {index} is release engineering. "
                ) * 4
                store.sqlite.upsert(RecordEnvelope.create(
                    kind="memory", title=f"{topic} release checklist {index}", summary=text[:200],
                    content={"text": text, "memory_type": "fact"}, scope=scope,
                    source_id="bench", source="user.capture", meta={"memory_type": "fact"},
                ), commit=False)
            store.sqlite.conn.commit()
            api = MemoryAPI(store)
            # Alternate order to reduce warm-cache and temporal ordering bias.
            for iteration in range(args.rounds + 3):
                names = ("baseline", "optimized") if iteration % 2 == 0 else ("optimized", "baseline")
                for name in names:
                    contracts._freeze_bounded, contracts.Mapping = implementations[name]
                    started = perf_counter()
                    bundle = api.recall(query=query, scope=asdict(scope), limit=6,
                                        task_context={"source_ids": ["bench"]})
                    elapsed_ms = (perf_counter() - started) * 1000
                    assert bundle.items, "Benchmark must exercise a nonempty recall"
                    fingerprint = {
                        "items": [item.record_id for item in bundle.items],
                        "rules": [rule.record_id for rule in bundle.rules],
                        "confidence": bundle.confidence,
                        "retrieval_status": bundle.explanation.get("retrieval_status"),
                        "scoring": stable_scoring(bundle.explanation["scoring"]),
                        "evidence_refs": bundle.explanation["evidence_refs"],
                        "cascade_evidence": bundle.explanation["cascade_evidence"],
                    }
                    if expected is None:
                        expected = fingerprint
                    assert fingerprint == expected, "Recall results changed between variants"
                    if iteration >= 3:
                        timings[name].append(elapsed_ms)
    finally:
        contracts._freeze_bounded, contracts.Mapping = original_freeze, original_mapping
        for key, value in previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    medians = {name: median(values) for name, values in timings.items()}
    result = {
        "baseline_commit": args.baseline,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "disabled_optional_features": disabled_features,
        "dataset": {"records": args.records, "topics": 5, "source_ids": ["bench"],
                    "query": query, "limit": 6, "sqlite_projection_only": True},
        "warmup_rounds": 3,
        "measured_rounds": args.rounds,
        "timings_ms": {name: [round(value, 3) for value in values] for name, values in timings.items()},
        "median_ms": {name: round(value, 3) for name, value in medians.items()},
        "median_reduction_percent": round(100 * (1 - medians["optimized"] / medians["baseline"]), 2),
        "identical_results": True,
        "comparison": "Item/rule order, confidence, scoring excluding generated_at, evidence refs and cascade evidence",
        "returned_items": len(expected["items"]),
        "limits": "Synthetic warm SQLite workload; isolates contract optimization, not all closure changes or production p95.",
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
