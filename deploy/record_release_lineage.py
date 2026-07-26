#!/usr/bin/env python3
"""Persist the current release's verified capability-lineage attestation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


RELEASE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RELEASE_ROOT))

from eimemory.api.runtime import Runtime  # noqa: E402
from eimemory.governance.evidence_contract import current_release_identity  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--current-commit", required=True)
    parser.add_argument("--scope-agent", required=True)
    parser.add_argument("--scope-workspace", required=True)
    parser.add_argument("--scope-user", required=True)
    args = parser.parse_args(argv)
    commit = str(args.current_commit or "").strip().lower()
    scope = {
        "agent_id": args.scope_agent,
        "workspace_id": args.scope_workspace,
        "user_id": args.scope_user,
    }
    runtime = Runtime.create()
    try:
        release = current_release_identity(runtime, scope)
        if release is None or release.commit != commit or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            report = {"ok": False, "error": "current_release_receipt_invalid"}
        else:
            report = runtime.record_release_lineage(
                scope=scope,
                repo_root=args.repo_root,
                current_release=release,
            )
    finally:
        runtime.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
