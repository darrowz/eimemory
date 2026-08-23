"""Real system diagnostics for ``eimemory doctor``.

Until 1.9.129 the ``doctor`` subcommand was a thin alias of ``status`` — both
called :func:`build_health_payload` and reported the loopback RPC supervisor
contract. 1.9.70 promised "System diagnostics" in the changelog, and this
module delivers it: SQLite integrity, storage footprint, JSONL health,
optional systemd timer liveness, sample record parsing, and a summarised
L5 readiness report, with an overall PASS/WARN/FAIL score.

Design notes
------------
* Pure read-only — no mutation, no persistence. The L5 readiness call is
  invoked with ``persist=False`` so ``eimemory doctor`` is safe to run as a
  scheduled check.
* Cross-platform. systemd probes are guarded by ``sys.platform == "linux"``
  plus ``importlib.util.find_spec``; on Windows / macOS the check returns
  ``SKIP`` with a clear reason.
* Reads only from ``runtime.store`` and the public ``eimemory.ops`` /
  ``eimemory.governance.l5_readiness`` APIs — never touches
  ``sqlite_store.py`` internals directly beyond ``runtime.store.sqlite``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


# Status tokens (kept short and stable for machine consumption)
PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

STATUS_RANK = {PASS: 0, SKIP: 1, WARN: 2, FAIL: 3}

# Auxiliary JSONL streams that share the runtime root. ``records.jsonl``
# itself is the primary log; the rest live under ``state/`` once a record
# is written. ``replay_manifests`` exists for completeness even though no
# current producer fills it.
JSONL_STREAMS: tuple[str, ...] = (
    "records",
    "events",
    "event_outcomes",
    "intent_patterns",
    "policy_rollout_ledger",
    "memory_edges",
    "replay_manifests",
)

# Expected schema migrations table population is a soft heuristic — anything
# fewer than 3 applied migrations on a populated DB is suspicious, anything
# 0 means the DB was never bootstrapped.
MIN_HEALTHY_MIGRATIONS = 3

# Upper bound on how many JSONL lines we sample for parse checks. Larger
# files would explode the runtime; the goal is to detect "all empty {}"
# regressions like the events.jsonl honxin bug.
JSONL_PARSE_SAMPLE_LIMIT = 10_000

# Largest log we will eagerly read into memory. Anything bigger is
# streamed line-by-line.
JSONL_STREAMING_THRESHOLD_BYTES = 8 * 1024 * 1024

# Number of latest records to sample for the record-sampling check.
RECORD_SAMPLE_SIZE = 3

# Free disk percentage below which we WARN. Below ``DISK_FAIL_PCT`` we
# FAIL — at that point writes will start failing.
DISK_WARN_PCT = 85.0
DISK_FAIL_PCT = 95.0

# WAL file size above which we WARN. 64 MiB mirrors the
# journal_size_limit in ``sqlite_store._configure_connection``.
WAL_WARN_BYTES = 64 * 1024 * 1024
WAL_FAIL_BYTES = 256 * 1024 * 1024


@dataclass
class CheckResult:
    """Result of a single doctor check."""

    status: str
    details: str
    recommendation: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status, "details": self.details}
        if self.recommendation:
            payload["recommendation"] = self.recommendation
        if self.metrics:
            payload["metrics"] = self.metrics
        return payload


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def _file_size(path: Path) -> int:
    try:
        return int(path.stat(follow_symlinks=False).st_size)
    except FileNotFoundError:
        return 0
    except OSError:
        return 0


def _dir_size_and_count(root: Path) -> tuple[int, int]:
    """Return ``(bytes, file_count)`` under ``root`` (recursive)."""

    total = 0
    count = 0
    if not root.exists():
        return 0, 0
    for current, _dirs, files in os.walk(root):
        for name in files:
            p = Path(current) / name
            try:
                total += int(p.stat(follow_symlinks=False).st_size)
                count += 1
            except OSError:
                continue
    return total, count


def _count_lines(path: Path) -> int:
    """Count newline-terminated records without loading the file."""

    if not path.exists():
        return 0
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size == 0:
        return 0
    if size <= JSONL_STREAMING_THRESHOLD_BYTES:
        try:
            with path.open("rb") as handle:
                return sum(1 for _ in handle)
        except OSError:
            return 0
    # Chunked counting for very large files.
    total = 0
    last_byte = b"\n"
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                total += chunk.count(b"\n")
                last_byte = chunk[-1:]
    except OSError:
        return 0
    if total and last_byte != b"\n":
        # Trailing partial line — still counts as a record.
        total += 1
    elif not total and last_byte and last_byte != b"\n":
        total = 1
    return total


def _head_parse_check(path: Path, sample_limit: int = JSONL_PARSE_SAMPLE_LIMIT) -> dict[str, Any]:
    """Read up to ``sample_limit`` non-empty lines and return parse stats.

    Returns a dict with ``head_parse_ok``, ``empty_dict_count``,
    ``non_empty_count``, ``sample_lines`` (int — actual lines scanned),
    ``first_error`` (str | None), and ``last_mtime`` (float epoch seconds).
    """

    out: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": _file_size(path),
        "lines": 0,
        "head_parse_ok": True,
        "first_error": None,
        "empty_dict_count": 0,
        "non_empty_count": 0,
        "blank_count": 0,
        "sample_lines": 0,
        "last_mtime": path.stat().st_mtime if path.exists() else 0.0,
    }
    if not path.exists():
        out["head_parse_ok"] = False
        out["first_error"] = "file missing"
        return out
    try:
        out["lines"] = _count_lines(path)
    except Exception as exc:  # pragma: no cover - defensive
        out["head_parse_ok"] = False
        out["first_error"] = f"line_count_error: {type(exc).__name__}"
        return out
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                if out["sample_lines"] >= sample_limit:
                    break
                stripped = raw.strip()
                if not stripped:
                    out["blank_count"] += 1
                    continue
                out["sample_lines"] += 1
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    out["head_parse_ok"] = False
                    if out["first_error"] is None:
                        out["first_error"] = f"line {out['sample_lines']}: {exc.msg}"
                    continue
                if isinstance(parsed, dict) and not parsed:
                    out["empty_dict_count"] += 1
                else:
                    out["non_empty_count"] += 1
    except OSError as exc:
        out["head_parse_ok"] = False
        if out["first_error"] is None:
            out["first_error"] = f"read_error: {type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_sqlite_integrity(runtime: Any) -> CheckResult:
    """PRAGMA integrity_check, foreign_key_check, migrations, WAL."""

    sqlite = getattr(getattr(runtime, "store", None), "sqlite", None)
    if sqlite is None:
        return CheckResult(SKIP, "no sqlite store on runtime", metrics={})
    conn = getattr(sqlite, "conn", None)
    if conn is None:
        return CheckResult(SKIP, "sqlite connection unavailable", metrics={})

    metrics: dict[str, Any] = {}

    # PRAGMA integrity_check — should return a single row containing "ok"
    try:
        rows = list(conn.execute("PRAGMA integrity_check").fetchall())
        integrity = [str(r[0]) for r in rows]
        metrics["integrity_check"] = integrity
    except Exception as exc:
        return CheckResult(FAIL, f"PRAGMA integrity_check raised {type(exc).__name__}: {exc}", metrics=metrics)
    if not integrity or integrity != ["ok"]:
        return CheckResult(
            FAIL,
            "PRAGMA integrity_check reported corruption: " + ", ".join(integrity[:5]),
            recommendation="Restore from a known-good snapshot or run a manual sqlite3 `.recover`.",
            metrics=metrics,
        )

    # PRAGMA foreign_key_check — empty result means clean
    try:
        fk_rows = [
            (str(r[0]), str(r[1]), str(r[2]), str(r[3]) if len(r) > 3 else "")
            for r in conn.execute("PRAGMA foreign_key_check").fetchall()
        ]
    except Exception as exc:
        return CheckResult(FAIL, f"PRAGMA foreign_key_check raised {type(exc).__name__}: {exc}", metrics=metrics)
    metrics["foreign_key_violations"] = fk_rows
    if fk_rows:
        return CheckResult(
            FAIL,
            f"PRAGMA foreign_key_check found {len(fk_rows)} violation(s); first: {fk_rows[0]}",
            recommendation="Investigate the referenced table/rowid and re-export the affected record.",
            metrics=metrics,
        )

    # schema_migrations drift — count applied vs. sqlite_master tables
    try:
        migrations = int(
            conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        )
    except Exception as exc:
        return CheckResult(
            FAIL,
            f"schema_migrations table unreadable: {type(exc).__name__}: {exc}",
            metrics=metrics,
        )
    metrics["applied_migrations"] = migrations
    if migrations == 0:
        # Newly created DBs may legitimately have 0 if nothing has been written
        # yet. Check whether records table is empty before failing.
        try:
            record_count = int(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])
        except Exception:
            record_count = -1
        if record_count > 0:
            return CheckResult(
                FAIL,
                f"schema_migrations is empty but records table has {record_count} rows — drift suspected",
                recommendation="Run the bootstrap migration or restore from a snapshot.",
                metrics=metrics,
            )
        # Bootstrap not yet run; this is WARN at worst.
        metrics["bootstrap_pending"] = True

    # Per-table row counts (so the report surfaces drift without an extra query)
    table_counts: dict[str, int] = {}
    for table in (
        "records",
        "events",
        "event_outcomes",
        "intent_patterns",
        "policy_rollout_ledger",
        "memory_edges",
        "schema_migrations",
    ):
        try:
            table_counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except Exception:
            table_counts[table] = -1
    metrics["table_counts"] = table_counts

    # WAL file health — based on filesystem sidecar
    db_path = Path(getattr(sqlite, "path", ""))
    wal_path = db_path.with_name(db_path.name + "-wal")
    shm_path = db_path.with_name(db_path.name + "-shm")
    wal_bytes = _file_size(wal_path)
    shm_bytes = _file_size(shm_path)
    metrics["wal_bytes"] = wal_bytes
    metrics["shm_bytes"] = shm_bytes

    if wal_bytes >= WAL_FAIL_BYTES:
        return CheckResult(
            FAIL,
            f"WAL file is {wal_bytes} bytes — write-ahead log is dangerously large",
            recommendation="Run `eimemory ops nightly` to checkpoint the WAL, or open the DB with `PRAGMA wal_checkpoint(TRUNCATE)`.",
            metrics=metrics,
        )
    if wal_bytes >= WAL_WARN_BYTES:
        # Read-only: a brief WARN is enough; PRAGMA wal_checkpoint is read-side-effect free
        # (PASSIVE mode) but we don't want doctor to mutate state, so we only report.
        if migrations >= MIN_HEALTHY_MIGRATIONS and not fk_rows:
            return CheckResult(
                WARN,
                f"WAL file is {wal_bytes} bytes (>{WAL_WARN_BYTES // (1024 * 1024)} MiB)",
                recommendation="Schedule a maintenance window to checkpoint the WAL.",
                metrics=metrics,
            )

    status = PASS
    details = f"integrity=ok, fk=clean, {migrations} migrations applied, {table_counts.get('records', 0)} records"
    if migrations == 0:
        status = WARN
        details += " — schema_migrations empty (bootstrap may not have run)"
    return CheckResult(status, details, metrics=metrics)


def check_storage_disk(runtime: Any) -> CheckResult:
    """EIMEMORY_ROOT disk usage + payload / release-snapshot / jsonl sizes."""

    root = Path(getattr(getattr(runtime, "store", None), "root", "")).resolve()
    if not root:
        return CheckResult(SKIP, "could not resolve EIMEMORY_ROOT")
    metrics: dict[str, Any] = {"root": str(root)}

    # Disk usage of the runtime root filesystem
    try:
        usage = shutil.disk_usage(root)
        used_pct = (usage.used / usage.total) * 100 if usage.total else 0.0
        metrics["disk_total_bytes"] = int(usage.total)
        metrics["disk_used_bytes"] = int(usage.used)
        metrics["disk_free_bytes"] = int(usage.free)
        metrics["disk_used_pct"] = round(used_pct, 2)
    except OSError as exc:
        return CheckResult(FAIL, f"disk_usage on {root} failed: {exc}", metrics=metrics)

    # payload_segments
    payload_root = root / "state" / "payload_segments"
    payload_bytes, payload_count = _dir_size_and_count(payload_root)
    metrics["payload_segments"] = {
        "path": str(payload_root),
        "bytes": payload_bytes,
        "file_count": payload_count,
        "exists": payload_root.exists(),
    }

    # records.jsonl + segments
    records_dir = root
    records_jsonl = records_dir / "records.jsonl"
    record_segs = sorted(
        [
            p
            for p in records_dir.iterdir()
            if p.name.startswith("records.segment-") and p.suffix == ".jsonl"
        ]
    ) if records_dir.exists() else []
    legacy_segs = sorted(records_dir.glob("records.[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].jsonl")) if records_dir.exists() else []
    records_segments_bytes = sum(_file_size(p) for p in (*record_segs, *legacy_segs, records_jsonl))
    metrics["records_jsonl"] = {
        "active_bytes": _file_size(records_jsonl),
        "segment_count": len(record_segs) + len(legacy_segs),
        "total_bytes": records_segments_bytes,
    }

    # release-snapshots
    snapshots_root = root / "state" / "release-snapshots"
    snap_count = 0
    snap_bytes = 0
    if snapshots_root.exists():
        for entry in snapshots_root.iterdir():
            if entry.is_dir():
                snap_count += 1
                size, _ = _dir_size_and_count(entry)
                snap_bytes += size
            else:
                snap_count += 1
                snap_bytes += _file_size(entry)
    metrics["release_snapshots"] = {
        "path": str(snapshots_root),
        "count": snap_count,
        "bytes": snap_bytes,
        "exists": snapshots_root.exists(),
    }

    # state/ total (catches anything we forgot)
    state_bytes, state_count = _dir_size_and_count(root / "state")
    metrics["state_dir"] = {"bytes": state_bytes, "file_count": state_count}

    # Verdict
    status = PASS
    notes: list[str] = []
    if used_pct >= DISK_FAIL_PCT:
        status = FAIL
        notes.append(
            f"disk {used_pct:.1f}% used (>= {DISK_FAIL_PCT:.0f}%) — writes may fail"
        )
    elif used_pct >= DISK_WARN_PCT:
        status = WARN
        notes.append(
            f"disk {used_pct:.1f}% used (>= {DISK_WARN_PCT:.0f}%)"
        )
    if snap_count > 50:
        # Heuristic: 50+ release snapshots is a sign we are leaking space
        if status == PASS:
            status = WARN
        notes.append(f"{snap_count} release-snapshots — review retention")
    details = "; ".join(notes) if notes else (
        f"disk {used_pct:.1f}% used, "
        f"payload_segments={payload_count} files ({payload_bytes // (1024 * 1024)} MiB), "
        f"records.jsonl+segments={records_segments_bytes // (1024 * 1024)} MiB, "
        f"release-snapshots={snap_count}"
    )
    recommendation = ""
    if status == WARN and used_pct >= DISK_WARN_PCT:
        recommendation = "Prune release-snapshots and rotate the payload archive."
    elif status == FAIL:
        recommendation = "Free disk immediately — eimemory writes will start failing."
    return CheckResult(status, details, recommendation=recommendation, metrics=metrics)


def check_jsonl_health(runtime: Any) -> CheckResult:
    """Per-stream line count + parse sanity."""

    root = Path(getattr(getattr(runtime, "store", None), "root", ""))
    state_dir = root / "state"
    per_stream: dict[str, Any] = {}
    for name in JSONL_STREAMS:
        # records.jsonl lives at root; auxiliary streams under state/
        candidate = (root if name == "records" else state_dir) / f"{name}.jsonl"
        per_stream[name] = _head_parse_check(candidate)

    fails: list[str] = []
    warns: list[str] = []
    for name, info in per_stream.items():
        if not info["exists"]:
            # Not yet written is benign; not a failure
            continue
        if not info["head_parse_ok"]:
            fails.append(f"{name}.jsonl parse error: {info['first_error']}")
            continue
        # "All empty dicts" bug detection — only when we have data and parsed samples
        if info["sample_lines"] > 0 and info["non_empty_count"] == 0 and info["empty_dict_count"] > 0:
            fails.append(
                f"{name}.jsonl: {info['empty_dict_count']}/{info['sample_lines']} sampled lines are empty {{}}"
            )
            continue
        # Heuristic: a stream with >0 lines but every single sampled line is blank
        if info["lines"] > 100 and info["sample_lines"] == 0 and info["blank_count"] > 0:
            fails.append(f"{name}.jsonl: {info['lines']} lines but every sampled line is blank")

    status = PASS
    details_parts: list[str] = []
    for name, info in per_stream.items():
        details_parts.append(
            f"{name}={info['lines']}L"
            + (f"({info['empty_dict_count']}∅)" if info["empty_dict_count"] else "")
        )
    details = ", ".join(details_parts) or "no JSONL streams present"
    recommendation = ""
    if fails:
        status = FAIL
        details = "; ".join(fails)
        recommendation = (
            "Investigate the writer for the failing stream — empty dict writes usually "
            "indicate a payload-construction bug. The events.jsonl honxin regression is the canonical example."
        )
    elif warns:
        status = WARN
        details = "; ".join(warns) + " | " + details

    return CheckResult(status, details, recommendation=recommendation, metrics={"streams": per_stream})


def check_systemd_services(runtime: Any, scope: Mapping[str, Any]) -> CheckResult:
    """Best-effort systemd timer liveness. SKIP on non-Linux / not configured."""

    if sys.platform != "linux":
        return CheckResult(
            SKIP,
            f"systemd timers only meaningful on Linux (current platform: {sys.platform})",
            metrics={"platform": sys.platform},
        )
    if importlib.util.find_spec("eimemory.ops.timer_monitor") is None:
        return CheckResult(SKIP, "eimemory.ops.timer_monitor module is not importable", metrics={})
    try:
        from eimemory.ops.timer_monitor import check_user_systemd_timers  # type: ignore
    except Exception as exc:  # pragma: no cover - defensive
        return CheckResult(SKIP, f"timer monitor import failed: {type(exc).__name__}: {exc}", metrics={})

    try:
        report = check_user_systemd_timers(
            runtime,
            scope=dict(scope) if scope else {},
            stale_after_minutes=60,
            include_legacy_learning_timers=True,
            persist=False,
        )
    except Exception as exc:
        return CheckResult(
            WARN,
            f"check_user_systemd_timers raised {type(exc).__name__}: {exc}",
            metrics={},
        )
    if not isinstance(report, dict):
        return CheckResult(SKIP, "timer monitor returned a non-dict report", metrics={"report_type": type(report).__name__})

    timers = report.get("timers") or report.get("units") or []
    if not isinstance(timers, list):
        return CheckResult(SKIP, "timer monitor report missing a list of timers", metrics=report)

    live = sum(1 for t in timers if isinstance(t, dict) and t.get("active") is True)
    dead = sum(1 for t in timers if isinstance(t, dict) and t.get("active") is False)
    total = live + dead
    metrics = {"total": total, "live": live, "dead": dead, "report_ok": report.get("ok")}
    if total == 0:
        return CheckResult(SKIP, "no systemd timers reported by timer-monitor", metrics=metrics)
    if dead == 0:
        return CheckResult(PASS, f"{live}/{total} systemd timers active", metrics=metrics)
    if live > 0:
        return CheckResult(
            WARN,
            f"{dead}/{total} systemd timers inactive (last seen: {timers[0].get('last_trigger') if timers else 'unknown'})",
            recommendation="systemctl --user reset-failed <timer> && systemctl --user start <timer>",
            metrics=metrics,
        )
    return CheckResult(
        FAIL,
        f"All {total} systemd timers are inactive",
        recommendation="Restart the user systemd instance: `systemctl --user daemon-reload && systemctl --user start eimemory-nightly.timer`",
        metrics=metrics,
    )


def check_code_implementation_owner(runtime: Any) -> CheckResult:
    """Inspect the exact production authority and release-owned refresh timer."""

    from eimemory.ops.code_implementation_owner import inspect_code_implementation_owner

    try:
        report = inspect_code_implementation_owner(runtime)
    except Exception as exc:  # pragma: no cover - defensive doctor boundary
        return CheckResult(
            FAIL,
            f"code-implementation owner inspection failed: {type(exc).__name__}",
            recommendation="Run `eimemory ops code-implementation-status --json`.",
        )
    authority = report.get("authority") if isinstance(report.get("authority"), Mapping) else {}
    catalog = report.get("catalog") if isinstance(report.get("catalog"), Mapping) else {}
    advertisement = report.get("advertisement") if isinstance(report.get("advertisement"), Mapping) else {}
    safety = report.get("safety") if isinstance(report.get("safety"), Mapping) else {}
    metrics = {
        "authority_root": str(authority.get("root") or ""),
        "authority_matches_runtime": authority.get("matches_runtime") is True,
        "refresh_ready": report.get("refresh_ready") is True,
        "provider_reader_ready": report.get("provider_reader_ready") is True,
        "advertisement_fresh": advertisement.get("fresh") is True,
        "catalog_status": str(catalog.get("status") or "unknown"),
        "catalog_valid_passes": int(catalog.get("valid_passes") or 0),
        "kill_switch_present": safety.get("kill_switch_present") is True,
        "automation_policy_present": safety.get("automation_policy_present") is True,
        "effects_fail_closed": safety.get("effects_fail_closed") is True,
        "timer_owner": dict(report.get("timer_owner") or {}),
    }
    if report.get("reason") == "authority_runtime_root_mismatch":
        return CheckResult(
            FAIL,
            "runtime root does not match the production code-implementation authority",
            recommendation="Set EIMEMORY_ROOT to the authoritative production store and rerun doctor.",
            metrics=metrics,
        )
    if report.get("ok") is True:
        return CheckResult(
            PASS,
            "exact v2 provider, fresh advertisement, catalog receipts, and timer owner are ready",
            metrics=metrics,
        )
    return CheckResult(
        WARN,
        "code-implementation lifecycle is waiting on one or more live/durable prerequisites",
        recommendation="Run `eimemory ops code-implementation-status --json` for the bounded evidence view.",
        metrics=metrics,
    )


def check_record_sampling(runtime: Any, scope: Mapping[str, Any]) -> CheckResult:
    """Pull the latest ``RECORD_SAMPLE_SIZE`` records and parse each one."""

    try:
        records = runtime.store.list_records(limit=RECORD_SAMPLE_SIZE)  # type: ignore[attr-defined]
    except Exception as exc:
        return CheckResult(
            SKIP,
            f"runtime.store.list_records raised {type(exc).__name__}: {exc}",
            metrics={},
        )
    if not records:
        return CheckResult(SKIP, "no records present in the store", metrics={"sampled": 0})

    from eimemory.models.records import RecordEnvelope  # local import — keeps module loadable in test contexts

    sampled: list[dict[str, Any]] = []
    parse_failures: list[str] = []
    meta_failures: list[str] = []
    for record in records[:RECORD_SAMPLE_SIZE]:
        record_id = getattr(record, "record_id", "<no-id>")
        try:
            payload = record.to_dict() if hasattr(record, "to_dict") else dict(record)
        except Exception as exc:
            parse_failures.append(f"{record_id}: to_dict failed: {type(exc).__name__}")
            continue
        # Round-trip parse to confirm schema integrity
        try:
            RecordEnvelope.from_dict(payload)
        except Exception as exc:
            parse_failures.append(f"{record_id}: from_dict failed: {type(exc).__name__}: {exc}")
            continue
        # business_meta integrity
        meta = payload.get("meta") if isinstance(payload, dict) else None
        if not isinstance(meta, dict):
            meta_failures.append(f"{record_id}: meta is {type(meta).__name__}, expected dict")
        sampled.append(
            {
                "record_id": record_id,
                "kind": payload.get("kind"),
                "has_meta": isinstance(meta, dict),
                "meta_keys": sorted(list(meta.keys()))[:8] if isinstance(meta, dict) else [],
                "updated_at": payload.get("time", {}).get("updated_at") if isinstance(payload.get("time"), dict) else None,
            }
        )

    metrics = {"sampled": len(sampled), "parse_failures": parse_failures, "meta_failures": meta_failures}
    if parse_failures:
        return CheckResult(
            FAIL,
            f"{len(parse_failures)}/{len(records)} sampled records failed to round-trip parse",
            recommendation="Inspect the failed record(s) and either repair the on-disk payload or re-insert.",
            metrics=metrics,
        )
    if meta_failures:
        return CheckResult(
            WARN,
            f"{len(meta_failures)}/{len(records)} sampled records have non-dict meta",
            metrics=metrics,
        )
    return CheckResult(
        PASS,
        f"sampled {len(sampled)} latest records; all parseable; meta dict-shaped",
        metrics=metrics,
    )


def check_l5_readiness(runtime: Any, scope: Mapping[str, Any]) -> CheckResult:
    """Summarise the L5 readiness report (no persistence)."""

    try:
        report = runtime.build_l5_readiness_report(  # type: ignore[attr-defined]
            scope=dict(scope) if scope else {},
            persist=False,
            limit=200,
            loop_id="cli_doctor",
        )
    except Exception as exc:
        return CheckResult(
            SKIP,
            f"build_l5_readiness_report raised {type(exc).__name__}: {exc}",
            metrics={},
        )
    if not isinstance(report, dict):
        return CheckResult(SKIP, "L5 readiness returned a non-dict", metrics={"type": type(report).__name__})

    if str(report.get("schema_version") or "") == "l5_readiness.v3":
        assessment = report.get("assessment") if isinstance(report.get("assessment"), dict) else {}
        metrics = {
            "reader_mode": str(report.get("reader_mode") or "v3"),
            "profile_key": str(report.get("profile_key") or ""),
            "loop_maturity": str(report.get("loop_maturity") or ""),
            "capability_ready": bool(report.get("capability_ready")),
            "adapter_ready": bool(report.get("adapter_ready")),
            "deployment_ready": bool(report.get("deployment_ready")),
            "gap_count": len(report.get("gaps") or []),
            "assessment_status": str(assessment.get("status") or report.get("status") or ""),
        }
        if bool(report.get("ok")):
            return CheckResult(
                PASS,
                "L5 v3 axes are independently evidenced and ready",
                metrics=metrics,
            )
        return CheckResult(
            WARN,
            "L5 v3 is not ready; inspect independent capability, adapter, and deployment axes",
            recommendation="Review `eimemory learn l5-readiness --reader-mode v3 --profile <profile> --json`.",
            metrics=metrics,
        )

    observed_stage = str(report.get("observed_stage", ""))
    current_stage = str(report.get("current_stage", ""))
    score = float(report.get("readiness_score") or 0.0)

    gates = {
        "live_task_gate": report.get("live_task_gate"),
        "real_business_gate": report.get("real_business_gate"),
        "production_recall_gate": report.get("production_recall_gate"),
        "production_recall_strict_state": report.get("production_recall_strict_state"),
    }
    gate_summary: dict[str, dict[str, Any]] = {}
    for name, gate in gates.items():
        if isinstance(gate, dict):
            ok = gate.get("ok") is True
            status = gate.get("status", "")
            gate_summary[name] = {"ok": ok, "status": status}
        else:
            gate_summary[name] = {"ok": False, "status": "missing"}

    open_gates = [name for name, info in gate_summary.items() if not info["ok"]]
    transition = str(report.get("maturity_transition", ""))
    metrics = {
        "observed_stage": observed_stage,
        "current_stage": current_stage,
        "readiness_score": score,
        "gates": gate_summary,
        "open_gates": open_gates,
        "maturity_transition": transition,
    }

    # Status policy: any closed gate + maturity_transition=held -> WARN
    if current_stage == "L5" and not open_gates:
        return CheckResult(PASS, f"observed={observed_stage}, current={current_stage}, score={score}, all gates open", metrics=metrics)
    if transition == "held":
        return CheckResult(
            WARN,
            f"observed={observed_stage}, current={current_stage}, score={score}, maturity_transition=held; open gates: {open_gates or 'none'}",
            recommendation="Review next_actions from the full L5 readiness report: `eimemory learn l5-readiness --json`.",
            metrics=metrics,
        )
    if observed_stage and observed_stage != current_stage:
        return CheckResult(
            WARN,
            f"observed={observed_stage} but current={current_stage}; score={score}; open gates: {open_gates or 'none'}",
            metrics=metrics,
        )
    if open_gates:
        return CheckResult(
            WARN,
            f"current={current_stage}, score={score}, open gates: {open_gates}",
            metrics=metrics,
        )
    return CheckResult(PASS, f"current={current_stage}, score={score}, no open gates", metrics=metrics)


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------


def _overall_status(checks: Mapping[str, CheckResult]) -> str:
    if any(c.status == FAIL for c in checks.values()):
        return "UNHEALTHY"
    if any(c.status == WARN for c in checks.values()):
        return "DEGRADED"
    if checks and all(c.status == SKIP for c in checks.values()):
        return "UNKNOWN"
    return "HEALTHY"


def _collect_recommendations(checks: Mapping[str, CheckResult]) -> list[str]:
    out: list[str] = []
    for name, check in checks.items():
        if not check.recommendation:
            continue
        out.append(f"[{name}] {check.recommendation}")
    return out


def run_doctor(
    runtime: Any,
    *,
    scope: Mapping[str, Any] | None = None,
    include_l5: bool = True,
    include_systemd: bool = True,
) -> dict[str, Any]:
    """Run every doctor check and return a structured report."""

    from eimemory.adapters.eibrain.rpc_server import build_health_payload
    from eimemory.config.loader import load_settings
    from eimemory.governance.supervisor import build_supervisor_contract

    effective_scope: dict[str, Any] = dict(scope) if scope else {}

    checks: dict[str, CheckResult] = {}
    checks["sqlite_integrity"] = check_sqlite_integrity(runtime)
    checks["storage_disk"] = check_storage_disk(runtime)
    checks["jsonl_health"] = check_jsonl_health(runtime)
    checks["record_sampling"] = check_record_sampling(runtime, effective_scope)
    if include_systemd:
        checks["systemd_services"] = check_systemd_services(runtime, effective_scope)
        checks["code_implementation_owner"] = check_code_implementation_owner(runtime)
    if include_l5:
        checks["l5_readiness"] = check_l5_readiness(runtime, effective_scope)

    overall = _overall_status(checks)
    recommendations = _collect_recommendations(checks)
    settings = load_settings()
    health = build_health_payload(
        runtime,
        listen_host=settings.rpc_host,
        listen_port=int(settings.rpc_port),
    )
    return {
        **health,
        "ok": bool(health.get("ok")) and overall in {"HEALTHY", "DEGRADED", "UNKNOWN"},
        "report_type": "doctor_report",
        "schema_version": "doctor.v1",
        "generated_at": time.time(),
        "root": str(getattr(getattr(runtime, "store", None), "root", "")),
        "overall_status": overall,
        "checks": {
            **dict(health.get("checks") or {}),
            **{name: check.to_dict() for name, check in checks.items()},
        },
        "supervisor": build_supervisor_contract(runtime, scope=effective_scope),
        "recommendations": recommendations,
    }


# ---------------------------------------------------------------------------
# Human-readable rendering
# ---------------------------------------------------------------------------


_GLYPHS = {
    PASS: "\u2705",   # ✅
    WARN: "\u26a0\ufe0f",  # ⚠️
    FAIL: "\u274c",   # ❌
    SKIP: "\u23ed\ufe0f",  # ⏭️
}


def render_human(report: Mapping[str, Any]) -> str:
    """Render the report as a multi-line text block (with emoji)."""

    lines: list[str] = []
    overall = str(report.get("overall_status", "UNKNOWN"))
    overall_glyph = {
        "HEALTHY": "\u2705",
        "DEGRADED": "\u26a0\ufe0f",
        "UNHEALTHY": "\u274c",
    }.get(overall, "\u2753")
    lines.append(f"{overall_glyph} eimemory doctor — overall: {overall}")
    lines.append("")
    checks = report.get("checks") or {}
    for name in (
        "sqlite_integrity",
        "storage_disk",
        "jsonl_health",
        "systemd_services",
        "code_implementation_owner",
        "record_sampling",
        "l5_readiness",
    ):
        if name not in checks:
            continue
        info = checks[name]
        glyph = _GLYPHS.get(str(info.get("status")), "\u2026")
        lines.append(f"  {glyph} {name}: {info.get('status', '?')} — {info.get('details', '')}")
        if info.get("recommendation"):
            lines.append(f"      \u21b3 {info['recommendation']}")
    recs = report.get("recommendations") or []
    if recs:
        lines.append("")
        lines.append("Recommendations:")
        for i, rec in enumerate(recs, start=1):
            lines.append(f"  {i}. {rec}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m eimemory.cli.doctor`` and similar."""

    from eimemory.api.runtime import Runtime
    from eimemory.config.defaults import default_root

    args = list(argv if argv is not None else sys.argv[1:])
    as_json = "--json" in args
    as_human = "--human" in args or not as_json  # default to human when JSON not requested
    skip_l5 = "--no-l5" in args
    skip_systemd = "--no-systemd" in args
    runtime = Runtime.create(root=default_root(None))
    try:
        report = run_doctor(
            runtime,
            scope={},
            include_l5=not skip_l5,
            include_systemd=not skip_systemd,
        )
    finally:
        try:
            runtime.close()
        except Exception:
            pass

    if as_human:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
        print(render_human(report))
    if as_json:
        if as_human:
            print()  # blank line between human block and JSON
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("overall_status") in {"HEALTHY", "DEGRADED", "UNKNOWN"} else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
