"""Durable operator workflow for bounded subject projections.

Run with ``python -m eimemory.subject_projection_cli``. Initialize the separate
authority directory using the existing ``identity authority-init`` command.
Preview and authorization read source SQLite without Runtime/schema bootstrap.
Actual apply must run as the existing database's service owner, never root.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from types import SimpleNamespace

from eimemory.core import strict_json
from eimemory.identity_authority import IdentityAuthorityError
from eimemory.storage.sqlite_store import SqliteRecordStore
from eimemory import subject_projection as subject


_MAX_MANIFEST_BYTES = 1_100_000
_CODE = re.compile(r"(?:subject_projection|identity)_[a-z_]{1,120}")
_SAFE_REPORT_FIELDS = frozenset({"ok", "dry_run", "manifest_digest", "planned_count", "written_count",
                               "original_records_changed", "natural_samples_created", "authorization_verified",
                               "grant_id", "record_ids", "idempotent"})


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Argument text can contain a private request. Never echo it on failure.
        raise ValueError("subject_projection_cli_arguments_invalid")


def _paths(root_value, database_value):
    root = Path(root_value).resolve(strict=True)
    supplied = Path(database_value)
    database = supplied.resolve(strict=True)
    managed_state, managed_database = root / "state", root / "state" / "eimemory.sqlite"
    if (not root.is_dir() or managed_state.is_symlink() or managed_database.is_symlink()
            or supplied.is_symlink() or database != managed_database.resolve(strict=True)
            or not stat.S_ISREG(database.stat().st_mode)):
        raise ValueError("subject_projection_cli_database_root_mismatch")
    metadata = database.stat()
    return root, database, (metadata.st_dev, metadata.st_ino)


@contextmanager
def _read_source(database):
    with subject.readonly_subject_source(database) as reader:
        yield reader


def _json_file(path, maximum):
    with Path(path).open("rb") as handle:
        raw = handle.read(maximum + 1)
    return strict_json.loads(raw, max_bytes=maximum, max_depth=24)


def _request(parsed):
    if parsed.request_text is not None:
        return parsed.request_text
    with Path(parsed.request_file).open("rb") as handle:
        raw = handle.read(16_001)
    if len(raw) > 16_000:
        raise ValueError("subject_projection_human_request_required")
    return raw.decode("utf-8", errors="strict")


def _require_service_user(database):
    if (os.name != "posix" or os.geteuid() == 0 or os.getuid() != os.geteuid()
            or database.stat().st_uid != os.geteuid()):
        raise ValueError("subject_projection_cli_service_user_required")


def _apply(parsed, root, database, identity):
    directory = subject.projection_authority_directory(parsed.authority_dir)
    with subject.authority_api.snapshot(directory) as (authority, _):
        _, manifest = subject._active_grant(authority, parsed.grant_id)
        host_database = parsed.host_database or manifest["host_database"]
    kwargs = {"grant_id": parsed.grant_id, "host_database": host_database, "directory": directory}
    if not parsed.apply:
        return subject.preview_subject_projection_apply(database, **kwargs)
    # Reject root before RuntimeStore can create directories, initialize schema,
    # flush an outbox, or change a service-owned file's ownership expectations.
    _require_service_user(database)
    from eimemory.storage.runtime_store import RuntimeStore

    store = RuntimeStore(root)
    try:
        current = database.stat()
        if (Path(store.sqlite.path).resolve(strict=True) != database
                or (current.st_dev, current.st_ino) != identity):
            raise ValueError("subject_projection_cli_database_changed")
        return subject.apply_subject_projection(SimpleNamespace(store=store), **kwargs, dry_run=False)
    finally:
        store.close()


def _parser():
    parser = _Parser(description="Plan and apply exact, protected subject-memory projections.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for command in ("preview", "authorize", "apply"):
        child = commands.add_parser(command)
        child.add_argument("--root", required=True)
        child.add_argument("--database", required=True)
        child.add_argument("--host-database")
        child.add_argument("--authority-dir")
        if command in {"preview", "authorize"}:
            child.add_argument("--manifest", required=True)
        if command in {"authorize", "apply"}:
            child.add_argument("--apply", action="store_true", help="Persist this operation; default is read-only dry run.")
        if command == "authorize":
            request = child.add_mutually_exclusive_group(required=True)
            request.add_argument("--request-text")
            request.add_argument("--request-file")
        if command == "apply":
            child.add_argument("--grant-id", required=True)
    return parser


def main(argv=None):
    try:
        parsed = _parser().parse_args(argv)
        root, database, identity = _paths(parsed.root, parsed.database)
        if parsed.command == "apply":
            report = _apply(parsed, root, database, identity)
        else:
            manifest = _json_file(parsed.manifest, _MAX_MANIFEST_BYTES)
            # Validate typed manifest before accepting its host path as input.
            proposal = subject._manifest(manifest)
            host_database = parsed.host_database or proposal["host_database"]
            # Pass a descriptor, not a root-opened SQLite handle. The core runs
            # validation as the database owner before SQLite can create WAL files.
            if parsed.command == "preview":
                report = subject.plan_subject_projection(database, proposal, host_database=host_database)
            else:
                report = subject.authorize_subject_projection(database, proposal, host_database=host_database,
                    directory=subject.projection_authority_directory(parsed.authority_dir),
                    request_text=_request(parsed), dry_run=not parsed.apply)
        safe = {key: value for key, value in report.items() if key in _SAFE_REPORT_FIELDS}
        print(json.dumps({"schema": "subject_projection_cli.v1", **safe}, ensure_ascii=False, sort_keys=True))
        return 0 if report.get("ok") is True else 1
    except (OSError, sqlite3.Error, TypeError, ValueError, RuntimeError) as exc:
        message = str(exc)
        code = message if _CODE.fullmatch(message) else "subject_projection_cli_invalid_input"
        print(json.dumps({"schema": "subject_projection_cli.v1", "ok": False, "error": code}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
