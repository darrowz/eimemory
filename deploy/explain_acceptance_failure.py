#!/usr/bin/env python3
"""Correlate original acceptance results with exact-scope probe rows in a backup.

Default output contains safe codes and hashes. Private errors require an
explicit switch AND a new file destination; they are never sent to stdout.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.offline_state_review import open_snapshot, write_new_report
from eimemory.storage.atomic_file import read_json_strict
from eimemory.governance.release.closure_contracts import acceptance_failure_details, acceptance_report_ok

_SAFE = re.compile(r"[A-Za-z0-9_.:-]{1,256}")


def explain(report: dict, *, snapshot: Path | None = None, include_private_errors: bool = False) -> dict:
    bootstrap = report.get("replay_bootstrap", report)
    if not isinstance(bootstrap, dict):
        raise ValueError("invalid_replay_bootstrap")
    acceptance = bootstrap.get("capability_acceptance", bootstrap)
    if not isinstance(acceptance, dict):
        raise ValueError("invalid_acceptance_report")
    rows = acceptance.get("results")
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("missing_or_oversized_acceptance_results")
    failures = [r for r in rows if isinstance(r, dict) and (r.get("passed") is not True or r.get("error"))]
    output = {"schema": "acceptance_failure_correlation.v1", "diagnostics": ({"failure_count": 0, "failures": [], "truncated": False}
                  if acceptance_report_ok(acceptance, expected_count=None)
                  else acceptance_failure_details(acceptance)),
              "records": [], "private_errors_included": include_private_errors,
              "source": "original_report_and_optional_offline_snapshot", "success_evidence_created": False}
    scope = acceptance.get("scope", report.get("scope"))
    if snapshot is not None and not isinstance(scope, dict):
        raise ValueError("exact_scope_required_for_snapshot_lookup")
    scope_values = [str((scope or {}).get(k) or ("default" if k == "tenant_id" else ""))
                    for k in ("tenant_id", "agent_id", "workspace_id", "user_id")]

    def collect(connection=None):
        for row in failures[:100]:
            raw_error = str(row.get("error") or "")
            item = {k: v for k in ("case_id", "probe_record_id", "trace_record_id", "executor_id", "evaluation_run_id")
                    if isinstance((v := row.get(k)), str) and _SAFE.fullmatch(v)}
            item["reported_error_sha256"] = hashlib.sha256(raw_error.encode()).hexdigest()
            if include_private_errors:
                item["reported_error"] = raw_error[:16384]
            probe_id = row.get("probe_record_id")
            if connection is not None and isinstance(probe_id, str) and probe_id:
                found = connection.execute(
                    "SELECT payload_json,payload_pointer_json FROM records WHERE record_id=? AND tenant_id=? "
                    "AND agent_id=? AND workspace_id=? AND user_id=? LIMIT 2", [probe_id, *scope_values]
                ).fetchall()
                item["probe_lookup"] = "missing_or_ambiguous" if len(found) != 1 else "found"
                if len(found) == 1:
                    if found[0]["payload_pointer_json"]:
                        item["probe_lookup"] = "archived_payload_requires_full_snapshot_reader"
                    else:
                        from eimemory.core.strict_json import loads
                        payload = loads(found[0]["payload_json"], max_bytes=16 * 1024 * 1024)
                        content = payload.get("content") if isinstance(payload, dict) else None
                        if isinstance(content, dict):
                            err = str(content.get("error") or "")
                            item["probe_error_sha256"] = hashlib.sha256(err.encode()).hexdigest()
                            if include_private_errors:
                                item["probe_error"] = err[:16384]
                            for key in ("executor_id", "executor_version", "grader_id", "grader_revision"):
                                value = content.get(key)
                                if isinstance(value, str) and _SAFE.fullmatch(value):
                                    item[key] = value
            output["records"].append(item)
    if snapshot is None:
        collect()
    else:
        with open_snapshot(snapshot) as connection:
            collect(connection)
    output["truncated"] = len(failures) > 100
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--offline-snapshot", type=Path)
    parser.add_argument("--include-private-errors", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.include_private_errors and args.output is None:
        parser.error("private errors require --output in a private directory")
    try:
        report = read_json_strict(args.report, dict)
        result = explain(report, snapshot=args.offline_snapshot, include_private_errors=args.include_private_errors)
        if args.output:
            write_new_report(args.output, result)
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"correlation_complete": False, "error_type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
