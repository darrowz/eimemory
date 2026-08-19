"""Isolated functional and performance baseline for the L5 v3 refactor.

The baseline deliberately measures the current (v2) runtime before Storage v2
or the dynamic L5 reader exists.  It is a comparison point, not a claim that
the fixed v2 capability taxonomy is the desired architecture.

The module has no third-party benchmark dependency.  It forces the SQLite
candidate source, uses a fresh non-production root for every run, and reports
machine details as context only.  The comparison policy is relative to a
recorded workload digest; it never treats a hostname, model version, or an
absolute millisecond target as a pass/fail identity.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.config.defaults import default_root
from eimemory.core.clock import now_iso
from eimemory.governance.capability_ledger import build_capability_ledger, record_capability_score
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.sqlite_store import SqliteRecordStore


SCHEMA_VERSION = "l5_v3_baseline.v1"
PROFILE_SCHEMA_VERSION = "l5_v3_budget_profile.v1"
FIXTURE_TIMESTAMP = "2026-08-20T00:00:00Z"
SCOPE = ScopeRef(
    tenant_id="benchmark",
    agent_id="l5-v3-baseline",
    workspace_id="isolated",
    user_id="",
)
ADAPTER_CHANNELS = ("codex", "hermes", "openclaw")
REPLAY_CAPABILITIES = ("memory.recall",)
REPO_ROOT = Path(__file__).resolve().parents[1]
RECALL_QUERY = "benchmark capability signal stable retrieval"
MUTATION_TEXT = "benchmark capability signal mutation payload"
OUTCOME_SOURCE = "benchmark.local"
OUTCOME_TASK_TYPE = "benchmark.l5_v3"
WORKLOAD_SCHEMA_VERSION = "l5_v3_workload.v1"


class BenchmarkIsolationError(ValueError):
    """Raised before a benchmark could use a production-owned path."""


@dataclass(frozen=True, slots=True)
class TierSpec:
    """One deterministic workload scale; counts exclude warmup write samples."""

    name: str
    memory_records: int
    capabilities: int
    scores_per_capability: int
    replay_results: int
    outcome_traces: int
    knowledge_links: int
    adapter_descriptors: int
    legacy_migration_rows: int

    def to_dict(self) -> dict[str, int | str]:
        return asdict(self)


TIER_SPECS: dict[str, TierSpec] = {
    "small": TierSpec(
        name="small",
        memory_records=256,
        capabilities=12,
        scores_per_capability=3,
        replay_results=36,
        outcome_traces=36,
        knowledge_links=12,
        adapter_descriptors=3,
        legacy_migration_rows=32,
    ),
    "medium": TierSpec(
        name="medium",
        memory_records=5_000,
        capabilities=48,
        scores_per_capability=5,
        replay_results=240,
        outcome_traces=240,
        knowledge_links=48,
        adapter_descriptors=3,
        legacy_migration_rows=256,
    ),
    "large": TierSpec(
        name="large",
        memory_records=25_000,
        capabilities=128,
        scores_per_capability=8,
        replay_results=1_024,
        outcome_traces=1_024,
        knowledge_links=128,
        adapter_descriptors=3,
        legacy_migration_rows=2_048,
    ),
}

DEFAULT_TIERS = ("small", "medium")
DEFAULT_SAMPLES = 9
DEFAULT_WARMUP = 2
_BOOTSTRAP_REPLICATES = 200
_VOLATILE_KEYS = frozenset(
    {
        "generated_at",
        "executed_at",
        "execution_id",
        "run_id",
        "recorded_at",
        "started_at",
        "finished_at",
        "latency_ms",
        "duration_ms",
        "elapsed_ms",
    }
)


def canonical_digest(value: Any) -> str:
    """Hash JSON after a deterministic conversion of benchmark-owned values."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True


def production_root() -> Path:
    """Resolve the ordinary runtime root without creating or touching it."""

    return default_root(None).resolve(strict=False)


def _assert_isolated_path(path: Path) -> None:
    target = path.resolve(strict=False)
    protected = production_root()
    if _path_is_within(target, protected):
        raise BenchmarkIsolationError(
            f"benchmark path must not equal or live below EIMEMORY_ROOT: {target}"
        )


@contextlib.contextmanager
def _benchmark_workspace(
    output_dir: Path | None,
    *,
    keep_artifacts: bool,
) -> Iterator[tuple[Path, bool]]:
    """Yield an isolated workspace and whether it remains after the run."""

    if output_dir is not None:
        parent = output_dir.resolve(strict=False)
        _assert_isolated_path(parent)
        parent.mkdir(parents=True, exist_ok=True)
        workspace = parent / f"l5-v3-baseline-{uuid.uuid4().hex}"
        _assert_isolated_path(workspace)
        workspace.mkdir(parents=True, exist_ok=False)
        yield workspace, True
        return

    if keep_artifacts:
        parent = (REPO_ROOT / ".bench-artifacts").resolve(strict=False)
        _assert_isolated_path(parent)
        parent.mkdir(parents=True, exist_ok=True)
        workspace = parent / f"l5-v3-baseline-{uuid.uuid4().hex}"
        workspace.mkdir(parents=True, exist_ok=False)
        yield workspace, True
        return

    with tempfile.TemporaryDirectory(prefix="eimemory-l5-v3-benchmark-") as temporary:
        workspace = Path(temporary).resolve(strict=False)
        _assert_isolated_path(workspace)
        yield workspace, False


