#!/usr/bin/env python3
"""Emit a bounded, non-sensitive summary of a release-closure report."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any


# This standalone CLI also imports from the immutable release when invoked
# without -B. Pin suppression before loading any local package modules.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eimemory.governance.release.closure_contracts import (
    acceptance_failure_details,
    channel_wait_report_ok,
    live_acceptance_report_ok,
    legacy_release_replay_ok,
)

MAX_REPORT_BYTES = 16 * 1024 * 1024


def _reported_release_summary(report: object) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValueError("release closure report must be an object")
    deployment = report.get("deployment") if isinstance(report.get("deployment"), dict) else {}
    replay = report.get("replay_bootstrap") if isinstance(report.get("replay_bootstrap"), dict) else {}
    recall_gate = report.get("production_recall_gate") if isinstance(report.get("production_recall_gate"), dict) else {}
    recall_threshold = (
        recall_gate.get("threshold_gate")
        if isinstance(recall_gate.get("threshold_gate"), dict)
        else {}
    )
    if "gate_ok" in recall_gate:
        recall_gate_ok = recall_gate.get("gate_ok") is True
    elif recall_threshold:
        recall_gate_ok = recall_threshold.get("ok") is True
    else:
        recall_gate_ok = recall_gate.get("ok") is True
    live = report.get("live_acceptance") if isinstance(report.get("live_acceptance"), dict) else {}
    channel = (
        report.get("channel_acceptance")
        if isinstance(report.get("channel_acceptance"), dict)
        else {}
    )
    rehearsal = report.get("closure_rehearsal") if isinstance(report.get("closure_rehearsal"), dict) else {}
    readiness = report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
    rehearsal_complete = rehearsal.get("closure_complete") is True
    rehearsal_accumulating = rehearsal.get("data_accumulating") is True
    closure_complete = report.get("closure_complete") is True
    data_accumulating = report.get("data_accumulating") is True
    if (
        report.get("ok") is True
        and report.get("report_type") == "code_evolution_pre_observation"
        and report.get("status") == "ready_for_observation"
        and not closure_complete
        and not data_accumulating
    ):
        business_closure_outcome = "ready_for_observation"
    elif report.get("ok") is True and closure_complete and not data_accumulating:
        business_closure_outcome = "closure_complete"
    elif report.get("ok") is True and data_accumulating and not closure_complete:
        business_closure_outcome = "data_accumulating"
    elif channel_wait_report_ok(report):
        # This is a bound wait for external evidence, never closure success.
        business_closure_outcome = "data_accumulating"
        data_accumulating = True
    else:
        business_closure_outcome = "failed"
    return {
        "ok": report.get("ok") is True,
        "business_closure_outcome": business_closure_outcome,
        "report_type": str(report.get("report_type") or ""),
        "observation_admission_status": str(report.get("status") or "") if report.get("report_type") == "code_evolution_pre_observation" else "",
        "closure_complete": closure_complete,
        "data_accumulating": data_accumulating,
        "blocked_stage": str(report.get("blocked_stage") or ""),
        "blocked_reason": str(report.get("blocked_reason") or ""),
        "commit": str(deployment.get("commit") or ""),
        "version": str(deployment.get("version") or ""),
        "receipt_id": str(deployment.get("promotion_request_id") or ""),
        "production_recall_gate_ok": recall_gate_ok,
        "production_recall_gate_status": str(
            recall_gate.get("status") or recall_gate.get("gate_status") or ""
        ),
        "production_recall_gate_report_id": str(
            recall_gate.get("report_id")
            or recall_gate.get("record_id")
            or recall_gate.get("persisted_record_id")
            or ""
        ),
        "production_recall_gate_reason": str(
            recall_gate.get("reason") or recall_gate.get("blocked_reason") or ""
        ),
        "replay_ok": replay.get("ok") is True,
        "acceptance_failure": (
            acceptance_failure_details(replay.get("capability_acceptance"))
            if replay.get("ok") is not True and report.get("blocked_stage") == "replay_bootstrap"
            else {}
        ),
        "live_acceptance_ok": live.get("ok") is True,
        "live_pass_count": live.get("pass_count") if type(live.get("pass_count")) is int else 0,
        "live_case_count": live.get("case_count") if type(live.get("case_count")) is int else 0,
        "channel_acceptance_ok": channel.get("ok") is True,
        "channel_acceptance_record_id": str(channel.get("record_id") or ""),
        "rehearsal_ok": rehearsal.get("ok") is True and rehearsal_complete != rehearsal_accumulating,
        "readiness_stage": str(readiness.get("current_stage") or readiness.get("status") or ""),
        "readiness_score": readiness.get("readiness_score"),
    }


def summarize_release_closure(report: object) -> dict[str, Any]:
    """Separate reported state from a structurally validated release result.

    This summary does not replace the runtime's authoritative evidence checks.
    A wait can have exit_code=0 but never closure_certified=True. Invalid
    self-declared success is retained only in reported_* diagnostic fields.
    """
    raw = _reported_release_summary(report)
    contract_ok = _release_closure_summary_contract_ok(report, raw)
    waiting = channel_wait_report_ok(report)
    admitted = bool(contract_ok or waiting)
    certified = bool(contract_ok and raw["closure_complete"] and not raw["data_accumulating"])
    replay = report.get("replay_bootstrap") if isinstance(report, dict) else None
    receipt = report.get("deployment_receipt") if isinstance(report, dict) else None
    live = report.get("live_acceptance") if isinstance(report, dict) else None
    # Dynamic pre-observation has its own verified cohort contract; it must
    # not be relabelled as a failed historic weak-cohort replay.
    replay_ok = (bool(contract_ok and raw["replay_ok"])
                 if report.get("report_type") == "code_evolution_pre_observation"
                 else legacy_release_replay_ok(replay))
    return {
        **raw,
        "summary_schema_version": "release_closure_summary.v2",
        "validation_scope": "structural_report_contract_not_independent_attestation",
        "reported_ok": raw["ok"],
        "reported_closure_complete": raw["closure_complete"],
        "reported_data_accumulating": report.get("data_accumulating") is True,
        "reported_replay_ok": raw["replay_ok"],
        "reported_live_acceptance_ok": raw["live_acceptance_ok"],
        "ok": bool(contract_ok and raw["ok"]),
        "contract_ok": admitted,
        "closure_complete": certified,
        "closure_certified": certified,
        "data_accumulating": bool(admitted and raw["data_accumulating"]),
        "replay_ok": replay_ok,
        "live_acceptance_ok": live_acceptance_report_ok(live, receipt=receipt),
        "business_closure_outcome": raw["business_closure_outcome"] if admitted else "failed",
        "contract_error": "" if admitted else "release_closure_report_contract_invalid",
        "exit_code": 0 if admitted else 1,
    }


def _read_report(path: Path) -> object:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
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
    from eimemory.core.strict_json import loads as strict_json_loads

    return strict_json_loads(raw, max_bytes=MAX_REPORT_BYTES, max_depth=64)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = _read_report(args.path)
        summary = summarize_release_closure(report)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"release closure summary failed: {exc}\n")
    # Emit only after validation. JSON and the process status share one decision.
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return summary["exit_code"]


def _release_closure_summary_contract_ok(report: object, summary: dict[str, Any]) -> bool:
    if not isinstance(report, dict) or summary.get("ok") is not True:
        return False
    if report.get("report_type") == "code_evolution_pre_observation":
        # The installer invokes this script with -I and system Python. Import
        # only the matching release's dependency-free structural contract.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from eimemory.governance.release_pre_observation import pre_observation_report_ok

        return pre_observation_report_ok(report)
    if report.get("report_type") != "l5_release_closure":
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
    channel = (
        report.get("channel_acceptance")
        if isinstance(report.get("channel_acceptance"), dict)
        else {}
    )
    rehearsal = report.get("closure_rehearsal") if isinstance(report.get("closure_rehearsal"), dict) else {}
    readiness = report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
    recall = report.get("production_recall_gate") if isinstance(report.get("production_recall_gate"), dict) else {}
    readiness_identity = (
        readiness.get("release_identity") if isinstance(readiness.get("release_identity"), dict) else {}
    )
    live_deployment = live.get("deployment") if isinstance(live.get("deployment"), dict) else {}
    common = bool(
        re.fullmatch(r"[0-9a-f]{40}", commit)
        and receipt_id
        and session_id
        and receipt.get("ok") is True
        and receipt.get("commit") == commit
        and receipt.get("promotion_request_id") == receipt_id
        and receipt.get("release_session_id") == session_id
        and deployment.get("release_path") == receipt.get("release_path")
        and isinstance(report.get("storage_migrations"), dict)
        and report["storage_migrations"].get("ok") is True
        and not str(report.get("blocked_stage") or "")
        and not str(report.get("blocked_reason") or "")
        and replay.get("ok") is True
        and legacy_release_replay_ok(replay)
        and live_acceptance_report_ok(live, receipt=receipt)
        and channel.get("ok") is True
        and channel.get("evidence_class") == "external_channel_receipt"
        and str(channel.get("record_id") or "")
        and _deployment_identity_matches(
            live_deployment,
            commit=commit,
            receipt_id=receipt_id,
        )
        and readiness.get("ok") is True
        and readiness.get("schema_version") == "l5_readiness.v2"
        and _release_authority_matches(readiness_identity, release_identity)
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
                and _release_authority_matches(
                    item.get("release_identity"),
                    release_identity,
                )
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
    receipt_id: str,
) -> bool:
    return bool(
        deployment.get("commit") == commit
        and deployment.get("promotion_request_id") == receipt_id
    )


def _release_authority_matches(left: object, right: object) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    keys = (
        "release_commit",
        "deployment_receipt_id",
        "release_session_id",
    )
    return bool(
        all(str(left.get(key) or "").strip() for key in keys)
        and all(
            str(left.get(key) or "").strip() == str(right.get(key) or "").strip()
            for key in keys
        )
    )


def _exact_int(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected


if __name__ == "__main__":
    raise SystemExit(main())
