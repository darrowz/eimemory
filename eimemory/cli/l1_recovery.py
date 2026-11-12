"""Metadata-only, explicitly selected L1 dead-job operator recovery."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from eimemory.knowledge.l1_queue import L1ExtractQueue, L1QueueStateError, RECOVERY_INTENT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("inspect", "recover"):
        command = sub.add_parser(action)
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--job-id", action="append")
        if action == "inspect":
            command.add_argument("--limit", type=int, default=32)
        if action == "recover":
            command.add_argument("--operator-intent", required=True, choices=[RECOVERY_INTENT])
            command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    queue = L1ExtractQueue(args.root / "state" / "l1_extract_queue.json")
    try:
        if args.action == "inspect":
            report = queue.inspect_dead(job_ids=args.job_id, limit=args.limit)
        else:
            report = queue.recover_dead(args.job_id, operator_intent=args.operator_intent,
                                        dry_run=args.dry_run)
    except L1QueueStateError as exc:
        # A write/fsync failure can occur after replace; do not imply rollback.
        report = {"ok": False, "error": exc.code, "outcome": "unconfirmed"}
    except TimeoutError:
        report = {"ok": False, "error": "recovery_lock_timeout", "outcome": "unconfirmed"}
    except (OSError, ValueError, UnicodeError):
        report = {"ok": False, "error": "recovery_io_failed", "outcome": "unconfirmed"}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