def _forced_sqlite_runtime(root: Path) -> Runtime:
    """Create a runtime without reading an optional external candidate source."""

    _assert_isolated_path(root)
    # Calling Runtime(RuntimeStore(...)) intentionally bypasses Runtime.create,
    # whose optional PostgreSQL candidate source follows environment settings.
    # MemoryAPI therefore installs its SQLiteCandidateSource.
    return Runtime(RuntimeStore(root))


def _fixed_record(
    *,
    record_id: str,
    kind: str,
    title: str,
    summary: str,
    content: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
    source: str = "benchmark.l5_v3",
) -> RecordEnvelope:
    record = RecordEnvelope.create(
        kind=kind,
        title=title,
        summary=summary,
        detail=summary,
        content=dict(content or {}),
        meta=dict(meta or {}),
        source=source,
        scope=SCOPE,
    )
    record.record_id = record_id
    record.time = TimeRef(
        created_at=FIXTURE_TIMESTAMP,
        updated_at=FIXTURE_TIMESTAMP,
        occurred_at=FIXTURE_TIMESTAMP,
    )
    return record


def _capability_ids(spec: TierSpec) -> list[str]:
    dynamic_count = max(0, spec.capabilities - 1)
    return ["memory.recall", *[f"benchmark.{spec.name}.capability.{index:03d}" for index in range(dynamic_count)]]


def workload_contract(spec: TierSpec) -> dict[str, Any]:
    """The complete logical workload identity, independent of machine context."""

    return {
        "schema_version": WORKLOAD_SCHEMA_VERSION,
        "tier": spec.to_dict(),
        "scope": asdict(SCOPE),
        "candidate_source": "sqlite_forced",
        "fixture_roles": {
            "memory": {
                "kind": "memory",
                "record_id_template": f"bench-{spec.name}-memory-{{index:05d}}",
                "title_template": "Benchmark memory {index}",
                "summary_template": "benchmark capability signal stable retrieval item {index}",
                "content": {"memory_type": "fact"},
                "meta": {"memory_type": "fact", "benchmark_role": "memory"},
            },
            "knowledge_link": {
                "kind": "knowledge_page",
                "record_id_template": f"bench-{spec.name}-knowledge-{{index:04d}}",
                "summary_template": "knowledge applicability for benchmark capability {index}",
                "meta": {"benchmark_role": "knowledge_link", "trust": "reviewed"},
            },
            "adapter_descriptor": {
                "kind": "capability_model",
                "channels": list(ADAPTER_CHANNELS[: spec.adapter_descriptors]),
                "operations": ["prefetch", "status"],
                "meta": {"benchmark_role": "adapter_descriptor"},
            },
            "replay_result": {
                "kind": "replay_result",
                "record_id_template": f"bench-{spec.name}-replay-{{index:04d}}",
                "case_id_template": "benchmark-case-{index:04d}",
                "verdict": "pass",
                "hit": True,
            },
            "outcome_trace": {
                "trace_id_template": f"bench-{spec.name}-outcome-{{index:04d}}",
                "source": OUTCOME_SOURCE,
                "task_type": OUTCOME_TASK_TYPE,
                "outcome": {"status": "success", "rehearsal": True},
                "actions": [{"tool": "memory", "status": "ok"}],
            },
            "capability_score": {
                "capabilities": _capability_ids(spec),
                "score_formula": "0.72 + ((capability_index + score_index) % 10) / 100",
                "evidence_memory_positions": [0, 1, 2],
                "evidence_tiers": ["benchmark"],
                "evidence_sources": [OUTCOME_SOURCE],
                "meta": {"benchmark_role": "capability_score"},
            },
            "measurement_writes": {
                "kind": "memory",
                "text": MUTATION_TEXT,
                "memory_type": "fact",
            },
        },
        "operation_inputs": {
            "candidate_recall": {"query": RECALL_QUERY, "limit": 8},
            "adapter_prefetch": {
                "channels": list(ADAPTER_CHANNELS),
                "query": RECALL_QUERY,
                "limit": 8,
            },
            "adapter_status": {"channels": list(ADAPTER_CHANNELS), "cache_mode": "warm"},
            "readiness_no_release": {"persist": False, "limit": 500, "release_receipt": "absent"},
            "capability_ledger": {"attribute_outcomes": False, "ensure_seeded": False, "limit": 500},
            "capability_replay_pack": {"capabilities": list(REPLAY_CAPABILITIES), "persist": False},
            "runtime_cold_startup": {"candidate_source": "sqlite_forced", "fixture_copy_excluded": True},
            "legacy_migration_batch": {
                "batch_size": min(64, spec.legacy_migration_rows),
                "offline": False,
                "schema": "storage.schema.v1+records.meta_keys.v1+intent_patterns.payload_status.v1",
            },
        },
    }


