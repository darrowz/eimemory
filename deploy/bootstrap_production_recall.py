#!/usr/bin/env python3
"""Run the production-recall adoption gate before switching immutable releases."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from eimemory.api.runtime import Runtime
from eimemory.evaluation.real_query_gate import (
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


def _progress(frozen: dict[str, Any]) -> dict[str, Any]:
    eligibility = frozen.get("eligibility") if isinstance(frozen.get("eligibility"), dict) else {}
    return {
        "case_count": int(eligibility.get("case_count") or 0),
        "accepted_label_count": int(eligibility.get("accepted_label_count") or 0),
        "per_channel_case_count": dict(eligibility.get("per_channel_case_count") or {}),
        "required_case_count": 15,
        "required_per_channel": 5,
        "blocked_reasons": list(eligibility.get("blocked_reasons") or []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-switch production recall bootstrap")
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--prior-commit", required=True)
    parser.add_argument("--current-link", required=True)
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--dataset", default=os.environ.get("EIMEMORY_PRODUCTION_RECALL_DATASET", ""))
    parser.add_argument("--root", default=os.environ.get("EIMEMORY_ROOT", "~/.eimemory"))
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--user", required=True)
    args = parser.parse_args(argv)
    scope = {
        "tenant_id": args.tenant,
        "agent_id": args.agent,
        "workspace_id": args.workspace,
        "user_id": args.user,
    }
    runtime = Runtime.create(root=Path(args.root).expanduser())
    try:
        collection = collect_pending_production_queries(runtime, scope=scope)
        dataset_path = str(args.dataset or "").strip()
        if not dataset_path or not Path(dataset_path).is_file():
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
                    reason="production_dataset_not_ready",
                    progress={**dict(accumulated.get("progress") or {}), "pending_collected": int(collection.get("created") or 0)},
                )
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
                    scope=scope,
                    persist_report=True,
                )
        report["collection"] = {"created": int(collection.get("created") or 0), "skipped": dict(collection.get("skipped") or {})}
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report.get("ok") is True or report.get("status") == "bootstrap_data_pending" or report.get("bootstrap_status") in {"anchor_ready", "baseline_ready"} else 1
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
