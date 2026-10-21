"""Explicit, record-bound subject repair; never a global user alias.

An independent native identity-authority directory must name the exact current
owner/source. Its root operator grants a bounded manifest after the original
Hermes turn and physical source versions have been checked. Applying that grant
appends new memories; it never rewrites an old memory, capture, or quality label.
All public mutations default to dry run. Consumers must call the validation
helper before admitting this producer's projections after revocation or drift.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from threading import RLock
from types import SimpleNamespace

from eimemory import identity_authority as authority_api
from eimemory.core import strict_json
from eimemory.identity_authority import IdentityAuthorityError, canonical, digest
from eimemory.identity_ops import _RepairSkipped, _repair_snapshot
from eimemory.metadata import business_metadata, normalize_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage.sqlite_store import SqliteRecordStore


SCHEMA = "subject_projection_manifest.v1"
HOST_SCHEMA = "hermes_completed_turn_subject.v1"
LINEAGE_SCHEMA = "subject_projection_lineage.v1"
SOURCE = "eimemory.subject_projection"
DEFAULT_AUTHORITY_DIRECTORY = "/etc/eimemory-subject-projection"
MAX_ITEMS = 32
_SCOPE = ("tenant_id", "agent_id", "workspace_id", "user_id")
_HEX = re.compile(r"[0-9a-f]{64}")
_HOST_KEYS = {"schema", "session_id", "user_message_id", "assistant_message_id",
              "user_id_digest", "chat_id_digest", "query_digest", "answer_digest", "turn_digest"}


def _error(reason):
    raise IdentityAuthorityError("subject_projection_" + reason)


def _text_hash(value):
    return sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _require_reader_owner(path):
    # SQLite mode=ro may create live-WAL sidecars. Filesystem identity must be
    # the data owner even when no SQL statement is permitted to mutate rows.
    if os.name == "posix" and (os.getuid() != path.stat().st_uid or os.geteuid() != path.stat().st_uid):
        _error("database_reader_owner_required")


@contextmanager
def readonly_subject_source(database):
    """No schema/bootstrap; this bounded contract admits inline memories only."""
    path = Path(database).resolve(strict=True)
    _require_reader_owner(path)
    with SqliteRecordStore.readonly_inline_reader(path) as sqlite:
        lock = RLock()
        sqlite.bind_runtime_lock(lock)
        class Reader:
            def read_consistent(self, callback):
                with lock:
                    return callback(sqlite)
        reader = Reader()
        reader.sqlite = sqlite
        yield reader


def _owner_process(operation, database, payload):
    """Root validates using a real owner process, never root-owned WAL files."""
    import pwd
    path = Path(database).resolve(strict=True)
    metadata = path.stat()
    if os.name != "posix" or os.getuid() != 0 or os.geteuid() != 0 or metadata.st_uid == 0:
        _error("database_reader_owner_required")
    try:
        owner = pwd.getpwuid(metadata.st_uid)
    except KeyError:
        _error("owner_account_unavailable")
    def demote():
        os.initgroups(owner.pw_name, owner.pw_gid)
        os.setgid(owner.pw_gid)
        os.setuid(metadata.st_uid)
    source_root = Path(__file__).resolve().parent.parent
    env = {"PATH": os.defpath, "HOME": owner.pw_dir, "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONPATH": str(source_root)}
    # No caller command, private body or arbitrary module is interpolated.
    try:
        result = subprocess.run([sys.executable, "-B", "-s", "-c",
            "from eimemory.subject_projection import _owner_worker; _owner_worker()"],
            input=canonical({"operation": operation, "database": str(path), **payload}),
            text=True, capture_output=True, env=env, cwd=source_root, preexec_fn=demote, timeout=60)
    except (OSError, subprocess.SubprocessError):
        _error("owner_validation_unavailable")
    try:
        report = strict_json.loads(result.stdout, max_bytes=65536, max_depth=12)
    except (TypeError, ValueError):
        _error("owner_validation_unavailable")
    if result.returncode or not isinstance(report, dict) or report.get("ok") is not True:
        code = report.get("error") if isinstance(report, dict) else None
        if isinstance(code, str) and re.fullmatch(r"subject_projection_[a-z_]{1,100}", code):
            raise IdentityAuthorityError(code)
        _error("owner_validation_unavailable")
    return report


def _owner_worker():
    """Fixed subprocess entry point; all operations are read-only."""
    try:
        request = strict_json.loads(sys.stdin.buffer.read(1_100_001), max_bytes=1_100_000, max_depth=24)
        if request["operation"] == "plan":
            report = plan_subject_projection(Path(request["database"]), request["manifest"],
                                             host_database=request["host_database"])
        elif request["operation"] == "preview_apply":
            report = preview_subject_projection_apply(Path(request["database"]), grant_id=request["grant_id"],
                host_database=request["host_database"], directory=request["directory"])
        else:
            _error("owner_operation_invalid")
    except (OSError, TypeError, ValueError, RuntimeError, sqlite3.Error) as exc:
        code = str(exc)
        report = {"ok": False, "error": code if re.fullmatch(r"subject_projection_[a-z_]{1,100}", code)
                  else "subject_projection_owner_validation_unavailable"}
    print(canonical(report))


def _scope(value):
    if (not isinstance(value, dict) or set(value) != set(_SCOPE)
            or any(not isinstance(value[key], str) or not value[key]
                   or value[key].strip() != value[key] or "*" in value[key] for key in _SCOPE)):
        _error("scope_invalid")
    return ScopeRef.from_dict(value)


def _manifest(value):
    """Copy the exact bounded proposal; no caller object survives a lock boundary."""
    if (not isinstance(value, dict) or set(value) != {
            "schema", "owner_scope", "source_id", "channel", "host_database", "items"}
            or value.get("schema") != SCHEMA or value.get("channel") != "hermes"
            or value.get("source_id") != "hermes"):
        _error("manifest_invalid")
    owner = _scope(value["owner_scope"])
    if not owner.workspace_id.endswith("::channel::hermes"):
        _error("channel_mismatch")
    path = value["host_database"]
    if not isinstance(path, str) or not Path(path).is_absolute():
        _error("host_database_invalid")
    if (not isinstance(value["items"], list) or not 1 <= len(value["items"]) <= MAX_ITEMS):
        _error("items_invalid")
    seen = set()
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != {
                "source_ref", "source_version", "host_evidence", "host_evidence_digest"}:
            _error("item_invalid")
        ref, version, evidence = (item[key] for key in ("source_ref", "source_version", "host_evidence"))
        if (not isinstance(ref, list) or len(ref) != 6
                or any(not isinstance(part, str) or not part or part.strip() != part or "*" in part for part in ref)
                or tuple(ref[:3]) != tuple(getattr(owner, key) for key in _SCOPE[:3])
                or ref[3] == owner.user_id or ref[4] != value["source_id"]):
            _error("source_boundary_mismatch")
        _scope(dict(zip(_SCOPE, ref[:4])))
        if tuple(ref) in seen:
            _error("duplicate_source")
        seen.add(tuple(ref))
        if (not isinstance(version, list) or len(version) != 3
                or any(not isinstance(part, str) or not part for part in version)
                or any(_HEX.fullmatch(part) is None for part in version[:2])):
            _error("source_version_invalid")
        if (not isinstance(evidence, dict) or set(evidence) != _HOST_KEYS
                or evidence.get("schema") != HOST_SCHEMA
                or not isinstance(evidence["session_id"], str) or not evidence["session_id"]
                or any(type(evidence[key]) is not int or evidence[key] < 1
                       for key in ("user_message_id", "assistant_message_id"))
                or any(not isinstance(evidence[key], str) or _HEX.fullmatch(evidence[key]) is None
                       for key in ("user_id_digest", "chat_id_digest", "query_digest", "answer_digest", "turn_digest"))
                or item["host_evidence_digest"] != digest(evidence)
                or evidence["user_id_digest"] != _text_hash(owner.user_id)):
            _error("host_evidence_invalid")
    return deepcopy(value)


@contextmanager
def _host_snapshot(manifest, host_database):
    try:
        path = Path(host_database).resolve(strict=True)
        if str(path) != manifest["host_database"]:
            _error("host_database_mismatch")
        _require_reader_owner(path)
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise IdentityAuthorityError("subject_projection_host_unavailable") from exc
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db
    finally:
        db.rollback()
        db.close()


def _source_snapshot(sqlite, item):
    try:
        record, version = _repair_snapshot(sqlite, tuple(item["source_ref"]))
    except _RepairSkipped as exc:
        raise IdentityAuthorityError("subject_projection_source_" + exc.reason) from None
    if list(version) != item["source_version"]:
        _error("source_version_changed")
    metadata = business_metadata(record.meta)
    evidence = item["host_evidence"]
    if (record.kind != "memory" or record.status != "active" or record.source != "hermes.memory"
            or metadata.get("capture_origin") != "turn_sync"
            or metadata.get("session_id") != evidence["session_id"]
            or str(metadata.get("memory_type") or record.content.get("memory_type") or "") != "conversation"):
        _error("source_origin_unknown")
    text = record.content.get("text")
    if not isinstance(text, str) or _text_hash(text) != evidence["turn_digest"]:
        _error("host_turn_mismatch")
    return deepcopy(record)


def _verified_source(sqlite, host, manifest, item):
    record = _source_snapshot(sqlite, item)
    evidence = item["host_evidence"]
    session = host.execute("SELECT * FROM sessions WHERE id=?", (evidence["session_id"],)).fetchone()
    owner = manifest["owner_scope"]
    if session is None:
        _error("host_session_missing")
    try:
        origin = strict_json.loads(str(session["origin_json"]), max_bytes=65536, max_depth=8)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise IdentityAuthorityError("subject_projection_host_origin_unknown") from exc
    if (session["source"] != "feishu" or session["profile_name"] != owner["agent_id"]
            or session["user_id"] != owner["user_id"]
            or not isinstance(session["chat_id"], str) or not session["chat_id"]
            or _text_hash(session["chat_id"]) != evidence["chat_id_digest"]
            or not isinstance(origin, dict) or origin.get("platform") != "feishu"
            or origin.get("chat_type") != "dm" or origin.get("user_id") != owner["user_id"]
            or ("chat_id" in origin and origin["chat_id"] != session["chat_id"])):
        _error("host_subject_mismatch")
    messages = []
    for key, role, hash_key in (("user_message_id", "user", "query_digest"),
                                ("assistant_message_id", "assistant", "answer_digest")):
        message = host.execute("SELECT * FROM messages WHERE id=?", (evidence[key],)).fetchone()
        if (message is None or message["role"] != role or message["session_id"] != session["id"]
                or bool(message["_compressed_summary"]) or not isinstance(message["content"], str)
                or not message["content"].strip() or _text_hash(message["content"]) != evidence[hash_key]):
            _error("host_message_mismatch")
        messages.append(message)
    user, assistant = messages
    if (assistant["id"] <= user["id"] or assistant["timestamp"] < user["timestamp"]
            or host.execute("SELECT 1 FROM messages WHERE session_id=? AND role='user' AND id>? AND id<? LIMIT 1",
                            (session["id"], user["id"], assistant["id"])).fetchone() is not None):
        _error("host_turn_ambiguous")
    text = record.content.get("text")
    if (not isinstance(text, str) or text != "User: " + user["content"].strip() + "\nAssistant: " + assistant["content"].strip()
            or _text_hash(text) != evidence["turn_digest"]):
        _error("host_turn_mismatch")
    return deepcopy(record)


def _plan(sqlite, host, manifest):
    return [_verified_source(sqlite, host, manifest, item) for item in manifest["items"]]


def plan_subject_projection(store, manifest, *, host_database):
    """Validate source versions and actual host subjects without writing or granting."""
    proposal = _manifest(manifest)
    if isinstance(store, (str, Path)):
        path = Path(store).resolve(strict=True)
        if os.name == "posix" and os.geteuid() == 0 and path.stat().st_uid != 0:
            return _owner_process("plan", path, {"manifest": proposal, "host_database": str(host_database)})
        with readonly_subject_source(path) as reader:
            return plan_subject_projection(reader, proposal, host_database=host_database)
    _require_reader_owner(Path(store.sqlite.path))
    with _host_snapshot(proposal, host_database) as host:
        store.read_consistent(lambda sqlite: _plan(sqlite, host, proposal))
    return {"ok": True, "dry_run": True, "manifest_digest": digest(proposal),
            "planned_count": len(proposal["items"]), "written_count": 0,
            "authorization_verified": False,
            "original_records_changed": 0, "natural_samples_created": 0}


def authorize_subject_projection(store, manifest, *, host_database, directory, request_text, dry_run=True):
    """Use native root-owned policy/journal; a service or manifest cannot self-authorize."""
    proposal = _manifest(manifest)
    if type(dry_run) is not bool:
        _error("dry_run_invalid")
    if not isinstance(request_text, str) or not request_text.strip() or len(request_text) > 4000:
        _error("human_request_required")
    if dry_run:
        return plan_subject_projection(store, proposal, host_database=host_database)
    authority_api._require_root()
    with authority_api.snapshot(directory, exclusive=True) as (authority, fd):
        if (proposal["owner_scope"] != authority.policy["owner_scope"]
                or proposal["source_id"] not in authority.policy["source_ids"]):
            _error("owner_source_not_authorized")
        plan_subject_projection(store, proposal, host_database=host_database)
        refs = {tuple(item["source_ref"]) for item in proposal["items"]}
        for grant_id in authority.grants:
            grant = authority.active(grant_id)
            if grant is None:
                continue
            if grant["manifest"].get("schema") != SCHEMA:
                _error("authority_namespace_conflict")
            if grant["manifest_digest"] == digest(proposal):
                return {"ok": True, "grant_id": grant_id, "manifest_digest": digest(proposal), "idempotent": True}
            if any(tuple(item.get("source_ref", ())) in refs for item in grant["manifest"].get("items", [])):
                _error("overlapping_request")
        event = authority_api._append(authority, fd, {
            "operation": "authorize", "policy_digest": authority.policy_digest,
            "manifest_digest": digest(proposal), "manifest": proposal, "request_text": request_text,
        })
        return {"ok": True, "grant_id": event["event_id"], "manifest_digest": event["manifest_digest"], "idempotent": False}


def _active_grant(authority, grant_id):
    grant = authority.active(grant_id)
    if grant is None:
        _error("grant_not_active")
    proposal = _manifest(grant["manifest"])
    if not isinstance(grant.get("request_text"), str) or not grant["request_text"].strip():
        _error("human_request_required")
    try:
        timestamp = datetime.fromisoformat(grant["requested_at"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("unbound time")
    except (KeyError, TypeError, ValueError) as exc:
        raise IdentityAuthorityError("subject_projection_grant_time_invalid") from exc
    return grant, proposal


def _projection(record, manifest, item, grant):
    created = grant["requested_at"]
    lineage = {"schema": LINEAGE_SCHEMA, "grant_id": grant["event_id"],
               "manifest_digest": grant["manifest_digest"], "policy_digest": grant["policy_digest"],
               "authority_event_hash": grant["event_hash"], "root_request_digest": _text_hash(grant["request_text"]),
               "original_ref": item["source_ref"], "original_version": item["source_version"],
               "host_evidence_digest": item["host_evidence_digest"],
               "original_created_at": record.time.created_at, "original_occurred_at": record.time.occurred_at,
               "projection_created_at": created}
    projected = RecordEnvelope.create(
        # The host binding attests the whole actual turn, not unrelated legacy
        # title/summary/detail projections that may have independent drift.
        kind="memory", title="Hermes completed turn", summary=record.content["text"], detail=record.content["text"],
        content={"text": record.content["text"], "memory_type": "conversation"},
        scope=ScopeRef.from_dict(manifest["owner_scope"]), source=SOURCE, source_id=manifest["source_id"],
        meta={"memory_type": "conversation", "memory_layer": "l0", "subject_projection": lineage},
        provenance={"identity_scope_preserved": True, "subject_projection": lineage},
    )
    projected.record_id = "mem_sproj_" + digest({"grant_id": grant["event_id"], "item": item})[:32]
    projected.time = TimeRef(created, created, record.time.occurred_at)
    # Scoring is recomputed for this producer, never copied from the original.
    # Its capture timestamp must be the authenticated projection event time so
    # retry/consumption reconstruction is byte-identical across wall-clock ticks.
    metadata = business_metadata(projected.meta)
    metadata["scoring"]["memory_score_v1"]["provenance"]["generated_at"] = created
    projected.meta = normalize_metadata(metadata)
    return projected


def _prepare_projection(sqlite, host, manifest, grant):
    records = _plan(sqlite, host, manifest)
    result = []
    for item, source in zip(manifest["items"], records):
        projected = _projection(source, manifest, item, grant)
        # The physical unique key excludes source_id; never overwrite a foreign partition.
        rows = sqlite.execute("SELECT source_id,payload_pointer_json FROM records WHERE tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? AND record_id=?",
                              (*[manifest["owner_scope"][key] for key in _SCOPE], projected.record_id)).fetchall()
        if any(str(row["payload_pointer_json"] or "") for row in rows):
            _error("target_archived_unsupported")
        existing = sqlite.get_by_exact_ref(projected.record_id, scope=projected.scope, source_id=projected.source_id)
        if rows and (len(rows) != 1 or existing is None or canonical(existing.to_dict()) != canonical(projected.to_dict())):
            _error("target_conflict")
        result.append((projected, existing is not None))
    return result


def apply_subject_projection(runtime, *, grant_id, host_database, directory, dry_run=True):
    """Recheck the root grant, actual host turns and native versions at atomic append."""
    if type(dry_run) is not bool:
        _error("dry_run_invalid")
    _require_reader_owner(Path(runtime.store.sqlite.path))
    with authority_api.snapshot(directory) as (authority, _):
        grant, manifest = _active_grant(authority, grant_id)
        with _host_snapshot(manifest, host_database) as host:
            def action(sqlite):
                prepared = _prepare_projection(sqlite, host, manifest, grant)
                changed = [record for record, exists in prepared if not exists]
                if not dry_run:
                    for record in changed:
                        sqlite.upsert(record, commit=False)
                report = {"ok": True, "dry_run": bool(dry_run), "grant_id": grant_id,
                          "manifest_digest": grant["manifest_digest"], "planned_count": len(prepared),
                          "written_count": 0 if dry_run else len(changed), "idempotent": not changed,
                          "record_ids": [record.record_id for record, _ in prepared],
                          "original_records_changed": 0, "natural_samples_created": 0}
                return report, ([] if dry_run else changed), []
            if dry_run:
                return runtime.store.read_consistent(lambda sqlite: action(sqlite)[0])
            return runtime.store.mutate_records_atomically(action)


def preview_subject_projection_apply(database, *, grant_id, host_database, directory):
    """Read inline targets under the database owner without schema/bootstrap."""
    path = Path(database).resolve(strict=True)
    if os.name == "posix" and os.geteuid() == 0 and path.stat().st_uid != 0:
        return _owner_process("preview_apply", path, {"grant_id": grant_id,
            "host_database": str(host_database), "directory": str(directory)})
    with readonly_subject_source(path) as reader:
        return apply_subject_projection(SimpleNamespace(store=reader), grant_id=grant_id,
            host_database=host_database, directory=directory)


def projection_authority_directory(value=None):
    """Server configuration only; a record can never select an authority path."""
    return Path(value or os.environ.get("EIMEMORY_SUBJECT_PROJECTION_AUTHORITY_DIR")
                or DEFAULT_AUTHORITY_DIRECTORY)


def is_subject_projection(record):
    """Any reserved marker triggers validation, including partial/forged markers."""
    meta = record.meta if isinstance(record.meta, dict) else {}
    provenance = record.provenance if isinstance(record.provenance, dict) else {}
    return (record.source == SOURCE or record.record_id.startswith("mem_sproj_")
            or "subject_projection" in meta or "subject_projection" in business_metadata(meta)
            or "subject_projection" in provenance)


def subject_projection_validation_error(sqlite, record, *, authority=None, scope=None, source_ids=None):
    """Pure transaction-local admission: ordinary records pass without authority.

    The supplied authority is an open native server-configured snapshot. Its
    immutable grant attests the host evidence verified at authorization/apply;
    consumption therefore needs no mutable or subsequently pruned host history.
    Old physical versions and active authority are still checked on every read.
    No path in the record or its lineage is opened or trusted.
    """
    if not is_subject_projection(record):
        return ""
    try:
        if authority is None:
            _error("authority_unavailable")
        if scope is not None and asdict(record.scope) != asdict(_scope(asdict(scope) if isinstance(scope, ScopeRef) else scope)):
            _error("requested_scope_mismatch")
        if source_ids is not None and record.source_id not in source_ids:
            _error("requested_source_mismatch")
        lineage = business_metadata(record.meta).get("subject_projection")
        if record.source != SOURCE or not isinstance(lineage, dict) or lineage.get("schema") != LINEAGE_SCHEMA:
            _error("lineage_invalid")
        grant, manifest = _active_grant(authority, lineage.get("grant_id"))
        matches = [item for item in manifest["items"]
                   if "mem_sproj_" + digest({"grant_id": grant["event_id"], "item": item})[:32] == record.record_id]
        if len(matches) != 1:
            _error("projection_mismatch")
        item = matches[0]
        source = _source_snapshot(sqlite, item)
        expected = _projection(source, manifest, item, grant)
        authoritative = sqlite.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id)
        if (authoritative is None or canonical(authoritative.to_dict()) != canonical(expected.to_dict())
                or canonical(record.to_dict()) != canonical(expected.to_dict())):
            _error("projection_mismatch")
        return ""
    except (IdentityAuthorityError, _RepairSkipped) as exc:
        return str(exc) if isinstance(exc, IdentityAuthorityError) else "subject_projection_source_invalid"
    except (KeyError, IndexError, TypeError, ValueError, RuntimeError, sqlite3.Error):
        return "subject_projection_validation_unavailable"


def validate_subject_projection(store, record, *, directory=None):
    """Fail closed on revoked grants, source drift, wrong scope, or altered projections.

    This function grants no wider scope and returns no rewritten envelope.
    Shared recall entry points deliberately remain outside this module.
    """
    if not is_subject_projection(record):
        return True
    with authority_api.snapshot(projection_authority_directory(directory)) as (authority, _):
        reason = store.read_consistent(lambda sqlite: subject_projection_validation_error(sqlite, record, authority=authority))
        if reason:
            raise IdentityAuthorityError(reason)
        return True