def seed_fixture(runtime: Runtime, spec: TierSpec) -> dict[str, Any]:
    """Seed all workload roles using current production-facing write paths."""

    scope = asdict(SCOPE)
    memory_ids: list[str] = []
    for index in range(spec.memory_records):
        record_id = f"bench-{spec.name}-memory-{index:05d}"
        record = _fixed_record(
            record_id=record_id,
            kind="memory",
            title=f"Benchmark memory {index}",
            summary=f"{RECALL_QUERY} item {index}",
            content={
                "text": f"{RECALL_QUERY} item {index}",
                "memory_type": "fact",
            },
            meta={"memory_type": "fact", "benchmark_role": "memory"},
        )
        runtime.store.append(record)
        memory_ids.append(record_id)

    knowledge_ids: list[str] = []
    for index in range(spec.knowledge_links):
        record_id = f"bench-{spec.name}-knowledge-{index:04d}"
        runtime.store.append(
            _fixed_record(
                record_id=record_id,
                kind="knowledge_page",
                title=f"Benchmark knowledge page {index}",
                summary=f"knowledge applicability for benchmark capability {index}",
                content={"text": f"knowledge applicability for benchmark capability {index}"},
                meta={"benchmark_role": "knowledge_link", "trust": "reviewed"},
            )
        )
        knowledge_ids.append(record_id)

    adapter_channels = list(ADAPTER_CHANNELS[: spec.adapter_descriptors])
    for channel in adapter_channels:
        runtime.store.append(
            _fixed_record(
                record_id=f"bench-{spec.name}-adapter-{channel}",
                kind="capability_model",
                title=f"Benchmark adapter descriptor {channel}",
                summary=f"adapter descriptor for {channel}",
                content={"channel": channel, "operations": ["prefetch", "status"]},
                meta={"benchmark_role": "adapter_descriptor", "channel": channel},
            )
        )

    replay_ids: list[str] = []
    for index in range(spec.replay_results):
        record_id = f"bench-{spec.name}-replay-{index:04d}"
        runtime.store.append(
            _fixed_record(
                record_id=record_id,
                kind="replay_result",
                title=f"Benchmark replay result {index}",
                summary=f"replay fixture {index}",
                content={"case_id": f"benchmark-case-{index:04d}", "verdict": "pass", "hit": True},
                meta={"benchmark_role": "replay_result", "verdict": "pass"},
            )
        )
        replay_ids.append(record_id)

    outcome_ids: list[str] = []
    for index in range(spec.outcome_traces):
        trace_id = f"bench-{spec.name}-outcome-{index:04d}"
        result = runtime.record_outcome_trace(
            {
                "trace_id": trace_id,
                "idempotency_key": f"{trace_id}:v1",
                "recorded_at": FIXTURE_TIMESTAMP,
                "source": OUTCOME_SOURCE,
                "task_type": OUTCOME_TASK_TYPE,
                "input_summary": f"deterministic benchmark outcome {index}",
                "outcome": {"status": "success", "rehearsal": True},
                "actions": [{"tool": "memory", "status": "ok"}],
            },
            scope=scope,
        )
        if result.get("ok") is not True:
            raise RuntimeError(f"outcome trace fixture failed: {result}")
        outcome_ids.append(str(result.get("record_id") or result.get("record", {}).get("record_id") or ""))

    score_ids: list[str] = []
    evidence_ids = memory_ids[:3]
    for capability_index, capability in enumerate(_capability_ids(spec)):
        for score_index in range(spec.scores_per_capability):
            score_ids.append(
                record_capability_score(
                    runtime,
                    scope=SCOPE,
                    loop_id=f"benchmark.{spec.name}",
                    capability=capability,
                    score=round(0.72 + ((capability_index + score_index) % 10) / 100.0, 3),
                    evidence_record_ids=evidence_ids,
                    evidence_tiers=["benchmark"],
                    evidence_sources=[OUTCOME_SOURCE],
                    meta={"benchmark_role": "capability_score"},
                )
            )

    return {
        "scope": scope,
        "memory_record_ids": memory_ids,
        "knowledge_record_ids": knowledge_ids,
        "replay_record_ids": replay_ids,
        "outcome_record_ids": [item for item in outcome_ids if item],
        "capability_score_record_ids": score_ids,
        "capabilities": _capability_ids(spec),
        "adapter_channels": adapter_channels,
        "fixture_digest": canonical_digest(workload_contract(spec)),
    }


