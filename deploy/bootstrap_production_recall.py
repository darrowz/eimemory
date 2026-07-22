#!/usr/bin/env python3
"""Run the production-recall adoption gate before switching immutable releases."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
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
from eimemory.governance.deployment_receipt import DEFAULT_DEPLOYMENT_CURRENT_LINK


_MAX_PRIOR_HEALTH_SNAPSHOT_BYTES = 64 * 1024
_MAX_PRIOR_HEALTH_SNAPSHOT_PATH_CHARS = 4096
_PRIOR_HEALTH_SNAPSHOT_RANDOM_CHARS = 8


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


def _effective_euid() -> int | None:
    getter = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    return int(getter()) if callable(getter) else None


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(metadata.st_mode)
        or int(getattr(metadata, "st_file_attributes", 0) or 0)
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    )


def _validate_snapshot_ancestor_chain(install_root: Path) -> os.stat_result:
    current = install_root
    immediate: os.stat_result | None = None
    while True:
        metadata = current.lstat()
        if immediate is None:
            immediate = metadata
        if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("invalid prior health snapshot")
        ancestor = current.parent
        if ancestor == current:
            break
        current = ancestor
    if immediate is None:  # pragma: no cover - absolute paths always have a root
        raise ValueError("invalid prior health snapshot")
    return immediate


def _prior_health_snapshot_metadata_error(metadata: Any, *, expected_euid: int) -> str:
    if not stat.S_ISREG(int(metadata.st_mode)):
        return "regular"
    if int(getattr(metadata, "st_uid", -1)) != int(expected_euid):
        return "owner"
    if stat.S_IMODE(int(metadata.st_mode)) != 0o600:
        return "mode"
    if int(getattr(metadata, "st_nlink", 0)) != 1:
        return "link"
    size = int(getattr(metadata, "st_size", -1))
    if size < 0 or size > _MAX_PRIOR_HEALTH_SNAPSHOT_BYTES:
        return "size"
    return ""


def _same_file_identity(left: Any, right: Any) -> bool:
    return (int(left.st_dev), int(left.st_ino)) == (int(right.st_dev), int(right.st_ino))


def _read_bounded_snapshot(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_PRIOR_HEALTH_SNAPSHOT_BYTES + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) > _MAX_PRIOR_HEALTH_SNAPSHOT_BYTES:
        raise ValueError("invalid prior health snapshot")
    return raw


def _load_prior_health_snapshot(
    path_value: str,
    *,
    candidate_commit: str = "",
) -> dict[str, Any] | None:
    value = str(path_value or "").strip()
    if not value:
        return None
    if len(value) > _MAX_PRIOR_HEALTH_SNAPSHOT_PATH_CHARS:
        raise ValueError("invalid prior health snapshot")
    path = Path(value).expanduser()
    install_root = Path(DEFAULT_DEPLOYMENT_CURRENT_LINK).expanduser().parent
    commit = str(candidate_commit or "").strip().lower()
    if (
        not path.is_absolute()
        or not install_root.is_absolute()
        or ".." in path.parts
        or ".." in install_root.parts
        or path.parent != install_root
        or re.fullmatch(
            rf"\.prior-health-{re.escape(commit)}-[A-Za-z0-9]{{{_PRIOR_HEALTH_SNAPSHOT_RANDOM_CHARS}}}\.json",
            path.name,
        )
        is None
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
    ):
        raise ValueError("invalid prior health snapshot")
    expected_euid = _effective_euid()
    if expected_euid is None:
        raise ValueError("invalid prior health snapshot")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    parent_descriptor = -1
    try:
        parent_before = _validate_snapshot_ancestor_chain(install_root)
        path_before = path.lstat()
        if _is_link_or_reparse(path_before):
            raise ValueError("invalid prior health snapshot")
        if os.open in getattr(os, "supports_dir_fd", set()):
            parent_descriptor = os.open(
                install_root,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            if not _same_file_identity(os.fstat(parent_descriptor), parent_before):
                raise ValueError("invalid prior health snapshot")
            descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        else:
            descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if _prior_health_snapshot_metadata_error(metadata, expected_euid=expected_euid):
            raise ValueError("invalid prior health snapshot")
        if not _same_file_identity(metadata, path_before):
            raise ValueError("invalid prior health snapshot")
        raw = _read_bounded_snapshot(descriptor)
        after_read = os.fstat(descriptor)
        path_after = path.lstat()
        if (
            not _same_file_identity(metadata, after_read)
            or not _same_file_identity(metadata, path_after)
            or _is_link_or_reparse(path_after)
            or int(after_read.st_size) != len(raw)
        ):
            raise ValueError("invalid prior health snapshot")
        if parent_descriptor >= 0:
            if not _same_file_identity(os.fstat(parent_descriptor), parent_before):
                raise ValueError("invalid prior health snapshot")
        elif not _same_file_identity(install_root.lstat(), parent_before):
            raise ValueError("invalid prior health snapshot")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid prior health snapshot") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
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
        prior_health_snapshot = _load_prior_health_snapshot(
            args.prior_health_snapshot,
            candidate_commit=args.candidate_commit,
        )
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
