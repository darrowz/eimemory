#!/usr/bin/env python3
"""Run the production-recall adoption gate before switching immutable releases."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
from typing import Any

from eimemory.api.runtime import Runtime
from eimemory.evaluation.real_query_gate import (
    _REAL_QUERY_MIN_CASES,
    _REAL_QUERY_MIN_CASES_PER_CHANNEL,
    bootstrap_production_recall_baseline,
    freeze_production_recall_dataset,
    record_production_recall_bootstrap_pending,
)
from eimemory.evaluation.production_query_dataset import (
    build_production_query_dataset,
    collect_pending_production_queries,
    write_production_query_dataset,
)
from eimemory.scheduler.jobs import load_json_dataset_with_evidence


_MAX_PRIOR_HEALTH_SNAPSHOT_BYTES = 64 * 1024
_MAX_PRIOR_HEALTH_SNAPSHOT_PATH_CHARS = 4096


def _progress(frozen: dict[str, Any]) -> dict[str, Any]:
    eligibility = frozen.get("eligibility") if isinstance(frozen.get("eligibility"), dict) else {}
    return {
        "case_count": int(eligibility.get("case_count") or 0),
        "accepted_label_count": int(eligibility.get("accepted_label_count") or 0),
        "per_channel_case_count": dict(eligibility.get("per_channel_case_count") or {}),
        "required_case_count": _REAL_QUERY_MIN_CASES,
        "required_per_channel": _REAL_QUERY_MIN_CASES_PER_CHANNEL,
        "blocked_reasons": list(eligibility.get("blocked_reasons") or []),
    }


def _collection_summary(collection: dict[str, Any]) -> dict[str, Any]:
    return {
        "created": int(collection.get("created") or 0),
        "skipped": dict(collection.get("skipped") or {}),
    }


def _load_prior_health_snapshot(path_value: str) -> dict[str, Any] | None:
    value = str(path_value or "").strip()
    if not value:
        return None
    if len(value) > _MAX_PRIOR_HEALTH_SNAPSHOT_PATH_CHARS:
        raise ValueError("invalid prior health snapshot")
    path = Path(value).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        if path.is_symlink():
            raise ValueError("invalid prior health snapshot")
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_PRIOR_HEALTH_SNAPSHOT_BYTES:
            raise ValueError("invalid prior health snapshot")
        raw = os.read(descriptor, _MAX_PRIOR_HEALTH_SNAPSHOT_BYTES + 1)
        if len(raw) > _MAX_PRIOR_HEALTH_SNAPSHOT_BYTES or os.read(descriptor, 1):
            raise ValueError("invalid prior health snapshot")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid prior health snapshot") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise ValueError("invalid prior health snapshot")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-switch production recall bootstrap")
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--prior-commit", required=True)
    parser.add_argument("--current-link", required=True)
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--prior-health-snapshot", default="")
    parser.add_argument("--dataset", default=os.environ.get("EIMEMORY_PRODUCTION_RECALL_DATASET", ""))
    parser.add_argument("--root", default=os.environ.get("EIMEMORY_ROOT", "~/.eimemory"))
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--user", required=True)
    args = parser.parse_args(argv)
    try:
        prior_health_snapshot = _load_prior_health_snapshot(args.prior_health_snapshot)
    except ValueError:
        print(json.dumps({"ok": False, "status": "blocked", "reason": "prior_health_snapshot_invalid"}, sort_keys=True))
        return 2
    scope = {
        "tenant_id": args.tenant,
        "agent_id": args.agent,
        "workspace_id": args.workspace,
        "user_id": args.user,
    }
    runtime = Runtime.create(root=Path(args.root).expanduser())
    try:
        collection = collect_pending_production_queries(runtime, scope=scope)
        collection_summary = _collection_summary(collection)
        dataset_path = str(args.dataset or "").strip()
        if dataset_path and not Path(dataset_path).is_file():
            report = {
                "ok": False,
                "status": "blocked",
                "reason": "dataset_path_unavailable",
                "path": dataset_path,
                "collection": collection_summary,
            }
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 2
        if not dataset_path:
            accumulated = build_production_query_dataset(runtime, scope=scope)
            if accumulated.get("ready") is True:
                conventional = Path(args.root).expanduser() / "evaluation" / "production_recall.json"
                write_production_query_dataset(accumulated["dataset"], conventional)
                dataset_path = str(conventional)
            else:
                report = record_production_recall_bootstrap_pending(
                    runtime,
                    scope=scope,
                    candidate_commit=args.candidate_commit,
                    prior_commit=args.prior_commit,
                    current_link=args.current_link,
                    health_url=args.health_url,
                    prior_health_snapshot=prior_health_snapshot,
                    reason="production_dataset_not_ready",
                    progress={**dict(accumulated.get("progress") or {}), "pending_collected": int(collection.get("created") or 0)},
                )
                report["collection"] = collection_summary
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
                return 0 if report.get("ok") is True else 1
        if dataset_path and Path(dataset_path).is_file():
            dataset, evidence = load_json_dataset_with_evidence(dataset_path)
            if not isinstance(dataset, dict):
                raise ValueError("production recall dataset must be an object")
            dataset = {**dataset, "_secure_dataset_evidence": evidence}
            frozen = freeze_production_recall_dataset(dataset)
            if not frozen.get("eligibility", {}).get("ok"):
                report = record_production_recall_bootstrap_pending(
                    runtime,
                    scope=scope,
                    candidate_commit=args.candidate_commit,
                    prior_commit=args.prior_commit,
                    current_link=args.current_link,
                    health_url=args.health_url,
                    prior_health_snapshot=prior_health_snapshot,
                    reason="production_dataset_not_ready",
                    progress=_progress(frozen),
                )
            else:
                report = bootstrap_production_recall_baseline(
                    runtime,
                    dataset,
                    candidate_commit=args.candidate_commit,
                    prior_commit=args.prior_commit,
                    current_link=args.current_link,
                    health_url=args.health_url,
                    prior_health_snapshot=prior_health_snapshot,
                    scope=scope,
                    persist_report=True,
                )
        report["collection"] = collection_summary
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report.get("ok") is True or report.get("status") == "bootstrap_data_pending" or report.get("bootstrap_status") in {"anchor_ready", "baseline_ready"} else 1
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
