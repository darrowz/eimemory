#!/usr/bin/env python3
"""Emit a bounded, non-sensitive summary of a release-closure report."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
from typing import Any


MAX_REPORT_BYTES = 16 * 1024 * 1024


def summarize_release_closure(report: object) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValueError("release closure report must be an object")
    deployment = report.get("deployment") if isinstance(report.get("deployment"), dict) else {}
    replay = report.get("replay_bootstrap") if isinstance(report.get("replay_bootstrap"), dict) else {}
    recall_gate = report.get("production_recall_gate") if isinstance(report.get("production_recall_gate"), dict) else {}
    live = report.get("live_acceptance") if isinstance(report.get("live_acceptance"), dict) else {}
    rehearsal = report.get("closure_rehearsal") if isinstance(report.get("closure_rehearsal"), dict) else {}
    readiness = report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
    rehearsal_complete = rehearsal.get("closure_complete") is True
    rehearsal_accumulating = rehearsal.get("data_accumulating") is True
    return {
        "ok": report.get("ok") is True,
        "closure_complete": report.get("closure_complete") is True,
        "data_accumulating": report.get("data_accumulating") is True,
        "blocked_stage": str(report.get("blocked_stage") or ""),
        "blocked_reason": str(report.get("blocked_reason") or ""),
        "commit": str(deployment.get("commit") or ""),
        "version": str(deployment.get("version") or ""),
        "receipt_id": str(deployment.get("promotion_request_id") or ""),
        "production_recall_gate_ok": recall_gate.get("ok") is True,
        "production_recall_gate_status": str(recall_gate.get("status") or ""),
        "production_recall_gate_report_id": str(recall_gate.get("report_id") or ""),
        "production_recall_gate_reason": str(recall_gate.get("reason") or ""),
        "replay_ok": replay.get("ok") is True,
        "live_acceptance_ok": live.get("ok") is True,
        "live_pass_count": int(live.get("pass_count") or 0),
        "live_case_count": int(live.get("case_count") or 0),
        "rehearsal_ok": rehearsal.get("ok") is True and rehearsal_complete != rehearsal_accumulating,
        "readiness_stage": str(readiness.get("current_stage") or readiness.get("status") or ""),
        "readiness_score": readiness.get("readiness_score"),
    }


def _read_report(path: Path) -> object:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("release closure report must be a regular non-symlink file")
        if metadata.st_size > MAX_REPORT_BYTES:
            raise ValueError("release closure report exceeds size limit")
        raw = handle.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("release closure report exceeds size limit")
    return json.loads(raw.decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = _read_report(args.path)
        summary = summarize_release_closure(report)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"release closure summary failed: {exc}\n")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if _release_closure_summary_contract_ok(report, summary) else 1


def _release_closure_summary_contract_ok(report: object, summary: dict[str, Any]) -> bool:
    if not isinstance(report, dict) or summary.get("ok") is not True:
        return False
    deployment = report.get("deployment") if isinstance(report.get("deployment"), dict) else {}
    commit = str(deployment.get("commit") or "").strip().lower()
    version = str(deployment.get("version") or "").strip()
    receipt_id = str(deployment.get("promotion_request_id") or "").strip()
    receipt = report.get("deployment_receipt") if isinstance(report.get("deployment_receipt"), dict) else {}
    session_id = str(receipt.get("release_session_id") or "").strip()
    release_identity = {
        "release_commit": commit,
        "release_version": version,
        "deployment_receipt_id": receipt_id,
        "release_session_id": session_id,
    }
    replay = report.get("replay_bootstrap") if isinstance(report.get("replay_bootstrap"), dict) else {}
    live = report.get("live_acceptance") if isinstance(report.get("live_acceptance"), dict) else {}
    rehearsal = report.get("closure_rehearsal") if isinstance(report.get("closure_rehearsal"), dict) else {}
    readiness = report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
    recall = report.get("production_recall_gate") if isinstance(report.get("production_recall_gate"), dict) else {}
    readiness_identity = (
        readiness.get("release_identity") if isinstance(readiness.get("release_identity"), dict) else {}
    )
    live_deployment = live.get("deployment") if isinstance(live.get("deployment"), dict) else {}
    common = bool(
        re.fullmatch(r"[0-9a-f]{40}", commit)
        and version
        and receipt_id
        and session_id
        and receipt.get("ok") is True
        and receipt.get("commit") == commit
        and receipt.get("version") == version
        and receipt.get("promotion_request_id") == receipt_id
        and receipt.get("release_session_id") == session_id
        and not str(report.get("blocked_stage") or "")
        and not str(report.get("blocked_reason") or "")
        and replay.get("ok") is True
        and live.get("ok") is True
        and _exact_int(live.get("case_count"), 10)
        and _exact_int(live.get("pass_count"), 10)
        and _exact_int(live.get("fail_count"), 0)
        and _exact_int(live.get("distinct_task_types"), 10)
        and _deployment_identity_matches(live_deployment, commit=commit, version=version, receipt_id=receipt_id)
        and readiness.get("ok") is True
        and readiness.get("schema_version") == "l5_readiness.v2"
        and readiness_identity == release_identity
    )
    if not common:
        return False
    complete = report.get("closure_complete") is True
    accumulating = report.get("data_accumulating") is True
    if complete == accumulating:
        return False
    if accumulating:
        pending = (
            report.get("bootstrap_pending_verification")
            if isinstance(report.get("bootstrap_pending_verification"), dict)
            else {}
        )
        recall_pending = recall.get("bootstrap") if isinstance(recall.get("bootstrap"), dict) else {}
        rehearsal_pending = (
            rehearsal.get("bootstrap_pending_verification")
            if isinstance(rehearsal.get("bootstrap_pending_verification"), dict)
            else {}
        )
        pending_record_id = str(pending.get("record_id") or "")
        score = readiness.get("readiness_score")
        return bool(
            recall.get("status") == "data_accumulating"
            and all(
                item.get("ok") is True
                and item.get("status") == "bootstrap_data_pending"
                and str(item.get("record_id") or "") == pending_record_id
                and item.get("release_identity") == release_identity
                for item in (pending, recall_pending, rehearsal_pending)
            )
            and pending_record_id
            and rehearsal.get("ok") is True
            and rehearsal.get("closure_complete") is False
            and rehearsal.get("data_accumulating") is True
            and readiness.get("current_stage") == "L4.5"
            and isinstance(score, (int, float))
            and not isinstance(score, bool)
            and float(score) == 0.8
        )
    strict = (
        report.get("production_recall_strict_state")
        if isinstance(report.get("production_recall_strict_state"), dict)
        else {}
    )
    score = readiness.get("readiness_score")
    return bool(
        recall.get("ok") is True
        and recall.get("status") == "accepted"
        and strict.get("ok") is True
        and strict.get("status") == "strict_activated"
        and str(strict.get("candidate_commit") or "") == commit
        and rehearsal.get("ok") is True
        and rehearsal.get("closure_complete") is True
        and rehearsal.get("data_accumulating") is False
        and readiness.get("current_stage") == "L5"
        and isinstance(score, (int, float))
        and not isinstance(score, bool)
        and float(score) == 1.0
    )


def _deployment_identity_matches(
    deployment: dict[str, Any],
    *,
    commit: str,
    version: str,
    receipt_id: str,
) -> bool:
    return bool(
        deployment.get("commit") == commit
        and deployment.get("version") == version
        and deployment.get("promotion_request_id") == receipt_id
    )


def _exact_int(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected


if __name__ == "__main__":
    raise SystemExit(main())