def _percentile(values: Sequence[float], percentage: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    rank = max(0, min(len(ordered) - 1, int(((percentage / 100.0) * len(ordered) + 0.999999999) - 1)))
    return ordered[rank]


def _bootstrap_median_band(values: Sequence[float]) -> list[float]:
    if not values:
        return [0.0, 0.0]
    seed = int(canonical_digest([round(float(value), 9) for value in values])[:16], 16)
    generator = random.Random(seed)
    samples = [float(value) for value in values]
    medians = sorted(
        statistics.median(generator.choice(samples) for _ in samples)
        for _ in range(_BOOTSTRAP_REPLICATES)
    )
    return [_percentile(medians, 2.5), _percentile(medians, 97.5)]


def _latency_summary(samples_ms: Sequence[float]) -> dict[str, Any]:
    values = [float(value) for value in samples_ms]
    median = statistics.median(values) if values else 0.0
    mad = statistics.median([abs(value - median) for value in values]) if values else 0.0
    return {
        "sample_count": len(values),
        "samples_ms": [round(value, 6) for value in values],
        "p50_ms": round(_percentile(values, 50), 6),
        "p95_ms": round(_percentile(values, 95), 6),
        "p99_ms": round(_percentile(values, 99), 6),
        "median_ms": round(median, 6),
        "mad_ms": round(mad, 6),
        "relative_mad": round((mad / median) if median else 0.0, 6),
        "median_bootstrap_95pct_ms": [round(value, 6) for value in _bootstrap_median_band(values)],
    }


def _stable_data(value: Any) -> Any:
    """Retain a recursively sorted, JSON-safe value while removing timestamps."""

    if isinstance(value, Mapping):
        return {
            str(key): _stable_data(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            if str(key) not in _VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_stable_data(item) for item in value]
    return value


def semantic_view(operation: str, result: Any) -> Any:
    """Project an API result to its stable semantic contract for parity checks."""

    if operation == "candidate_recall":
        records, diagnostics = result
        return {
            "records": [
                {
                    "record_id": record.record_id,
                    "kind": record.kind,
                    "source_id": record.source_id,
                }
                for record in records
            ],
            "diagnostic_keys": sorted(str(key) for key in (diagnostics or {})),
        }
    if operation in {"append", "atomic_mutation"}:
        record = result
        return {"kind": record.kind, "status": record.status, "source": record.source}
    if operation == "capability_ledger":
        capabilities = result.get("capabilities") if isinstance(result, Mapping) else {}
        compact = {
            capability: {
                "score": item.get("score"),
                "status": item.get("status"),
                "evidence_count": item.get("evidence_count"),
            }
            for capability, item in sorted((capabilities or {}).items())
            if capability == "memory.recall" or str(capability).startswith("benchmark.")
        }
        return {"ok": bool(result.get("ok")), "capabilities": compact}
    if operation == "readiness_no_release":
        validation = result.get("release_validation") if isinstance(result, Mapping) else {}
        migrations = result.get("storage_migrations") if isinstance(result, Mapping) else {}
        gaps = result.get("capability_gaps") if isinstance(result, Mapping) else {}
        return {
            "ok": bool(result.get("ok")),
            "observed_stage": result.get("observed_stage"),
            "observed_score": result.get("observed_score"),
            "stage_reason": result.get("stage_reason"),
            "capability_gaps": _stable_data(gaps),
            "release_validation_status": str((validation or {}).get("status") or ""),
            "storage_migrations": {
                "ok": bool((migrations or {}).get("ok")),
                "status": str((migrations or {}).get("status") or ""),
                "pending": sorted(str(item) for item in ((migrations or {}).get("pending") or [])),
            },
        }
    if operation == "capability_replay_pack":
        packs = []
        for pack in result.get("packs") or []:
            packs.append(
                {
                    "capability": str(pack.get("capability") or ""),
                    "pass_rate": pack.get("pass_rate"),
                    "score": pack.get("score"),
                    "cases": [
                        {
                            "case_id": str(case.get("case_id") or ""),
                            "verdict": str(case.get("verdict") or ""),
                            "hit": case.get("hit"),
                        }
                        for case in pack.get("case_results") or []
                    ],
                }
            )
        return {"ok": bool(result.get("ok")), "packs": packs}
    if operation == "adapter_prefetch":
        return {
            channel: {
                "ok": bool(value.get("ok")),
                "contract": str(value.get("adapter_contract_version") or ""),
                "items": [
                    {
                        "record_id": str(item.get("record_id") or ""),
                        "kind": str(item.get("kind") or ""),
                        "source_id": str(item.get("source_id") or ""),
                    }
                    for item in ((value.get("bundle") or {}).get("items") or [])
                ],
            }
            for channel, value in sorted(result.items())
        }
    if operation == "adapter_status":
        return {
            channel: {
                "ok": bool(value.get("ok")),
                "channel": str(value.get("channel") or ""),
                "attestation_available": bool(value.get("attestation_available")),
            }
            for channel, value in sorted(result.items())
        }
    if operation in {"runtime_cold_startup", "legacy_startup", "legacy_migration_batch"}:
        return _stable_data(result)
    return _stable_data(result)


def _measure_operation(
    *,
    operation: str,
    mode: str,
    samples: int,
    warmup: int,
    invoke: Callable[[int], Any],
) -> dict[str, Any]:
    if samples < 1:
        raise ValueError("samples must be positive")
    if warmup < 0:
        raise ValueError("warmup must not be negative")
    latencies: list[float] = []
    semantic_digests: list[str] = []
    semantic_views: list[Any] = []
    ordinal = 0
    for sample_index in range(samples):
        for _ in range(warmup):
            invoke(ordinal)
            ordinal += 1
        started = time.perf_counter_ns()
        result = invoke(ordinal)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        ordinal += 1
        latencies.append(elapsed_ms)
        view = semantic_view(operation, result)
        semantic_views.append(view)
        semantic_digests.append(canonical_digest(view))
    metrics = _latency_summary(latencies)
    metrics.update(
        {
            "operation": operation,
            "mode": mode,
            "semantic_digest": semantic_digests[0],
            "semantic_summary": semantic_views[0],
            "semantic_parity": {
                "ok": len(set(semantic_digests)) == 1,
                "unique_digest_count": len(set(semantic_digests)),
            },
        }
    )
    if metrics["semantic_parity"]["ok"] is not True:
        raise RuntimeError(f"{operation} returned non-deterministic semantics in {mode}")
    return metrics


def _record_for_measurement(*, spec: TierSpec, operation: str, ordinal: int) -> RecordEnvelope:
    return _fixed_record(
        record_id=f"bench-{spec.name}-{operation}-{ordinal:05d}",
        kind="memory",
        title=f"Benchmark {operation} {ordinal}",
        summary=MUTATION_TEXT,
        content={"text": MUTATION_TEXT, "memory_type": "fact"},
        meta={"memory_type": "fact", "benchmark_role": operation},
    )


def _measure_warm_operations(runtime: Runtime, spec: TierSpec, *, samples: int, warmup: int) -> dict[str, dict[str, Any]]:
    scope = asdict(SCOPE)
    service = AgentRuntimeMemoryService(runtime)

    def recall(_ordinal: int) -> Any:
        return runtime.store.search_with_diagnostics(
            query=RECALL_QUERY,
            scope=SCOPE,
            limit=8,
        )

    def append(ordinal: int) -> RecordEnvelope:
        return runtime.store.append(_record_for_measurement(spec=spec, operation="append", ordinal=ordinal))

    def atomic(ordinal: int) -> RecordEnvelope:
        record = _record_for_measurement(spec=spec, operation="atomic", ordinal=ordinal)

        def mutation(sqlite: SqliteRecordStore) -> tuple[RecordEnvelope, list[RecordEnvelope], list[Any]]:
            sqlite.upsert(record, commit=False)
            return record, [record], []

        return runtime.store.mutate_records_atomically(mutation)

    def readiness(_ordinal: int) -> dict[str, Any]:
        return runtime.build_l5_readiness_report(
            scope=scope,
            persist=False,
            limit=500,
            repo_root=str(REPO_ROOT),
        )

    def ledger(_ordinal: int) -> dict[str, Any]:
        return build_capability_ledger(
            runtime,
            scope=SCOPE,
            limit=500,
            ensure_seeded=False,
            attribute_outcomes=False,
        )

    def replay(_ordinal: int) -> dict[str, Any]:
        return runtime.build_capability_replay_packs(
            scope=scope,
            capabilities=list(REPLAY_CAPABILITIES),
            persist=False,
            loop_id="benchmark.l5_v3",
        )

    def adapter_prefetch(_ordinal: int) -> dict[str, Any]:
        return {
            channel: service.prefetch(
                channel=channel,
                scope=scope,
                query=RECALL_QUERY,
                limit=8,
            )
            for channel in ADAPTER_CHANNELS
        }

    def adapter_status(_ordinal: int) -> dict[str, Any]:
        return {channel: service.status(channel=channel, scope=scope) for channel in ADAPTER_CHANNELS}

    operations: tuple[tuple[str, Callable[[int], Any]], ...] = (
        ("candidate_recall", recall),
        ("append", append),
        ("atomic_mutation", atomic),
        ("readiness_no_release", readiness),
        ("capability_ledger", ledger),
        ("capability_replay_pack", replay),
        ("adapter_prefetch", adapter_prefetch),
        ("adapter_status", adapter_status),
    )
    measured = {
        name: _measure_operation(
            operation=name,
            mode="warm",
            samples=samples,
            warmup=warmup,
            invoke=operation,
        )
        for name, operation in operations
    }
    measured["adapter_status"]["cache_state"] = (
        "warm_cache_hit_after_per_sample_warmup"
        if warmup > 0
        else "mixed_first_sample_miss_then_process_cache_hits"
    )
    return measured


def _measure_runtime_cold_startup(seed_root: Path, tier_root: Path, *, samples: int) -> dict[str, Any]:
    cold_root = tier_root / "runtime-cold"
    cold_root.mkdir(parents=True, exist_ok=True)
    sample_roots: dict[int, Path] = {}
    for ordinal in range(samples):
        target = cold_root / f"sample-{ordinal:03d}"
        shutil.copytree(seed_root, target)
        sample_roots[ordinal] = target

    def open_runtime(ordinal: int) -> dict[str, Any]:
        runtime = _forced_sqlite_runtime(sample_roots[ordinal])
        try:
            return {
                "candidate_source": "sqlite_forced",
                "pending_migrations": sorted(runtime.store.sqlite.pending_storage_migrations()),
            }
        finally:
            runtime.close()

    result = _measure_operation(
        operation="runtime_cold_startup",
        mode="runtime_cold",
        samples=samples,
        warmup=0,
        invoke=open_runtime,
    )
    result["os_cache_not_controlled"] = True
    result["fixture_copy_excluded_from_timing"] = True
    return result


def _seed_legacy_database(path: Path, *, rows: int) -> None:
    """Create a deterministic pre-deferred-migration database, independent of tests."""

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE records (
              storage_key TEXT PRIMARY KEY, record_id TEXT NOT NULL, kind TEXT NOT NULL,
              status TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT NOT NULL,
              content_text TEXT NOT NULL, source TEXT NOT NULL, agent_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
              embedding_json TEXT NOT NULL DEFAULT '[]', idempotency_key TEXT NOT NULL DEFAULT '',
              semantic_key TEXT NOT NULL DEFAULT '', meta_json TEXT NOT NULL, payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
            CREATE TABLE recall_index (
              storage_key TEXT PRIMARY KEY, record_id TEXT NOT NULL, kind TEXT NOT NULL,
              status TEXT NOT NULL, source TEXT NOT NULL, tenant_id TEXT NOT NULL,
              agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL,
              lane TEXT NOT NULL, visibility TEXT NOT NULL, source_class TEXT NOT NULL,
              memory_type TEXT NOT NULL, projection_type TEXT NOT NULL,
              quality_score REAL NOT NULL DEFAULT 0.0, title_text TEXT NOT NULL,
              body_text TEXT NOT NULL, anchor_terms TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES
              ('storage.schema.v1', '2026-01-01T00:00:00Z'),
              ('records.meta_keys.v1', '2026-01-01T00:00:00Z'),
              ('intent_patterns.payload_status.v1', '2026-01-01T00:00:00Z');
            """
        )
        for index in range(rows):
            record = _fixed_record(
                record_id=f"legacy-benchmark-{index:05d}",
                kind="knowledge_page",
                title=f"legacy benchmark {index}",
                summary=f"legacy migration fixture {index}",
                content={"text": f"legacy benchmark {index}", "source_ids": ["paper-benchmark"]},
            )
            storage_key = "\x1f".join(["benchmark", "l5-v3-baseline", "isolated", "", record.record_id])
            payload = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True)
            connection.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    storage_key,
                    record.record_id,
                    record.kind,
                    record.status,
                    record.title,
                    record.summary,
                    record.detail,
                    f"legacy benchmark {index}",
                    record.source,
                    "l5-v3-baseline",
                    "isolated",
                    "",
                    "benchmark",
                    "[]",
                    "",
                    "",
                    "{}",
                    payload,
                    FIXTURE_TIMESTAMP,
                    FIXTURE_TIMESTAMP,
                ),
            )
            connection.execute(
                "INSERT INTO recall_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    storage_key,
                    record.record_id,
                    record.kind,
                    record.status,
                    record.source,
                    "benchmark",
                    "l5-v3-baseline",
                    "isolated",
                    "",
                    "knowledge",
                    "eligible",
                    "trusted",
                    "external_knowledge",
                    "full",
                    0.8,
                    record.title,
                    f"legacy benchmark {index}",
                    "legacy",
                    FIXTURE_TIMESTAMP,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _measure_legacy_migrations(tier_root: Path, spec: TierSpec, *, samples: int) -> dict[str, dict[str, Any]]:
    migration_root = tier_root / "legacy-migration"
    migration_root.mkdir(parents=True, exist_ok=True)
    startup_databases: dict[int, Path] = {}
    batch_databases: dict[int, Path] = {}
    for ordinal in range(samples):
        startup_database = migration_root / f"startup-{ordinal:03d}.sqlite"
        batch_database = migration_root / f"batch-{ordinal:03d}.sqlite"
        _seed_legacy_database(startup_database, rows=spec.legacy_migration_rows)
        _seed_legacy_database(batch_database, rows=spec.legacy_migration_rows)
        startup_databases[ordinal] = startup_database
        batch_databases[ordinal] = batch_database

    def start_store(ordinal: int) -> dict[str, Any]:
        store = SqliteRecordStore(startup_databases[ordinal])
        try:
            return {"pending": sorted(store.pending_storage_migrations())}
        finally:
            store.close()

    def one_batch(ordinal: int) -> dict[str, Any]:
        report = batch_stores[ordinal].apply_storage_migrations(
            batch_size=min(64, spec.legacy_migration_rows),
            offline=False,
        )
        return {
            "processed": int(report.get("processed") or 0),
            "pending": sorted(str(item) for item in (report.get("pending") or [])),
            "offline_required": bool(report.get("offline_required")),
            "index_created": bool(report.get("index_created")),
        }

    startup_report = _measure_operation(
        operation="legacy_startup",
        mode="isolated_legacy",
        samples=samples,
        warmup=0,
        invoke=start_store,
    )
    batch_stores = {ordinal: SqliteRecordStore(database) for ordinal, database in batch_databases.items()}
    try:
        batch_report = _measure_operation(
            operation="legacy_migration_batch",
            mode="isolated_legacy_batch",
            samples=samples,
            warmup=0,
            invoke=one_batch,
        )
    finally:
        for store in batch_stores.values():
            store.close()
    reports = {
        "legacy_startup": startup_report,
        "legacy_migration_batch": batch_report,
    }
    for report in reports.values():
        report["fixture_setup_excluded_from_timing"] = True
    return reports


def _closed_storage_footprint(root: Path) -> dict[str, int]:
    database = root / "state" / "eimemory.sqlite"
    paths = {
        "sqlite_bytes": database,
        "wal_bytes": database.with_name(database.name + "-wal"),
        "shm_bytes": database.with_name(database.name + "-shm"),
    }
    return {
        key: int(path.stat().st_size) if path.exists() else 0
        for key, path in paths.items()
    }


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _context() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "sqlite": sqlite3.sqlite_version,
        "machine": platform.machine(),
        "processor": platform.processor(),
        "environment_context_only": True,
        "candidate_source": "sqlite_forced",
        "os_cache_not_controlled": True,
        "preload_hot_rows": os.environ.get("EIMEMORY_PRELOAD_HOT_ROWS", "128"),
        "postgres_vector_enabled": os.environ.get("EIMEMORY_POSTGRES_VECTOR_ENABLED", ""),
    }


def _relative_allowance(operation: str) -> float:
    if operation in {"candidate_recall", "adapter_prefetch", "adapter_status"}:
        return 0.15
    if operation in {"append", "atomic_mutation", "capability_ledger", "capability_replay_pack", "readiness_no_release"}:
        return 0.20
    return 0.30


def build_budget_profile(report: Mapping[str, Any]) -> dict[str, Any]:
    """Build a versioned, machine-independent comparison profile."""

    tiers: dict[str, Any] = {}
    for tier_name, tier in sorted((report.get("tiers") or {}).items()):
        operations = dict(tier.get("warm") or {})
        operations.update(dict(tier.get("runtime_cold") or {}))
        operations.update(dict(tier.get("legacy_migration") or {}))
        tiers[tier_name] = {
            "workload_digest": tier.get("workload_digest"),
            "operations": {
                name: {
                    "baseline_p95_ms": metrics.get("p95_ms"),
                    "baseline_relative_mad": metrics.get("relative_mad"),
                    "baseline_semantic_digest": metrics.get("semantic_digest"),
                    "minimum_samples": 7,
                    "relative_allowance": _relative_allowance(name),
                }
                for name, metrics in sorted(operations.items())
            },
        }
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "baseline_commit": report.get("commit"),
        "workload_schema": SCHEMA_VERSION,
        "policy": {
            "environment_is_context_only": True,
            "same_workload_digest_required": True,
            "same_semantic_digest_required": True,
            "minimum_samples": 7,
            "noisy_relative_mad_threshold": 0.25,
            "noisy_result": "inconclusive",
            "comparison": "candidate_p95 <= baseline_p95 * (1 + relative_allowance)",
        },
        "tiers": tiers,
    }


def compare_against_profile(profile: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Compare compatible reports without using machine identity as a gate."""

    policy = dict(profile.get("policy") or {})
    minimum_samples = int(policy.get("minimum_samples") or 7)
    noise_threshold = float(policy.get("noisy_relative_mad_threshold") or 0.25)
    decisions: list[dict[str, Any]] = []
    candidate_tiers = dict(candidate.get("tiers") or {})
    for tier_name, baseline_tier in sorted((profile.get("tiers") or {}).items()):
        candidate_tier = candidate_tiers.get(tier_name)
        if not isinstance(candidate_tier, Mapping) or candidate_tier.get("workload_digest") != baseline_tier.get("workload_digest"):
            decisions.append({"tier": tier_name, "status": "incomparable", "reason": "workload_digest_mismatch"})
            continue
        candidate_operations: dict[str, Any] = {}
        for group in ("warm", "runtime_cold", "legacy_migration"):
            candidate_operations.update(dict(candidate_tier.get(group) or {}))
        for operation, baseline in sorted((baseline_tier.get("operations") or {}).items()):
            measured = candidate_operations.get(operation)
            if not isinstance(measured, Mapping):
                decisions.append({"tier": tier_name, "operation": operation, "status": "incomparable", "reason": "operation_missing"})
                continue
            if str(measured.get("semantic_digest") or "") != str(baseline.get("baseline_semantic_digest") or ""):
                decisions.append(
                    {
                        "tier": tier_name,
                        "operation": operation,
                        "status": "semantic_mismatch",
                        "reason": "semantic_digest_mismatch",
                    }
                )
                continue
            if int(measured.get("sample_count") or 0) < minimum_samples:
                decisions.append({"tier": tier_name, "operation": operation, "status": "inconclusive", "reason": "insufficient_samples"})
                continue
            if float(measured.get("relative_mad") or 0.0) > noise_threshold:
                decisions.append({"tier": tier_name, "operation": operation, "status": "inconclusive", "reason": "noisy_measurement"})
                continue
            baseline_p95 = max(0.001, float(baseline.get("baseline_p95_ms") or 0.0))
            allowance = float(baseline.get("relative_allowance") or 0.0)
            budget = baseline_p95 * (1.0 + allowance)
            actual = float(measured.get("p95_ms") or 0.0)
            decisions.append(
                {
                    "tier": tier_name,
                    "operation": operation,
                    "status": "pass" if actual <= budget else "regressed",
                    "candidate_p95_ms": actual,
                    "budget_p95_ms": round(budget, 6),
                }
            )
    statuses = {str(item["status"]) for item in decisions}
    overall = (
        "semantic_mismatch"
        if "semantic_mismatch" in statuses
        else (
            "regressed"
            if "regressed" in statuses
            else ("incomparable" if "incomparable" in statuses else ("inconclusive" if "inconclusive" in statuses else "pass"))
        )
    )
    return {"schema_version": "l5_v3_budget_comparison.v1", "status": overall, "decisions": decisions}


def _run_tier(workspace: Path, spec: TierSpec, *, samples: int, warmup: int) -> dict[str, Any]:
    tier_root = workspace / spec.name
    seed_root = tier_root / "runtime-seed"
    _assert_isolated_path(tier_root)
    runtime = _forced_sqlite_runtime(seed_root)
    try:
        fixture = seed_fixture(runtime, spec)
        warm = _measure_warm_operations(runtime, spec, samples=samples, warmup=warmup)
        open_footprint = runtime.store.storage_footprint()
    finally:
        runtime.close()
    runtime_cold = _measure_runtime_cold_startup(seed_root, tier_root, samples=samples)
    legacy_migration = _measure_legacy_migrations(tier_root, spec, samples=samples)
    return {
        "fixture": {
            "spec": spec.to_dict(),
            "fixture_digest": fixture["fixture_digest"],
            "capability_count": len(fixture["capabilities"]),
            "adapter_channels": fixture["adapter_channels"],
            "current_v3_storage_owner": False,
        },
        "workload_digest": canonical_digest(workload_contract(spec)),
        "warm": warm,
        "runtime_cold": {"runtime_cold_startup": runtime_cold},
        "legacy_migration": legacy_migration,
        "storage_footprint_open": open_footprint,
        "storage_footprint_closed": _closed_storage_footprint(seed_root),
    }


def run_baseline(
    *,
    output_dir: Path | str | None = None,
    tiers: Sequence[str] = DEFAULT_TIERS,
    samples: int = DEFAULT_SAMPLES,
    warmup: int = DEFAULT_WARMUP,
    keep_artifacts: bool = False,
) -> dict[str, Any]:
    """Run selected tiers and return/write an isolated reproducible report."""

    normalized_tiers = tuple(str(item).strip().lower() for item in tiers if str(item).strip())
    if not normalized_tiers:
        raise ValueError("at least one tier is required")
    unknown = sorted(set(normalized_tiers).difference(TIER_SPECS))
    if unknown:
        raise ValueError(f"unknown benchmark tier(s): {', '.join(unknown)}")
    if samples < 1:
        raise ValueError("samples must be positive")
    if warmup < 0:
        raise ValueError("warmup must not be negative")
    selected = tuple(dict.fromkeys(normalized_tiers))
    output_path = None if output_dir is None else Path(output_dir)
    with _benchmark_workspace(output_path, keep_artifacts=keep_artifacts) as (workspace, retained):
        report = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": now_iso(),
            "commit": _git_commit(),
            "context": _context(),
            "execution": {
                "samples": samples,
                "warmup_per_sample": warmup,
                "runtime_root": str(workspace),
                "production_root": str(production_root()),
                "isolated_state": True,
                "production_mutated": False,
                "artifact_workspace_retained": retained,
                "cold_definition": "new isolated Runtime/SQLite root copied outside timing; OS page cache is not controlled",
                "migration_definition": "one bounded apply_storage_migrations batch against an isolated legacy fixture",
            },
            "tiers": {tier: _run_tier(workspace, TIER_SPECS[tier], samples=samples, warmup=warmup) for tier in selected},
            "limitations": [
                "This is a v2 runtime baseline; v3 capability contracts are not yet SQLite-owned.",
                "readiness_no_release intentionally has no deployment receipt and must not be read as production readiness.",
                "runtime_cold does not control the operating-system page cache.",
                "capability_replay_pack exercises the current fixed v2 replay case implementation only as a pre-refactor comparison point.",
            ],
        }
        report["budget_profile"] = build_budget_profile(report)
        report["report_path"] = str(workspace / "l5-v3-baseline.json") if retained else None
        report["report_digest"] = canonical_digest(
            {
                key: value
                for key, value in report.items()
                if key not in {"generated_at", "report_digest", "report_path"}
            }
        )
        if retained:
            report_path = Path(str(report["report_path"]))
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return report


def _parse_tiers(value: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in value.split(",") if item.strip())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / ".bench-artifacts")
    parser.add_argument("--tiers", default=",".join(DEFAULT_TIERS), help="comma-separated: small,medium,large")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--discard-artifacts", action="store_true", help="use a temporary workspace instead of --output-dir")
    args = parser.parse_args(argv)
    report = run_baseline(
        output_dir=None if args.discard_artifacts else args.output_dir,
        tiers=_parse_tiers(args.tiers),
        samples=args.samples,
        warmup=args.warmup,
        keep_artifacts=not args.discard_artifacts,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI.
    raise SystemExit(main())
