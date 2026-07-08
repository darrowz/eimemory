from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote


DEFAULT_MODEL = "gpt-5.5"
DEFAULT_FALLBACK_MODEL = "MiniMax-M3"
REPORT_TYPE = "reflective_replay_pilot"


ModelExecutor = Callable[[str, str], str]
SleepFn = Callable[[float], None]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_minimax_api_key() -> str | None:
    """Return a MiniMax key from explicit process env only.

    Reflective production analysis must not know how OpenClaw stores provider
    secrets. Operators can inject a short-lived key through the environment
    when they intentionally opt into MiniMax fallback.
    """
    for name in ("EIMEMORY_MINIMAX_API_KEY", "MINIMAX_API_KEY"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def connect_readonly(path: Path) -> sqlite3.Connection:
    uri_path = path.resolve().as_posix()
    uri = f"file:{quote(uri_path, safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _parse_iso(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _cutoff_iso(*, source_snapshot_at: str | None, since_days: int) -> str:
    snapshot = _parse_iso(str(source_snapshot_at or "")) or datetime.now(timezone.utc)
    if snapshot.tzinfo is None:
        snapshot = snapshot.replace(tzinfo=timezone.utc)
    return (snapshot - timedelta(days=max(1, int(since_days or 1)))).isoformat()


def detect_source_snapshot_at(conn: sqlite3.Connection) -> str:
    candidates: list[str] = []
    if table_exists(conn, "event_outcomes"):
        row = conn.execute("SELECT MAX(recorded_at) AS value FROM event_outcomes").fetchone()
        if row and row["value"]:
            candidates.append(str(row["value"]))
    if table_exists(conn, "recall_index"):
        row = conn.execute("SELECT MAX(updated_at) AS value FROM recall_index").fetchone()
        if row and row["value"]:
            candidates.append(str(row["value"]))
    return max(candidates) if candidates else now_iso()


def select_replay_cases(
    conn: sqlite3.Connection,
    *,
    limit: int = 25,
    context_limit: int = 20,
    capability_limit: int = 5,
    source_snapshot_at: str | None = None,
    since_days: int = 7,
) -> list[dict[str, Any]]:
    max_cases = max(1, int(limit or 1))
    cutoff = _cutoff_iso(source_snapshot_at=source_snapshot_at, since_days=since_days)
    cases: list[dict[str, Any]] = []
    selected_event_ids: set[str] = set()
    selected_case_ids: set[str] = set()

    if table_exists(conn, "event_outcomes") and context_limit > 0:
        rows = conn.execute(
            """
            SELECT id, event_id, reason, correction_from_user, policy_update, payload_json, recorded_at
            FROM event_outcomes
            WHERE lower(outcome) = 'bad'
              AND recorded_at >= ?
              AND lower(reason) LIKE '%context%'
              AND lower(reason) LIKE '%overflow%'
            ORDER BY recorded_at DESC, id DESC
            LIMIT ?
            """,
            (cutoff, min(max_cases, int(context_limit))),
        ).fetchall()
        for row in rows:
            case = _event_outcome_case(row, source="event_outcome.context_overflow")
            cases.append(case)
            selected_event_ids.add(str(row["id"]))
            selected_case_ids.add(str(case["id"]))
            if len(cases) >= max_cases:
                return cases[:max_cases]

    if table_exists(conn, "recall_index") and capability_limit > 0 and len(cases) < max_cases:
        rows = conn.execute(
            """
            SELECT storage_key, record_id, kind, title_text, body_text, anchor_terms, quality_score, updated_at
            FROM recall_index
            WHERE updated_at >= ?
              AND (
                kind IN ('capability_candidate', 'replay_result')
                OR lower(title_text || ' ' || body_text || ' ' || anchor_terms) LIKE '%capability%'
                OR lower(title_text || ' ' || body_text || ' ' || anchor_terms) LIKE '%replay%'
              )
            ORDER BY quality_score ASC, updated_at DESC, storage_key DESC
            LIMIT ?
            """,
            (cutoff, min(max_cases - len(cases), int(capability_limit))),
        ).fetchall()
        for row in rows:
            case = _recall_index_case(row)
            if str(case["id"]) in selected_case_ids:
                continue
            cases.append(case)
            selected_case_ids.add(str(case["id"]))
            if len(cases) >= max_cases:
                return cases[:max_cases]

    if table_exists(conn, "event_outcomes") and len(cases) < max_cases:
        remaining = max_cases - len(cases)
        where = [
            "lower(outcome) = 'bad'",
            "recorded_at >= ?",
        ]
        params: list[Any] = [cutoff]
        if selected_event_ids:
            placeholders = ", ".join("?" for _ in selected_event_ids)
            where.append(f"id NOT IN ({placeholders})")
            params.extend(sorted(selected_event_ids))
        params.append(remaining)
        rows = conn.execute(
            f"""
            SELECT id, event_id, reason, correction_from_user, policy_update, payload_json, recorded_at
            FROM event_outcomes
            WHERE {' AND '.join(where)}
            ORDER BY recorded_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        for row in rows:
            case = _event_outcome_case(row, source="event_outcome.top_up")
            if str(case["id"]) in selected_case_ids:
                continue
            cases.append(case)
            selected_case_ids.add(str(case["id"]))
            if len(cases) >= max_cases:
                break

    return cases[:max_cases]


def _event_outcome_case(row: sqlite3.Row, *, source: str) -> dict[str, Any]:
    payload = _json_object(str(row["payload_json"] or "{}"))
    return {
        "id": str(row["id"]),
        "event_id": str(row["event_id"]),
        "source": source,
        "reason": str(row["reason"] or ""),
        "correction_from_user": str(row["correction_from_user"] or ""),
        "policy_update": str(row["policy_update"] or ""),
        "recorded_at": str(row["recorded_at"] or ""),
        "payload": payload,
    }


def _recall_index_case(row: sqlite3.Row) -> dict[str, Any]:
    body = str(row["body_text"] or "")
    title = str(row["title_text"] or "")
    return {
        "id": str(row["storage_key"]),
        "record_id": str(row["record_id"]),
        "source": "recall_index.capability",
        "kind": str(row["kind"] or ""),
        "reason": "capability evidence/replay candidate",
        "title": title,
        "body": body[:4000],
        "anchor_terms": str(row["anchor_terms"] or ""),
        "quality_score": float(row["quality_score"] or 0.0),
        "recorded_at": str(row["updated_at"] or ""),
    }


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def build_prompt(case: dict[str, Any]) -> str:
    payload = {
        "task": "Analyze this eimemory/OpenClaw failure as a reflective replay case.",
        "rules": [
            "Use only the provided evidence.",
            "Do not invent current production facts.",
            "Return concise JSON with root_cause, contributing_factors, fix, verification.",
        ],
        "case": case,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def is_rate_limit_error(error: BaseException) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in ("429", "rate limit", "ratelimit", "too many requests", "cooldown"))


def execute_with_retries(
    *,
    model: str,
    prompt: str,
    executor: ModelExecutor,
    retries: int,
    cooldown_seconds: float,
    sleep: SleepFn = time.sleep,
) -> str:
    attempts = max(1, int(retries or 0) + 1)
    last_error: BaseException | None = None
    for attempt in range(attempts):
        try:
            return executor(model, prompt)
        except Exception as exc:  # pragma: no cover - exact subprocess errors vary by platform
            last_error = exc
            if attempt >= attempts - 1 or not is_rate_limit_error(exc):
                break
            if cooldown_seconds > 0:
                sleep(float(cooldown_seconds))
    assert last_error is not None
    raise last_error


def analyze_case(
    case: dict[str, Any],
    *,
    model: str = DEFAULT_MODEL,
    fallback_model: str = DEFAULT_FALLBACK_MODEL,
    allow_fallback_minimax: bool = False,
    executor: ModelExecutor | None = None,
    primary_retries: int = 0,
    rate_limit_cooldown_seconds: float = 0.0,
    sleep: SleepFn = time.sleep,
) -> dict[str, Any]:
    run = executor or codex_exec
    prompt = build_prompt(case)
    try:
        output = execute_with_retries(
            model=model,
            prompt=prompt,
            executor=run,
            retries=primary_retries,
            cooldown_seconds=rate_limit_cooldown_seconds,
            sleep=sleep,
        )
        return _analysis_result(case, model_used=model, root_cause=output, fallback_used=False)
    except Exception as exc:
        primary_error = str(exc)

    if not allow_fallback_minimax:
        return {
            "case_id": str(case.get("id") or ""),
            "status": "skipped",
            "skipped_reason": "primary_model_failed",
            "model_requested": model,
            "model_used": None,
            "fallback_used": False,
            "root_cause": "",
            "primary_error": primary_error,
        }

    if "minimax" in str(fallback_model or "").lower() and load_minimax_api_key() is None:
        return {
            "case_id": str(case.get("id") or ""),
            "status": "skipped",
            "skipped_reason": "fallback_minimax_key_missing",
            "model_requested": model,
            "model_used": None,
            "fallback_model": fallback_model,
            "fallback_used": False,
            "root_cause": "",
            "primary_error": primary_error,
        }

    try:
        output = run(fallback_model, prompt)
    except Exception as exc:
        return {
            "case_id": str(case.get("id") or ""),
            "status": "skipped",
            "skipped_reason": "fallback_model_failed",
            "model_requested": model,
            "model_used": None,
            "fallback_model": fallback_model,
            "fallback_used": True,
            "root_cause": "",
            "primary_error": primary_error,
            "fallback_error": str(exc),
        }
    result = _analysis_result(case, model_used=fallback_model, root_cause=output, fallback_used=True)
    result["model_requested"] = model
    result["primary_error"] = primary_error
    return result


def _analysis_result(
    case: dict[str, Any],
    *,
    model_used: str,
    root_cause: str,
    fallback_used: bool,
) -> dict[str, Any]:
    return {
        "case_id": str(case.get("id") or ""),
        "status": "ok",
        "skipped_reason": "",
        "model_used": model_used,
        "fallback_used": bool(fallback_used),
        "root_cause": str(root_cause or "").strip(),
    }


def codex_exec(model: str, prompt: str) -> str:
    result = subprocess.run(
        ["codex", "exec", "--model", model, "-"],
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if result.returncode != 0:
        stderr = str(result.stderr or "").strip()
        stdout = str(result.stdout or "").strip()
        message = stderr or stdout or f"codex exec failed with exit {result.returncode}"
        raise RuntimeError(message)
    return str(result.stdout or "").strip()


def build_report(
    *,
    cases: list[dict[str, Any]],
    analyses: list[dict[str, Any]],
    generated_at: str,
    source_snapshot_at: str,
    model: str,
    fallback_model: str,
    allow_fallback_minimax: bool,
) -> dict[str, Any]:
    model_usage: dict[str, int] = {}
    skipped_count = 0
    for item in analyses:
        model_used = item.get("model_used")
        if model_used:
            model_usage[str(model_used)] = model_usage.get(str(model_used), 0) + 1
        if str(item.get("status") or "") == "skipped":
            skipped_count += 1
    model_usage.setdefault(model, 0)
    model_usage.setdefault(fallback_model, 0)
    return {
        "report_type": REPORT_TYPE,
        "generated_at": generated_at,
        "source_snapshot_at": source_snapshot_at,
        "case_count": len(cases),
        "analysis_count": len(analyses),
        "skipped_count": skipped_count,
        "model_requested": model,
        "fallback_model": fallback_model,
        "allow_fallback_minimax": bool(allow_fallback_minimax),
        "fail_closed": True,
        "model_usage": model_usage,
        "cases": cases,
        "analyses": analyses,
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    model_usage = dict(report.get("model_usage") or {})
    cases = list(report.get("cases") or [])
    analyses = list(report.get("analyses") or [])
    lines = [
        "# Reflective Replay Pilot",
        "",
        f"generated_at: {report.get('generated_at') or ''}",
        f"source_snapshot_at: {report.get('source_snapshot_at') or ''}",
        f"report_type: {report.get('report_type') or REPORT_TYPE}",
        f"case_count: {report.get('case_count', len(cases))}",
        f"skipped_count: {report.get('skipped_count', 0)}",
        f"fail_closed: {str(bool(report.get('fail_closed', True))).lower()}",
        "",
        "## Model Usage",
        "",
    ]
    for name in sorted(model_usage):
        lines.append(f"- {name}: {model_usage[name]}")
    lines.extend(["", "## Cases", ""])
    if not cases:
        lines.append("- No cases selected.")
    for case in cases:
        lines.append(f"- {case.get('id')}: {case.get('source')} - {case.get('reason')}")
    lines.extend(["", "## Analyses", ""])
    if not analyses:
        lines.append("- No analyses were run.")
    for item in analyses:
        status = str(item.get("status") or "")
        case_id = str(item.get("case_id") or "")
        model_used = item.get("model_used") or "none"
        lines.append(f"- {case_id}: {status}, model={model_used}, skipped_reason={item.get('skipped_reason') or ''}")
        root_cause = str(item.get("root_cause") or "").strip()
        if root_cause:
            lines.append(f"  root_cause: {root_cause[:500]}")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a fail-closed reflective replay pilot over eimemory SQLite data.")
    parser.add_argument("--db", type=Path, default=Path("data/eimemory.sqlite"), help="Path to production SQLite DB.")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--context-limit", type=int, default=20)
    parser.add_argument("--capability-limit", type=int, default=5)
    parser.add_argument("--since-days", type=int, default=7)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--fallback-model", default=DEFAULT_FALLBACK_MODEL)
    parser.add_argument("--allow-fallback-minimax", action="store_true")
    parser.add_argument("--primary-retries", type=int, default=2)
    parser.add_argument("--rate-limit-cooldown-seconds", type=float, default=30.0)
    parser.add_argument("--source-snapshot-at", default="")
    parser.add_argument("--output", type=Path, default=Path(".pilot/reflective_replay_pilot.md"))
    parser.add_argument("--json-output", type=Path, default=Path(".pilot/reflective_replay_pilot.json"))
    parser.add_argument("--select-only", action="store_true", help="Select cases and write report without model calls.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        conn = connect_readonly(args.db)
    except sqlite3.Error as exc:
        print(f"failed to open DB read-only: {exc}", file=sys.stderr)
        return 2
    with conn:
        source_snapshot_at = args.source_snapshot_at or detect_source_snapshot_at(conn)
        cases = select_replay_cases(
            conn,
            limit=args.limit,
            context_limit=args.context_limit,
            capability_limit=args.capability_limit,
            source_snapshot_at=source_snapshot_at,
            since_days=args.since_days,
        )

    analyses: list[dict[str, Any]] = []
    if not args.select_only:
        for case in cases:
            analyses.append(
                analyze_case(
                    case,
                    model=args.model,
                    fallback_model=args.fallback_model,
                    allow_fallback_minimax=args.allow_fallback_minimax,
                    primary_retries=args.primary_retries,
                    rate_limit_cooldown_seconds=args.rate_limit_cooldown_seconds,
                )
            )
    report = build_report(
        cases=cases,
        analyses=analyses,
        generated_at=now_iso(),
        source_snapshot_at=source_snapshot_at,
        model=args.model,
        fallback_model=args.fallback_model,
        allow_fallback_minimax=args.allow_fallback_minimax,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_markdown_report(report), encoding="utf-8")
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"ok": True, "case_count": len(cases), "skipped_count": report["skipped_count"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
