#!/usr/bin/env python3
"""Build a separate v2 Markdown tree from a standalone SQLite backup.

Never touches the old projection, production database, or downstream index.
Only inline exportable records are supported. Cold pointers fail closed and
require the existing full-snapshot reader; they are never silently skipped.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.offline_state_review import open_snapshot, require_real_directory_chain
from eimemory.core.strict_json import loads
from eimemory.storage.atomic_file import atomic_write_json


def rebuild(snapshot: Path, output: Path, *, max_records: int = 1_000_000) -> dict:
    if type(max_records) is not int or max_records < 1:
        raise ValueError("invalid_record_limit")
    snapshot = snapshot.absolute()
    output = output.absolute()
    if os.path.lexists(output):
        raise FileExistsError("output_must_be_new")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise ValueError("output_parent_must_be_existing_trusted_directory")
    require_real_directory_chain(output.parent)
    require_real_directory_chain(snapshot.parent)
    if output == snapshot or output.is_relative_to(snapshot.parent):
        raise ValueError("projection_output_must_be_outside_snapshot_directory")
    # Keep staging private. Validate the snapshot *before* publishing its
    # manifest/tree: a source mutation must not leave a completed output.
    with tempfile.TemporaryDirectory(prefix=".eimemory-projection-", dir=output.parent) as temporary:
        staging = Path(temporary) / "ready"
        staging.mkdir(mode=0o700)
        with open_snapshot(snapshot, max_seconds=3600) as connection:
            result = _build_into(connection, staging, max_records=max_records)
        atomic_write_json(staging / "projection-build.json", result)
        if os.path.lexists(output):
            raise FileExistsError("output_appeared_during_build")
        # The parent is operator-owned and has no competing writers. This is
        # publication only; it does not switch or modify any downstream index.
        os.rename(staging, output)
    return result



def _build_into(connection, staging: Path, *, max_records: int) -> dict:
    columns = {r[1] for r in connection.execute("PRAGMA table_info(records)")}
    required = {"record_id", "kind", "status", "tenant_id", "agent_id", "workspace_id", "user_id",
                "source_id", "payload_json", "payload_pointer_json", "storage_key"}
    if not required.issubset(columns):
        raise ValueError("unsupported_records_schema")
    count, cold = connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN payload_pointer_json<>'' THEN 1 ELSE 0 END),0) "
        "FROM records WHERE kind IN ('memory','multimodal_memory')"
    ).fetchone()
    if count > max_records:
        raise ValueError("export_record_limit_exceeded")
    if cold:
        raise ValueError("cold_export_payload_requires_full_snapshot_reader")
    from eimemory.models.records import RecordEnvelope
    from eimemory.storage.record_export import export_record_markdown

    cursor = connection.execute(
        "SELECT record_id,kind,status,tenant_id,agent_id,workspace_id,user_id,source_id,payload_json "
        "FROM records WHERE kind IN ('memory','multimodal_memory') ORDER BY storage_key"
    )
    written = 0
    for row in cursor:
        payload = loads(row["payload_json"], max_bytes=16 * 1024 * 1024)
        if not isinstance(payload, dict):
            raise ValueError("record_payload_not_object")
        record = RecordEnvelope.from_dict(payload)
        if any(getattr(record, k) != row[k] for k in ("record_id", "kind", "status", "source_id")) or any(
            getattr(record.scope, k) != row[k] for k in ("tenant_id", "agent_id", "workspace_id", "user_id")
        ):
            raise ValueError("record_projection_identity_mismatch")
        if export_record_markdown(staging, record) is not None:
            written += 1
    return {"schema": "projection_snapshot_build.v1", "complete": True, "source_record_count": count,
            "exported_count": written, "partition_format": "v2-full-sha256",
            "source_database_modified": False, "old_projection_modified": False,
            "index_switch_performed": False, "production_acceptance": "not_assessed"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-snapshot", required=True, type=Path)
    parser.add_argument("--new-output", required=True, type=Path)
    parser.add_argument("--max-records", type=int, default=1_000_000)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(rebuild(args.offline_snapshot, args.new_output, max_records=args.max_records), sort_keys=True))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"complete": False, "error_type": type(exc).__name__,
                          "reason": str(exc), "index_switch_performed": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
