"""Bounded exact-scope knowledge inventory and durable invalidation metadata.

This leaf owns its SQLite schema under RuntimeStore's transaction boundary.
Inventory pages never hydrate record bodies. A caller scans by storage key,
then reconciles the monotonic change log from its initial high-water mark.
The log deliberately excludes new review/checkpoint receipts: their creation
must not perpetually invalidate the source scan that created them.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import re
from typing import Any

from eimemory.models.records import ScopeRef

PAGE_LIMIT = 200
SCHEMA = "knowledge_review_inventory.v1"
_PREFIX = "knowledge_review_inventory_"
_SCOPE_COLUMNS = ("tenant_id", "agent_id", "workspace_id", "user_id")
_WHERE = "tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=?"
_SOURCE_KINDS = ("claim_card", "knowledge_page", "paper_source", "knowledge_candidate")
_KNOWLEDGE_KINDS = ("claim_card", "knowledge_page")
_REVIEW_STATUSES = {"pending", "unknown", "unavailable", "completed", "ineligible", "revoked"}
_REVIEW_TYPE = "knowledge.gap_applicability_review.v1"
_REVOCATION_TYPE = "knowledge_gap_applicability_review_revocation"
_REVIEW_SOURCE = "eimemory.governance.capability.knowledge_gap_review"
_ROW_COLUMNS = "storage_key,record_id,kind,status,source_id,payload_digest,tenant_id,agent_id,workspace_id,user_id"


class KnowledgeReviewInventoryError(ValueError):
    """Fixed diagnostic code, without private record data."""


def _scope(scope: ScopeRef | Mapping[str, str]) -> tuple[str, ...]:
    if isinstance(scope, ScopeRef):
        values = tuple(getattr(scope, key) for key in _SCOPE_COLUMNS)
    elif isinstance(scope, Mapping) and set(scope) == set(_SCOPE_COLUMNS):
        values = tuple(scope[key] for key in _SCOPE_COLUMNS)
    else:
        raise KnowledgeReviewInventoryError("inventory_exact_scope_required")
    if any(not isinstance(value, str) for value in values) or not values[0].strip():
        raise KnowledgeReviewInventoryError("inventory_exact_scope_required")
    return values


def _limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PAGE_LIMIT:
        raise KnowledgeReviewInventoryError("inventory_page_limit_invalid")
    return limit


def _text(value: Any, *, maximum: int = 4096, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value):
        raise KnowledgeReviewInventoryError("inventory_metadata_invalid")
    return value


def _version(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise KnowledgeReviewInventoryError("inventory_watermark_invalid")
    return value


def _timestamp(value: str, *, empty: bool = False) -> str:
    _text(value, maximum=64, empty=empty)
    if empty and not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise KnowledgeReviewInventoryError("inventory_transition_invalid") from None
    return parsed.astimezone(timezone.utc).isoformat()


def _review_condition(alias: str, report_type: str) -> str:
    # Native receipts expose their classification in compact metadata, so the
    # trigger does not inspect payload_json or external archived bodies.
    return (f"({alias}.kind='reflection' AND {alias}.source='{_REVIEW_SOURCE}' "
            f"AND CASE WHEN json_valid({alias}.meta_json) THEN "
            f"json_extract({alias}.meta_json,'$.report_type') ELSE '' END='{report_type}')")


def _relevant(alias: str, *, include_review: bool) -> str:
    source = f"{alias}.kind IN ('claim_card','knowledge_page','paper_source','knowledge_candidate')"
    parts = [source, _review_condition(alias, _REVOCATION_TYPE)]
    if include_review:
        parts.append(_review_condition(alias, _REVIEW_TYPE))
    return "(" + " OR ".join(parts) + ")"


def _event_insert(alias: str, operation: str, *, include_review: bool, predicate: str | None = None) -> str:
    predicate = predicate or _relevant(alias, include_review=include_review)
    return ("INSERT INTO knowledge_review_inventory_changes "
            "(operation,storage_key,record_id,kind,status,source_id,payload_digest,"
            "tenant_id,agent_id,workspace_id,user_id,category,prior_version) SELECT "
            f"'{operation}'," + ",".join(f"{alias}.{key}" for key in
                ("storage_key", "record_id", "kind", "status", "source_id", "payload_digest", *_SCOPE_COLUMNS))
            + f",CASE WHEN {_review_condition(alias, _REVOCATION_TYPE)} THEN 'revocation' "
            + f"WHEN {_review_condition(alias, _REVIEW_TYPE)} THEN 'review' ELSE 'input' END, "
            + "COALESCE((SELECT high FROM knowledge_review_inventory_watermarks WHERE "
            + " AND ".join(f"{key}={alias}.{key}" for key in _SCOPE_COLUMNS) + "),0) "
            + f"WHERE {predicate};"
            + "INSERT INTO knowledge_review_inventory_watermarks "
            + "SELECT " + ",".join(f"{alias}.{key}" for key in _SCOPE_COLUMNS)
            + ",(SELECT MAX(version) FROM knowledge_review_inventory_changes),0 "
            + f"WHERE {predicate} "
            + "ON CONFLICT(tenant_id,agent_id,workspace_id,user_id) DO UPDATE SET high=excluded.high;")


_DDL = {
    "knowledge_review_inventory_schema": """CREATE TABLE knowledge_review_inventory_schema (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema TEXT NOT NULL)""",
    "knowledge_review_inventory_changes": """CREATE TABLE knowledge_review_inventory_changes (
        version INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT NOT NULL,
        storage_key TEXT NOT NULL, record_id TEXT NOT NULL, kind TEXT NOT NULL,
        status TEXT NOT NULL, source_id TEXT NOT NULL, payload_digest TEXT NOT NULL,
        tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
        user_id TEXT NOT NULL, category TEXT NOT NULL, prior_version INTEGER NOT NULL)""",
    "knowledge_review_inventory_scan_state": """CREATE TABLE knowledge_review_inventory_scan_state (
        tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
        user_id TEXT NOT NULL, target_key TEXT NOT NULL, state_json TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>0),
        PRIMARY KEY(tenant_id,agent_id,workspace_id,user_id,target_key))""",
    "knowledge_review_inventory_watermarks": """CREATE TABLE knowledge_review_inventory_watermarks (
        tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
        user_id TEXT NOT NULL, high INTEGER NOT NULL, floor INTEGER NOT NULL,
        PRIMARY KEY(tenant_id,agent_id,workspace_id,user_id))""",
    "knowledge_review_inventory_manifest": """CREATE TABLE knowledge_review_inventory_manifest (
        tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
        user_id TEXT NOT NULL, target_key TEXT NOT NULL, storage_key TEXT NOT NULL,
        record_id TEXT NOT NULL, source_id TEXT NOT NULL, kind TEXT NOT NULL,
        status TEXT NOT NULL, payload_digest TEXT NOT NULL, input_digest TEXT NOT NULL, review_receipt_id TEXT NOT NULL,
        review_status TEXT NOT NULL, dirty INTEGER NOT NULL CHECK(dirty IN (0,1)),
        applicability TEXT NOT NULL, reason TEXT NOT NULL, next_transition TEXT NOT NULL,
        attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
        PRIMARY KEY(tenant_id,agent_id,workspace_id,user_id,target_key,storage_key))""",
    "knowledge_review_inventory_dependencies": """CREATE TABLE knowledge_review_inventory_dependencies (
        tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
        user_id TEXT NOT NULL, target_key TEXT NOT NULL, knowledge_storage_key TEXT NOT NULL,
        storage_key TEXT NOT NULL, record_id TEXT NOT NULL, source_id TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        PRIMARY KEY(tenant_id,agent_id,workspace_id,user_id,target_key,knowledge_storage_key,storage_key,record_id))""",
    "knowledge_review_inventory_changes_scope": """CREATE INDEX knowledge_review_inventory_changes_scope
        ON knowledge_review_inventory_changes(tenant_id,agent_id,workspace_id,user_id,version)""",
    "knowledge_review_inventory_manifest_dirty": """CREATE INDEX knowledge_review_inventory_manifest_dirty
        ON knowledge_review_inventory_manifest(tenant_id,agent_id,workspace_id,user_id,target_key,dirty,attempt_count,storage_key)""",
    "knowledge_review_inventory_manifest_input": """CREATE INDEX knowledge_review_inventory_manifest_input
        ON knowledge_review_inventory_manifest(tenant_id,agent_id,workspace_id,user_id,input_digest)""",
    "knowledge_review_inventory_dependencies_ref": """CREATE INDEX knowledge_review_inventory_dependencies_ref
        ON knowledge_review_inventory_dependencies(tenant_id,agent_id,workspace_id,user_id,target_key,record_id,source_id)""",
    "knowledge_review_inventory_inputs": """CREATE INDEX knowledge_review_inventory_inputs
        ON records(tenant_id,agent_id,workspace_id,user_id,storage_key,record_id,kind,status,source_id,payload_digest)
        WHERE kind IN ('claim_card','knowledge_page')""",
}
_REPORT_EXPR = "CASE WHEN json_valid(meta_json) THEN json_extract(meta_json,'$.report_type') ELSE '' END"
_DIGEST_EXPR = "CASE WHEN json_valid(meta_json) THEN json_extract(meta_json,'$.input_digest') ELSE '' END"
_REVOCATION_REF_EXPR = ("COALESCE(CASE WHEN json_valid(meta_json) THEN json_extract(meta_json,'$.review_receipt_id') END,"
                        "CASE WHEN json_valid(payload_json) THEN json_extract(payload_json,'$.content.review_receipt_id') END,'')")


def _status_expr(alias: str = "") -> str:
    prefix = alias + "." if alias else ""
    return (f"COALESCE(CASE WHEN json_valid({prefix}meta_json) THEN json_extract({prefix}meta_json,'$.review_status') END,"
            f"CASE WHEN json_valid({prefix}payload_json) THEN json_extract({prefix}payload_json,'$.content.status') END,'')")


def _digest_expr(alias: str) -> str:
    return f"CASE WHEN json_valid({alias}.meta_json) THEN json_extract({alias}.meta_json,'$.input_digest') ELSE '' END"


_DDL["knowledge_review_inventory_review_input"] = (
    "CREATE INDEX knowledge_review_inventory_review_input ON records(tenant_id,agent_id,workspace_id,user_id,"
    + _DIGEST_EXPR + ",storage_key) WHERE kind='reflection' AND source='" + _REVIEW_SOURCE
    + "' AND " + _REPORT_EXPR + "='" + _REVIEW_TYPE + "'")
_DDL["knowledge_review_inventory_revocation_ref"] = (
    "CREATE INDEX knowledge_review_inventory_revocation_ref ON records(tenant_id,agent_id,workspace_id,user_id,"
    + _REVOCATION_REF_EXPR + ",storage_key) WHERE kind='reflection' AND source='" + _REVIEW_SOURCE
    + "' AND " + _REPORT_EXPR + "='" + _REVOCATION_TYPE + "'")
_CONFLICTING_REVIEW_INSERT = (
    _review_condition("NEW", _REVIEW_TYPE) + " AND " + _status_expr("NEW") + "='completed' AND EXISTS ("
    "SELECT 1 FROM records AS prior INDEXED BY knowledge_review_inventory_review_input WHERE "
    + " AND ".join(f"prior.{key}=NEW.{key}" for key in _SCOPE_COLUMNS)
    + " AND prior.kind='reflection' AND prior.source='" + _REVIEW_SOURCE + "' AND "
    + _review_condition("prior", _REVIEW_TYPE) + " AND "
    + _digest_expr("prior") + "=" + _digest_expr("NEW") + " AND prior.storage_key!=NEW.storage_key LIMIT 1)")


def _dirty_review_input(alias: str) -> str:
    return ("UPDATE knowledge_review_inventory_manifest SET dirty=1 WHERE "
            + " AND ".join(f"{key}={alias}.{key}" for key in _SCOPE_COLUMNS)
            + " AND input_digest=" + _digest_expr(alias)
            + " AND " + _review_condition(alias, _REVIEW_TYPE) + ";")


_DDL["knowledge_review_inventory_insert"] = (
    "CREATE TRIGGER knowledge_review_inventory_insert AFTER INSERT ON records BEGIN "
    + _event_insert("NEW", "insert", include_review=False)
    + _event_insert("NEW", "insert", include_review=True, predicate=_CONFLICTING_REVIEW_INSERT)
    + "UPDATE knowledge_review_inventory_manifest SET dirty=1 WHERE "
    + " AND ".join(f"{key}=NEW.{key}" for key in _SCOPE_COLUMNS)
    + " AND input_digest=" + _digest_expr("NEW") + " AND " + _CONFLICTING_REVIEW_INSERT + "; END")
_DDL["knowledge_review_inventory_update"] = (
    "CREATE TRIGGER knowledge_review_inventory_update AFTER UPDATE ON records BEGIN "
    + _event_insert("OLD", "before_update", include_review=True)
    + _event_insert("NEW", "after_update", include_review=True)
    + _dirty_review_input("OLD") + _dirty_review_input("NEW") + " END")
_DDL["knowledge_review_inventory_delete"] = (
    "CREATE TRIGGER knowledge_review_inventory_delete AFTER DELETE ON records BEGIN "
    + _event_insert("OLD", "delete", include_review=True) + _dirty_review_input("OLD") + " END")


def _normalized_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().rstrip(";")


def _validate(sqlite) -> None:
    rows = sqlite.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE ?", (_PREFIX + "%",)).fetchall()
    actual = {str(row["name"]): str(row["sql"] or "") for row in rows}
    if set(actual) != set(_DDL) or any(name not in actual or _normalized_sql(actual[name]) != _normalized_sql(sql)
           for name, sql in _DDL.items()):
        raise KnowledgeReviewInventoryError("inventory_schema_invalid")
    row = sqlite.execute("SELECT singleton,schema FROM knowledge_review_inventory_schema").fetchall()
    if len(row) != 1 or row[0]["singleton"] != 1 or row[0]["schema"] != SCHEMA:
        raise KnowledgeReviewInventoryError("inventory_schema_invalid")


def ensure(store) -> None:
    """Install all triggers atomically before any generation-zero scan begins.

    Missing/altered objects after installation fail closed; they are never
    silently recreated after potentially missing a source mutation.
    """
    def install(sqlite):
        found = sqlite.execute("SELECT name FROM sqlite_master WHERE name LIKE ?", (_PREFIX + "%",)).fetchall()
        if found:
            _validate(sqlite)
            return
        if sqlite.in_transaction:
            raise KnowledgeReviewInventoryError("inventory_schema_requires_own_transaction")
        sqlite.execute("BEGIN IMMEDIATE")
        try:
            for sql in _DDL.values():
                sqlite.execute(sql)
            sqlite.execute("INSERT INTO knowledge_review_inventory_schema VALUES(1,?)", (SCHEMA,))
            _validate(sqlite)
            sqlite.commit()
        except Exception:
            sqlite.rollback()
            raise
    store.run_locked(install)


def _watermark(sqlite, scope) -> int:
    row = sqlite.execute("SELECT high FROM knowledge_review_inventory_watermarks WHERE " + _WHERE, scope).fetchone()
    return int(row["high"]) if row else 0


def current_watermark(store, scope) -> int:
    exact = _scope(scope)
    ensure(store)
    return store.read_consistent(lambda sqlite: _watermark(sqlite, exact))


def _compact(row) -> dict:
    value = dict(row)
    value["scope"] = {key: value.pop(key) for key in _SCOPE_COLUMNS}
    return value


def inventory_page(store, scope, *, cursor: str = "", limit: int = PAGE_LIMIT) -> dict:
    """Read at most ``limit`` compact inputs; no body/meta or hydration reads."""
    exact, bound, cursor = _scope(scope), _limit(limit), _text(cursor, empty=True)
    ensure(store)
    def read(sqlite):
        watermark = _watermark(sqlite, exact)
        rows = sqlite.execute("SELECT " + _ROW_COLUMNS + " FROM records INDEXED BY knowledge_review_inventory_inputs WHERE " + _WHERE
            + " AND kind IN ('claim_card','knowledge_page') AND storage_key>? ORDER BY storage_key LIMIT ?",
            (*exact, cursor, bound + 1)).fetchall()
        selected = rows[:bound]
        return {"rows": [_compact(row) for row in selected], "cursor": str(selected[-1]["storage_key"]) if selected else cursor,
                "complete": len(rows) <= bound, "page_count": len(selected), "watermark": watermark}
    return store.read_consistent(read)


def changes_page(store, scope, *, after_version: int, through_version: int,
                 limit: int = PAGE_LIMIT) -> dict:
    exact, after, through, bound = _scope(scope), _version(after_version), _version(through_version), _limit(limit)
    if through < after:
        raise KnowledgeReviewInventoryError("inventory_watermark_invalid")
    ensure(store)
    def read(sqlite):
        high = _watermark(sqlite, exact)
        marker = sqlite.execute("SELECT floor FROM knowledge_review_inventory_watermarks WHERE " + _WHERE, exact).fetchone()
        floor = int(marker["floor"]) if marker else 0
        if after < floor:
            raise KnowledgeReviewInventoryError("inventory_change_history_gap")
        if through > high:
            raise KnowledgeReviewInventoryError("inventory_watermark_unavailable")
        rows = sqlite.execute("SELECT * FROM knowledge_review_inventory_changes WHERE " + _WHERE
            + " AND version>? AND version<=? ORDER BY version LIMIT ?", (*exact, after, through, bound + 1)).fetchall()
        prior = after
        for row in rows:
            if int(row["prior_version"]) != prior:
                raise KnowledgeReviewInventoryError("inventory_change_history_gap")
            prior = int(row["version"])
        selected = rows[:bound]
        complete = len(rows) <= bound
        if complete and prior != through:
            raise KnowledgeReviewInventoryError("inventory_change_history_gap")
        return {"changes": [_compact(row) for row in selected], "cursor": through if complete else int(selected[-1]["version"]),
                "complete": complete, "page_count": len(selected), "watermark": high, "floor": floor}
    return store.read_consistent(read)


def count_inputs(store, scope) -> int:
    exact = _scope(scope)
    ensure(store)
    return store.read_consistent(lambda sqlite: int(sqlite.execute("SELECT COUNT(*) AS count FROM records WHERE "
        + _WHERE + " AND kind IN ('claim_card','knowledge_page')", exact).fetchone()["count"]))


def get_input_row(store, scope, storage_key: str) -> dict | None:
    exact, key = _scope(scope), _text(storage_key)
    ensure(store)
    def read(sqlite):
        row = sqlite.execute("SELECT " + _ROW_COLUMNS + " FROM records WHERE " + _WHERE
            + " AND storage_key=?", (*exact, key)).fetchone()
        return _compact(row) if row is not None else None
    return store.read_consistent(read)


def record_snapshot(store, scope, record_id: str, *, source_id: str | None = None):
    """Verified hydration and its storage digest from the same exact-scope snapshot."""
    from eimemory.models.source_partitions import normalize_source_id
    exact, identifier = _scope(scope), _text(record_id)
    source_id = normalize_source_id(source_id) if source_id is not None else None
    ensure(store)
    def read(sqlite):
        row = sqlite.execute("SELECT " + _ROW_COLUMNS + ",payload_json,payload_pointer_json,created_at,updated_at FROM records WHERE "
            + _WHERE + " AND record_id=?" + (" AND source_id=?" if source_id is not None else "")
            + " ORDER BY updated_at DESC LIMIT 1", (*exact, identifier, *((source_id,) if source_id is not None else ()))).fetchone()
        if row is None or not row["payload_digest"]:
            return None
        # Hash the stored body in the native reader, before hydration adds defaults.
        record = sqlite._record_from_storage_row(row, hydrate=True, preserve_unknown_times=True)
        if (record is None or not sqlite._record_matches_projection_row(record, row)
                or sqlite._storage_key(record) != row["storage_key"]):
            return None
        compact = {key: row[key] for key in _ROW_COLUMNS.split(",")}
        return record, _compact(compact)
    return store.read_consistent(read)


def _mutate(store, writer):
    ensure(store)
    def transact(sqlite):
        if sqlite.in_transaction:
            raise KnowledgeReviewInventoryError("inventory_write_requires_own_transaction")
        sqlite.execute("BEGIN IMMEDIATE")
        try:
            result = writer(sqlite)
            sqlite.commit()
            return result
        except Exception:
            sqlite.rollback()
            raise
    return store.run_locked(transact)


def load_scan_state(store, scope, target_key: str) -> dict | None:
    exact, target = _scope(scope), _text(target_key)
    ensure(store)
    def read(sqlite):
        row = sqlite.execute("SELECT state_json,revision FROM knowledge_review_inventory_scan_state WHERE " + _WHERE
            + " AND target_key=?", (*exact, target)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row["state_json"]))
        except (TypeError, ValueError):
            raise KnowledgeReviewInventoryError("inventory_scan_state_invalid") from None
        if not isinstance(value, dict):
            raise KnowledgeReviewInventoryError("inventory_scan_state_invalid")
        value["_revision"] = int(row["revision"])
        return value
    return store.read_consistent(read)


def save_scan_state(store, scope, target_key: str, state: dict, *, expected_revision: int | None = None) -> int:
    exact, target = _scope(scope), _text(target_key)
    if not isinstance(state, dict):
        raise KnowledgeReviewInventoryError("inventory_scan_state_invalid")
    if expected_revision is not None:
        _version(expected_revision)
    state = {key: value for key, value in state.items() if key != "_revision"}
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if len(encoded.encode()) > 32768:
        raise KnowledgeReviewInventoryError("inventory_scan_state_too_large")
    def write(sqlite):
        row = sqlite.execute("SELECT revision FROM knowledge_review_inventory_scan_state WHERE " + _WHERE
            + " AND target_key=?", (*exact, target)).fetchone()
        revision = int(row["revision"]) if row else 0
        if expected_revision is not None and expected_revision != revision:
            raise KnowledgeReviewInventoryError("inventory_scan_state_conflict")
        sqlite.execute("INSERT INTO knowledge_review_inventory_scan_state VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(tenant_id,agent_id,workspace_id,user_id,target_key) DO UPDATE SET state_json=excluded.state_json,revision=excluded.revision",
            (*exact, target, encoded, revision + 1))
        return revision + 1
    return _mutate(store, write)


def upsert_manifest_entry(store, scope, target_key: str, entry: dict) -> bool:
    exact, target = _scope(scope), _text(target_key)
    keys = ("storage_key", "record_id", "source_id", "kind", "status", "payload_digest", "input_digest", "review_receipt_id", "review_status")
    values = tuple(_text(entry.get(key, ""), empty=key in {"payload_digest", "input_digest", "review_receipt_id"}) for key in keys)
    if values[3] not in _KNOWLEDGE_KINDS:
        raise KnowledgeReviewInventoryError("inventory_manifest_kind_invalid")
    if values[8] not in _REVIEW_STATUSES:
        raise KnowledgeReviewInventoryError("inventory_review_status_invalid")
    dirty = entry.get("dirty", True)
    if not isinstance(dirty, bool):
        raise KnowledgeReviewInventoryError("inventory_manifest_dirty_invalid")
    applicability = entry.get("applicability", "")
    reason = entry.get("reason", "")
    next_transition = entry.get("next_transition", "")
    attempt_count = entry.get("attempt_count", 0)
    if applicability not in {"", "applicable", "not_applicable"}:
        raise KnowledgeReviewInventoryError("inventory_applicability_invalid")
    if not dirty and values[8] == "completed" and not applicability:
        raise KnowledgeReviewInventoryError("inventory_applicability_invalid")
    if not dirty and values[8] == "completed" and re.fullmatch(r"[0-9a-f]{64}", values[6]) is None:
        raise KnowledgeReviewInventoryError("inventory_input_digest_invalid")
    if not isinstance(reason, str) or re.fullmatch(r"[a-z0-9_]{0,160}", reason) is None:
        raise KnowledgeReviewInventoryError("inventory_reason_invalid")
    next_transition = _timestamp(next_transition, empty=True)
    _version(attempt_count)
    dependencies = entry.get("dependencies", [])
    if not isinstance(dependencies, list) or len(dependencies) > 20:
        raise KnowledgeReviewInventoryError("inventory_dependencies_invalid")
    deps = []
    for dependency in dependencies:
        if not isinstance(dependency, Mapping):
            raise KnowledgeReviewInventoryError("inventory_dependencies_invalid")
        deps.append(tuple(_text(dependency.get(key, ""), empty=key in {"storage_key", "payload_digest"})
                          for key in ("storage_key", "record_id", "source_id", "payload_digest")))
    def write(sqlite):
        effective_dirty = dirty
        current = sqlite.execute("SELECT " + _ROW_COLUMNS + " FROM records WHERE " + _WHERE
            + " AND storage_key=? AND kind IN ('claim_card','knowledge_page')", (*exact, values[0])).fetchone()
        if current is None:
            _delete_entry(sqlite, exact, target, values[0])
            return False
        if any(str(current[key]) != values[index] for index, key in enumerate(keys[:6])):
            effective_dirty = True
        if not dirty:
            if values[8] == "completed":
                reviews = _completed_review_refs(sqlite, exact, values[6])
                if len(reviews) != 1 or reviews[0] != values[7]:
                    effective_dirty = True
            for storage_key, record_id, source_id, digest in deps:
                if source_id == "*" and not digest:
                    exists = sqlite.execute("SELECT 1 FROM records WHERE " + _WHERE
                        + " AND record_id=? LIMIT 1", (*exact, record_id)).fetchone()
                    if exists is not None:
                        effective_dirty = True
                    continue
                row = sqlite.execute("SELECT storage_key,payload_digest FROM records WHERE " + _WHERE
                    + " AND record_id=? AND source_id=?" + (" AND storage_key=?" if storage_key else ""),
                    (*exact, record_id, source_id, *((storage_key,) if storage_key else ()))).fetchone()
                if row is None or not digest or row["payload_digest"] != digest:
                    effective_dirty = True
        current_values = tuple(str(current[key]) for key in keys[:6]) + values[6:]
        sqlite.execute("INSERT INTO knowledge_review_inventory_manifest VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(tenant_id,agent_id,workspace_id,user_id,target_key,storage_key) DO UPDATE SET "
            "record_id=excluded.record_id,source_id=excluded.source_id,kind=excluded.kind,status=excluded.status,"
            "payload_digest=excluded.payload_digest,input_digest=excluded.input_digest,review_receipt_id=excluded.review_receipt_id,review_status=excluded.review_status,dirty=excluded.dirty,"
            "applicability=excluded.applicability,reason=excluded.reason,next_transition=excluded.next_transition,attempt_count=excluded.attempt_count",
            (*exact, target, *current_values, int(effective_dirty), applicability, reason, next_transition, attempt_count))
        sqlite.execute("DELETE FROM knowledge_review_inventory_dependencies WHERE " + _WHERE
            + " AND target_key=? AND knowledge_storage_key=?", (*exact, target, values[0]))
        for dependency in deps:
            sqlite.execute("INSERT OR REPLACE INTO knowledge_review_inventory_dependencies VALUES(?,?,?,?,?,?,?,?,?,?)",
                (*exact, target, values[0], *dependency))
        return not effective_dirty
    return _mutate(store, write)


def manifest_page(store, scope, target_key: str, *, cursor: str = "", limit: int = PAGE_LIMIT,
                  dirty_only: bool = False) -> dict:
    exact, target, cursor, bound = _scope(scope), _text(target_key), _text(cursor, empty=True), _limit(limit)
    ensure(store)
    attempt, after_key = 0, cursor
    if dirty_only and cursor:
        try:
            decoded = json.loads(cursor)
            attempt, after_key = _version(decoded["attempt_count"]), _text(decoded["storage_key"])
        except (KeyError, TypeError, ValueError):
            raise KnowledgeReviewInventoryError("inventory_manifest_cursor_invalid") from None
    def read(sqlite):
        rows = sqlite.execute("SELECT * FROM knowledge_review_inventory_manifest WHERE " + _WHERE
            + " AND target_key=?" + (" AND dirty=1 AND (attempt_count>? OR (attempt_count=? AND storage_key>?))"
                                       if dirty_only else " AND storage_key>?")
            + (" ORDER BY attempt_count,storage_key LIMIT ?" if dirty_only else " ORDER BY storage_key LIMIT ?"),
            (*exact, target, *((attempt, attempt, after_key) if dirty_only else (cursor,)), bound + 1)).fetchall()
        selected = rows[:bound]
        values = []
        for row in selected:
            value = _compact(row)
            value["dirty"] = bool(value["dirty"])
            value["dependencies"] = [dict(dependency) for dependency in sqlite.execute(
                "SELECT storage_key,record_id,source_id,payload_digest FROM knowledge_review_inventory_dependencies WHERE "
                + _WHERE + " AND target_key=? AND knowledge_storage_key=? ORDER BY storage_key,record_id LIMIT 21",
                (*exact, target, row["storage_key"])).fetchall()]
            if len(value["dependencies"]) > 20:
                raise KnowledgeReviewInventoryError("inventory_dependencies_invalid")
            values.append(value)
        next_cursor = cursor
        if selected:
            next_cursor = (json.dumps({"attempt_count": int(selected[-1]["attempt_count"]), "storage_key": str(selected[-1]["storage_key"])},
                                      separators=(",", ":")) if dirty_only else str(selected[-1]["storage_key"]))
        return {"rows": values, "cursor": next_cursor,
                "complete": len(rows) <= bound, "page_count": len(selected)}
    return store.read_consistent(read)


def manifest_counts(store, scope, target_key: str) -> dict:
    exact, target = _scope(scope), _text(target_key)
    ensure(store)
    def read(sqlite):
        rows = sqlite.execute("SELECT review_status,dirty,applicability,status,COUNT(*) AS count,MIN(NULLIF(next_transition,'')) AS transition "
            "FROM knowledge_review_inventory_manifest WHERE "
            + _WHERE + " AND target_key=? GROUP BY review_status,dirty,applicability,status", (*exact, target)).fetchall()
        statuses: dict[str, int] = {}
        count = dirty = applicable = unknown = completed = eligible = 0
        transition = ""
        for row in rows:
            number = int(row["count"])
            count += number
            dirty += number if row["dirty"] else 0
            active = row["status"] == "active"
            eligible += number if active and row["review_status"] != "ineligible" else 0
            applicable += number if active and not row["dirty"] and row["review_status"] == "completed" and row["applicability"] == "applicable" else 0
            unknown += number if active and (row["dirty"] or row["review_status"] in {"pending", "unknown", "unavailable"}) else 0
            completed += number if active and not row["dirty"] and row["review_status"] == "completed" else 0
            if row["transition"]:
                transition = min(transition, row["transition"]) if transition else row["transition"]
            statuses[str(row["review_status"])] = statuses.get(str(row["review_status"]), 0) + number
        return {"entry_count": count, "dirty_count": dirty, "review_status_counts": statuses,
                "applicable_count": applicable, "unknown_count": unknown, "completed_count": completed,
                "eligible_count": eligible, "next_transition": transition}
    return store.read_consistent(read)


def apply_changes(store, scope, target_key: str, changes: list[dict]) -> None:
    """Invalidate exact inputs/dependencies; never promote an applicability result."""
    exact, target = _scope(scope), _text(target_key)
    if not isinstance(changes, list) or len(changes) > PAGE_LIMIT:
        raise KnowledgeReviewInventoryError("inventory_changes_invalid")
    for change in changes:
        if not isinstance(change, dict) or _scope(change.get("scope", {})) != exact:
            raise KnowledgeReviewInventoryError("inventory_change_scope_mismatch")
        for key in ("storage_key", "record_id", "kind", "source_id", "category"):
            _text(change.get(key))
    def write(sqlite):
        for change in changes:
            if change["category"] == "revocation":
                sqlite.execute("UPDATE knowledge_review_inventory_manifest SET dirty=1 WHERE " + _WHERE
                    + " AND target_key=?", (*exact, target))
                continue
            sqlite.execute("UPDATE knowledge_review_inventory_manifest SET dirty=1 WHERE " + _WHERE
                + " AND target_key=? AND (storage_key=? OR review_receipt_id=? OR storage_key IN "
                "(SELECT knowledge_storage_key FROM knowledge_review_inventory_dependencies WHERE " + _WHERE
                + " AND target_key=? AND (storage_key=? OR record_id=?)))",
                (*exact, target, change["storage_key"], change["record_id"], *exact, target,
                 change["storage_key"], change["record_id"]))
            if change["kind"] not in _KNOWLEDGE_KINDS:
                continue
            current = sqlite.execute("SELECT " + _ROW_COLUMNS + " FROM records WHERE " + _WHERE
                + " AND storage_key=? AND kind IN ('claim_card','knowledge_page')", (*exact, change["storage_key"])).fetchone()
            if current is None:
                _delete_entry(sqlite, exact, target, change["storage_key"])
            else:
                sqlite.execute("INSERT INTO knowledge_review_inventory_manifest VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'','','',0) "
                    "ON CONFLICT(tenant_id,agent_id,workspace_id,user_id,target_key,storage_key) DO UPDATE SET "
                    "record_id=excluded.record_id,source_id=excluded.source_id,kind=excluded.kind,status=excluded.status,payload_digest=excluded.payload_digest,dirty=1",
                    (*exact, target, current["storage_key"], current["record_id"], current["source_id"], current["kind"], current["status"], current["payload_digest"], "", "", "pending"))
    _mutate(store, write)


def _delete_entry(sqlite, exact, target, key):
    sqlite.execute("DELETE FROM knowledge_review_inventory_manifest WHERE " + _WHERE
        + " AND target_key=? AND storage_key=?", (*exact, target, key))
    sqlite.execute("DELETE FROM knowledge_review_inventory_dependencies WHERE " + _WHERE
        + " AND target_key=? AND knowledge_storage_key=?", (*exact, target, key))


def get_manifest_entry(store, scope, target_key: str, storage_key: str) -> dict | None:
    # Exact lookup uses an indexed equality, not a first-page membership test.
    exact, target, key = _scope(scope), _text(target_key), _text(storage_key)
    ensure(store)
    def read(sqlite):
        row = sqlite.execute("SELECT * FROM knowledge_review_inventory_manifest WHERE " + _WHERE
            + " AND target_key=? AND storage_key=?", (*exact, target, key)).fetchone()
        if row is None:
            return None
        value = _compact(row)
        value["dirty"] = bool(value["dirty"])
        value["dependencies"] = [dict(dep) for dep in sqlite.execute(
            "SELECT storage_key,record_id,source_id,payload_digest FROM knowledge_review_inventory_dependencies WHERE "
            + _WHERE + " AND target_key=? AND knowledge_storage_key=? ORDER BY storage_key,record_id LIMIT 21",
            (*exact, target, key)).fetchall()]
        if len(value["dependencies"]) > 20:
            raise KnowledgeReviewInventoryError("inventory_dependencies_invalid")
        return value
    return store.read_consistent(read)


def add_inventory_entries(store, scope, target_key: str, rows: list[dict]) -> None:
    exact, target = _scope(scope), _text(target_key)
    if not isinstance(rows, list) or len(rows) > PAGE_LIMIT:
        raise KnowledgeReviewInventoryError("inventory_page_limit_invalid")
    for row in rows:
        if not isinstance(row, dict) or _scope(row.get("scope", {})) != exact or row.get("kind") not in _KNOWLEDGE_KINDS:
            raise KnowledgeReviewInventoryError("inventory_change_scope_mismatch")
        for key in ("storage_key", "record_id", "source_id", "kind", "status", "payload_digest"):
            _text(row.get(key), empty=key == "payload_digest")
    def write(sqlite):
        for row in rows:
            # A page may already have been reconciled via a low-key change.
            # Re-read metadata so an old page cannot undo that newer progress.
            current = sqlite.execute("SELECT " + _ROW_COLUMNS + " FROM records WHERE " + _WHERE
                + " AND storage_key=? AND kind IN ('claim_card','knowledge_page')", (*exact, row["storage_key"])).fetchone()
            if current is None:
                _delete_entry(sqlite, exact, target, row["storage_key"])
                continue
            sqlite.execute("INSERT INTO knowledge_review_inventory_manifest VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'','','',0) "
                "ON CONFLICT(tenant_id,agent_id,workspace_id,user_id,target_key,storage_key) DO UPDATE SET "
                "dirty=CASE WHEN record_id!=excluded.record_id OR source_id!=excluded.source_id OR kind!=excluded.kind "
                "OR status!=excluded.status OR payload_digest!=excluded.payload_digest THEN 1 ELSE dirty END,"
                "record_id=excluded.record_id,source_id=excluded.source_id,kind=excluded.kind,status=excluded.status,payload_digest=excluded.payload_digest",
                (*exact, target, current["storage_key"], current["record_id"], current["source_id"], current["kind"],
                 current["status"], current["payload_digest"], "", "", "pending"))
    _mutate(store, write)


def mark_due_entries(store, scope, target_key: str, *, now: str) -> int:
    exact, target, now = _scope(scope), _text(target_key), _timestamp(now)
    def write(sqlite):
        return sqlite.execute("UPDATE knowledge_review_inventory_manifest SET dirty=1,next_transition='' WHERE "
            + _WHERE + " AND target_key=? AND next_transition!='' AND next_transition<=?", (*exact, target, now)).rowcount
    return _mutate(store, write)


def get_unique_applicable(store, scope, target_key: str) -> dict | None:
    exact, target = _scope(scope), _text(target_key)
    ensure(store)
    def read(sqlite):
        unknown = sqlite.execute("SELECT 1 FROM knowledge_review_inventory_manifest WHERE " + _WHERE
            + " AND target_key=? AND status='active' AND (dirty=1 OR review_status IN ('pending','unknown','unavailable')) LIMIT 1", (*exact, target)).fetchone()
        if unknown:
            return None
        rows = sqlite.execute("SELECT * FROM knowledge_review_inventory_manifest WHERE " + _WHERE
            + " AND target_key=? AND status='active' AND dirty=0 AND review_status='completed' AND applicability='applicable' LIMIT 2", (*exact, target)).fetchall()
        return _compact(rows[0]) if len(rows) == 1 else None
    return store.read_consistent(read)


def manifest_reason_counts(store, scope, target_key: str) -> dict[str, int]:
    exact, target = _scope(scope), _text(target_key)
    ensure(store)
    def read(sqlite):
        rows = sqlite.execute("SELECT reason,COUNT(*) AS count FROM knowledge_review_inventory_manifest WHERE "
            + _WHERE + " AND target_key=? AND reason!='' GROUP BY reason ORDER BY reason LIMIT 65", (*exact, target)).fetchall()
        if len(rows) > 64:
            raise KnowledgeReviewInventoryError("inventory_reason_counts_truncated")
        return {str(row["reason"]): int(row["count"]) for row in rows}
    return store.read_consistent(read)


def reviews_for_input(store, scope, input_digest: str, *, limit: int = 3) -> list:
    exact, digest = _scope(scope), _text(input_digest, maximum=64)
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 3:
        raise KnowledgeReviewInventoryError("inventory_review_lookup_invalid")
    ensure(store)
    scope_ref = ScopeRef(**dict(zip(_SCOPE_COLUMNS, exact)))
    def read(sqlite):
        rows = sqlite.execute("SELECT record_id FROM records INDEXED BY knowledge_review_inventory_review_input WHERE "
            + _WHERE + " AND kind='reflection' AND source='" + _REVIEW_SOURCE + "' AND " + _REPORT_EXPR + "='" + _REVIEW_TYPE
            + "' AND " + _DIGEST_EXPR + "=? ORDER BY storage_key LIMIT ?", (*exact, digest, limit + 1)).fetchall()
        if len(rows) > limit:
            raise KnowledgeReviewInventoryError("inventory_review_lookup_truncated")
        records = []
        for row in rows:
            record = sqlite.get_by_id(str(row["record_id"]), scope=scope_ref, exact_scope=True)
            if record is None or record.scope != scope_ref or record.source != _REVIEW_SOURCE or record.content.get("schema") != _REVIEW_TYPE:
                raise KnowledgeReviewInventoryError("inventory_review_payload_invalid")
            if record.content.get("status") in {"completed", "unavailable"}:
                records.append(record)
        return records
    return store.read_consistent(read)


def _completed_review_refs(sqlite, exact, digest: str) -> list[str]:
    # All indexed review refs participate: a legacy archived receipt can lack
    # inline content.status and compact review_status, so a completed-only
    # expression index would silently overlook a real competing completion.
    rows = sqlite.execute("SELECT record_id,source_id,status," + _status_expr() + " AS review_status "
        "FROM records INDEXED BY knowledge_review_inventory_review_input WHERE "
        + _WHERE + " AND kind='reflection' AND source='" + _REVIEW_SOURCE + "' AND " + _REPORT_EXPR + "='" + _REVIEW_TYPE
        + "' AND " + _DIGEST_EXPR + "=? ORDER BY storage_key LIMIT 4", (*exact, digest)).fetchall()
    if len(rows) > 3:
        return ["", ""]  # Unproven cardinality can never clear dirty state.
    scope = ScopeRef(**dict(zip(_SCOPE_COLUMNS, exact)))
    completed = []
    for row in rows:
        if row["status"] != "archived":
            return ["", ""]
        status = str(row["review_status"] or "")
        if status not in {"completed", "unavailable"}:
            try:
                record = sqlite.get_by_id(str(row["record_id"]), scope=scope, exact_scope=True)
            except Exception:
                return ["", ""]
            if (record is None or record.kind != "reflection" or record.status != "archived"
                    or record.scope != scope or record.source != _REVIEW_SOURCE
                    or record.source_id != row["source_id"] or record.content.get("schema") != _REVIEW_TYPE
                    or record.content.get("input_digest") != digest):
                return ["", ""]
            status = str(record.content.get("status") or "")
        if status == "completed":
            completed.append(str(row["record_id"]))
        elif status != "unavailable":
            return ["", ""]
    return completed


def review_is_revoked(store, scope, review_id: str) -> bool:
    exact, review_id = _scope(scope), _text(review_id)
    ensure(store)
    return store.read_consistent(lambda sqlite: sqlite.execute("SELECT 1 FROM records INDEXED BY knowledge_review_inventory_revocation_ref WHERE "
        + _WHERE + " AND kind='reflection' AND source='" + _REVIEW_SOURCE + "' AND " + _REPORT_EXPR + "='" + _REVOCATION_TYPE
        + "' AND " + _REVOCATION_REF_EXPR + "=? LIMIT 1", (*exact, review_id)).fetchone() is not None)
