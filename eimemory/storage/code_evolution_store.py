"""Transaction-scoped repository for the governed code-evolution ledger.

This repository intentionally accepts a ``RuntimeStore`` rather than a path.
That keeps the existing SQLite store as the only authority for transactions,
capabilities, records, and promotion compatibility projections.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import zlib
from threading import RLock
from typing import Any


CODE_EVOLUTION_STORE_SCHEMA = "code_evolution_store.v1"
MAX_SUMMARY_BYTES = 8 * 1024
MAX_ARTIFACT_BYTES = 256 * 1024
MAX_VERIFICATION_LOG_BYTES = 16 * 1024 * 1024
LEASE_SECONDS = 5 * 60


class CodeEvolutionStoreError(RuntimeError):
    """A durable code-evolution ledger operation failed closed."""


class CodeEvolutionConflict(CodeEvolutionStoreError):
    """An idempotency key or compare-and-swap identity conflicted."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_json(raw: str, *, field: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise CodeEvolutionStoreError(f"invalid stored JSON in {field}") from exc


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = {str(key): row[key] for key in row.keys()}
    for key in ("source_evidence_json", "payload_json"):
        if key in result:
            result[key[:-5] if key.endswith("_json") else key] = _parse_json(
                str(result.pop(key) or "{}"), field=key
            )
    return result


def _verification_row_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    payload = _parse_json(str(result.get("payload_json") or "{}"), field="payload_json")
    if not isinstance(payload, dict):
        raise CodeEvolutionStoreError("invalid verification receipt payload")
    if digest_json(payload) != str(result.get("receipt_digest") or ""):
        raise CodeEvolutionStoreError("verification receipt digest mismatch")
    for field, value in payload.items():
        if field not in result or result[field] != value:
            raise CodeEvolutionStoreError("verification receipt row identity mismatch")
    result["payload"] = payload
    return result


def _bool_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    raise CodeEvolutionStoreError("boolean ledger field must be a JSON boolean")


def _text(value: Any, *, field: str, required: bool = False, max_chars: int = 2048) -> str:
    if not isinstance(value, str):
        raise CodeEvolutionStoreError(f"{field} must be a string")
    result = value.strip()
    if required and not result:
        raise CodeEvolutionStoreError(f"{field} must not be empty")
    if len(result) > max_chars:
        raise CodeEvolutionStoreError(f"{field} exceeds its bound")
    return result


def _scope(payload: Mapping[str, Any]) -> tuple[str, str, str, str]:
    value = payload.get("scope", payload)
    if not isinstance(value, Mapping):
        raise CodeEvolutionStoreError("scope must be an object")
    fields = ("tenant_id", "agent_id", "workspace_id", "user_id")
    return tuple(_text(value.get(field), field=f"scope.{field}", required=True, max_chars=256) for field in fields)  # type: ignore[return-value]


def _compress_artifact(data: bytes) -> bytes:
    return zlib.compress(data, level=9)


_STEP_EVENT_DIGEST_FIELDS = (
    "transaction_id",
    "sequence",
    "step",
    "phase",
    "attempt",
    "idempotency_key",
    "from_state",
    "to_state",
    "input_digest",
    "output_digest",
    "artifact_digest",
    "evidence_digest",
    "summary",
    "prior_event_digest",
    "created_at",
)


def _step_event_body(value: Mapping[str, Any]) -> dict[str, Any]:
    return {field: value[field] for field in _STEP_EVENT_DIGEST_FIELDS}


def _validated_step_event_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    prior_digest = ""
    for expected_sequence, row in enumerate(rows, start=1):
        event = dict(row)
        if int(event.get("sequence") or 0) != expected_sequence:
            raise CodeEvolutionStoreError("step event sequence is not contiguous")
        if str(event.get("prior_event_digest") or "") != prior_digest:
            raise CodeEvolutionStoreError("step event prior digest mismatch")
        computed = digest_json(_step_event_body(event))
        if computed != str(event.get("event_digest") or ""):
            raise CodeEvolutionStoreError("step event digest mismatch")
        prior_digest = computed
        result.append(event)
    return result


def _validated_artifact_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    expected_bytes = int(result.get("byte_count") or 0)
    if expected_bytes < 0 or expected_bytes > MAX_VERIFICATION_LOG_BYTES:
        raise CodeEvolutionStoreError("artifact byte count exceeds its evidence bound")
    try:
        decompressor = zlib.decompressobj()
        data = decompressor.decompress(
            bytes(result.get("compressed_bytes") or b""),
            expected_bytes + 1,
        )
    except (TypeError, ValueError, zlib.error) as exc:
        raise CodeEvolutionStoreError("artifact compressed payload is invalid") from exc
    if (
        len(data) != expected_bytes
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise CodeEvolutionStoreError("artifact compressed payload identity mismatch")
    if hashlib.sha256(data).hexdigest() != str(result.get("sha256") or ""):
        raise CodeEvolutionStoreError("artifact digest mismatch")
    result["compressed_bytes"] = bytes(result["compressed_bytes"])
    return result


_TRANSACTION_UPDATE_COLUMNS = frozenset(
    {
        "source_evidence_json",
        "known_before_detection",
        "prior_user_reported",
        "manual_bootstrap",
        "lease_owner",
        "lease_expires_at",
        "advertisement_id",
        "advertisement_digest",
        "catalog_case_id",
        "catalog_snapshot_digest",
        "base_tree_digest",
        "proposal_digest",
        "patch_digest",
        "candidate_tree_digest",
        "policy_digest",
        "authorization_digest",
        "candidate_commit",
        "prior_commit",
        "deployed_commit",
        "observation_started_at",
        "observation_deadline",
        "terminal_receipt_digest",
        "payload_json",
    }
)

# Stable identity accepted by create/replay. State, effect outputs, leases,
# and timestamps may legitimately differ when the same request is retried
# after progress has already been recorded.
_TRANSACTION_IDENTITY_COLUMNS = (
    "transaction_id",
    "schema_version",
    "idempotency_key",
    "tenant_id",
    "agent_id",
    "workspace_id",
    "user_id",
    "incident_id",
    "incident_digest",
    "incident_class",
    "origin",
    "detector",
    "source_evidence_json",
    "known_before_detection",
    "prior_user_reported",
    "manual_bootstrap",
    "capability_id",
    "revision_id",
    "binding_id",
    "provider_kind",
    "provider_instance_id",
    "implementation_digest",
    "advertisement_id",
    "advertisement_digest",
    "catalog_case_id",
    "catalog_snapshot_digest",
    "repository_root",
    "repository_remote",
    "repository_ref",
    "base_commit",
    "base_tree_digest",
    "proposal_digest",
    "patch_digest",
    "candidate_tree_digest",
)


class CodeEvolutionStore:
    """The sole durable repository for normalized code-evolution state."""

    def __init__(self, runtime_store: Any) -> None:
        sqlite = getattr(runtime_store, "sqlite", None)
        lock = getattr(runtime_store, "_lock", None)
        if sqlite is None or getattr(sqlite, "conn", None) is None:
            raise CodeEvolutionStoreError("CodeEvolutionStore requires RuntimeStore.sqlite")
        # ``threading.RLock`` is a factory on some Python versions, so use its
        # small context-manager protocol instead of an ``isinstance`` check.
        if not all(callable(getattr(lock, name, None)) for name in ("__enter__", "__exit__")):
            raise CodeEvolutionStoreError("CodeEvolutionStore requires RuntimeStore lock")
        self.runtime_store = runtime_store
        self.sqlite = sqlite
        self.conn: sqlite3.Connection = sqlite.conn
        self.lock = lock

    def _write(self, callback):
        with self.lock:
            owns_transaction = not self.conn.in_transaction
            if owns_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = callback()
                if owns_transaction:
                    self.conn.commit()
                return result
            except Exception:
                if owns_transaction:
                    self.conn.rollback()
                raise

    def _read(self, callback):
        with self.lock:
            owns_transaction = not self.conn.in_transaction
            if owns_transaction:
                self.conn.execute("BEGIN")
            try:
                result = callback()
            finally:
                if owns_transaction and self.conn.in_transaction:
                    self.conn.rollback()
            return result

    def create_transaction(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Create or replay one transaction identity without backfilling."""

        if not isinstance(payload, Mapping):
            raise CodeEvolutionStoreError("transaction payload must be an object")
        scope = _scope(payload)
        transaction_id = _text(payload.get("transaction_id"), field="transaction_id", required=True, max_chars=256)
        idempotency_key = _text(payload.get("idempotency_key"), field="idempotency_key", required=True, max_chars=512)
        incident = payload.get("incident") if isinstance(payload.get("incident"), Mapping) else payload
        repository = payload.get("repository") if isinstance(payload.get("repository"), Mapping) else payload
        provider = payload.get("provider") if isinstance(payload.get("provider"), Mapping) else payload
        now = _text(payload.get("created_at") or utc_now(), field="created_at", required=True, max_chars=64)
        normalized = dict(payload)
        normalized["schema_version"] = str(payload.get("schema_version") or CODE_EVOLUTION_STORE_SCHEMA)
        normalized["transaction_id"] = transaction_id
        normalized["idempotency_key"] = idempotency_key
        normalized.setdefault("current_state", "DETECTED")
        normalized.setdefault("state_version", 0)
        normalized.setdefault("terminal", False)
        normalized.setdefault("created_at", now)
        normalized.setdefault("updated_at", now)
        normalized.setdefault("scope", {
            "tenant_id": scope[0],
            "agent_id": scope[1],
            "workspace_id": scope[2],
            "user_id": scope[3],
        })
        incident_id = _text(incident.get("incident_id") or incident.get("id"), field="incident_id", required=True)
        repository_root = _text(repository.get("repository_root") or repository.get("root"), field="repository.root", required=True, max_chars=4096)
        repository_remote = _text(repository.get("repository_remote") or repository.get("remote") or "", field="repository.remote", max_chars=4096)
        repository_ref = _text(repository.get("repository_ref") or repository.get("ref"), field="repository.ref", required=True, max_chars=512)
        row_values = {
            "transaction_id": transaction_id,
            "schema_version": _text(normalized["schema_version"], field="schema_version", required=True, max_chars=128),
            "idempotency_key": idempotency_key,
            "tenant_id": scope[0],
            "agent_id": scope[1],
            "workspace_id": scope[2],
            "user_id": scope[3],
            "incident_id": incident_id,
            "incident_digest": _text(incident.get("incident_digest") or incident.get("digest") or digest_json(incident), field="incident_digest", required=True, max_chars=128),
            "incident_class": _text(incident.get("incident_class") or incident.get("class") or "", field="incident_class", max_chars=256),
            "origin": _text(payload.get("origin") or "", field="origin", max_chars=256),
            "detector": _text(payload.get("detector") or "", field="detector", max_chars=256),
            "source_evidence_json": canonical_json(payload.get("source_evidence") or {}),
            "known_before_detection": _bool_int(payload.get("known_before_detection", False)),
            "prior_user_reported": _bool_int(payload.get("prior_user_reported", False)),
            "manual_bootstrap": _bool_int(payload.get("manual_bootstrap", False)),
            "current_state": _text(normalized["current_state"], field="current_state", required=True, max_chars=64),
            "state_version": int(normalized["state_version"]),
            "terminal": _bool_int(normalized["terminal"]),
            "lease_owner": _text(payload.get("lease_owner") or "", field="lease_owner", max_chars=256),
            "lease_expires_at": _text(payload.get("lease_expires_at") or "", field="lease_expires_at", max_chars=64),
            "capability_id": _text(provider.get("capability_id") or payload.get("capability_id") or "", field="capability_id", max_chars=256),
            "revision_id": _text(provider.get("revision_id") or payload.get("revision_id") or "", field="revision_id", max_chars=256),
            "binding_id": _text(provider.get("binding_id") or payload.get("binding_id") or "", field="binding_id", max_chars=256),
            "provider_kind": _text(provider.get("provider_kind") or payload.get("provider_kind") or "", field="provider_kind", max_chars=128),
            "provider_instance_id": _text(provider.get("provider_instance_id") or payload.get("provider_instance_id") or "", field="provider_instance_id", max_chars=256),
            "implementation_digest": _text(provider.get("implementation_digest") or payload.get("implementation_digest") or "", field="implementation_digest", max_chars=128),
            "advertisement_id": _text(provider.get("advertisement_id") or payload.get("advertisement_id") or "", field="advertisement_id", max_chars=256),
            "advertisement_digest": _text(provider.get("advertisement_digest") or payload.get("advertisement_digest") or "", field="advertisement_digest", max_chars=128),
            "catalog_case_id": _text(provider.get("catalog_case_id") or payload.get("catalog_case_id") or "", field="catalog_case_id", max_chars=256),
            "catalog_snapshot_digest": _text(provider.get("catalog_snapshot_digest") or payload.get("catalog_snapshot_digest") or "", field="catalog_snapshot_digest", max_chars=128),
            "repository_root": repository_root,
            "repository_remote": repository_remote,
            "repository_ref": repository_ref,
            "base_commit": _text(repository.get("base_commit") or payload.get("base_commit") or "", field="repository.base_commit", max_chars=128),
            "base_tree_digest": _text(repository.get("base_tree_digest") or payload.get("base_tree_digest") or "", field="base_tree_digest", max_chars=128),
            "proposal_digest": _text(payload.get("proposal_digest") or "", field="proposal_digest", max_chars=128),
            "patch_digest": _text(payload.get("patch_digest") or "", field="patch_digest", max_chars=128),
            "candidate_tree_digest": _text(payload.get("candidate_tree_digest") or "", field="candidate_tree_digest", max_chars=128),
            "policy_digest": _text(payload.get("policy_digest") or "", field="policy_digest", max_chars=128),
            "authorization_digest": _text(payload.get("authorization_digest") or "", field="authorization_digest", max_chars=128),
            "candidate_commit": _text(payload.get("candidate_commit") or "", field="candidate_commit", max_chars=128),
            "prior_commit": _text(payload.get("prior_commit") or "", field="prior_commit", max_chars=128),
            "deployed_commit": _text(payload.get("deployed_commit") or "", field="deployed_commit", max_chars=128),
            "observation_started_at": _text(payload.get("observation_started_at") or "", field="observation_started_at", max_chars=64),
            "observation_deadline": _text(payload.get("observation_deadline") or "", field="observation_deadline", max_chars=64),
            "terminal_receipt_digest": _text(payload.get("terminal_receipt_digest") or "", field="terminal_receipt_digest", max_chars=128),
            "payload_json": canonical_json(normalized),
            "created_at": now,
            "updated_at": _text(payload.get("updated_at") or now, field="updated_at", required=True, max_chars=64),
        }
        columns = tuple(row_values)
        placeholders = ",".join("?" for _ in columns)

        def write() -> dict[str, Any]:
            repository_blocker = self.conn.execute(
                "SELECT t.transaction_id,t.current_state FROM code_evolution_transactions t "
                "WHERE t.repository_root=? AND t.repository_ref=? "
                "AND (t.terminal=0 OR (t.current_state='RECOVERY_QUARANTINED' AND NOT EXISTS ("
                "SELECT 1 FROM code_evolution_step_events e "
                "JOIN code_evolution_artifacts a ON a.transaction_id=e.transaction_id "
                "AND a.artifact_kind='quarantine_resolution_evidence' AND a.sha256=e.artifact_digest "
                "WHERE e.transaction_id=t.transaction_id AND e.step='quarantine_resolution' "
                "AND e.phase='reconcile' AND e.from_state='RECOVERY_QUARANTINED' "
                "AND e.to_state='RECOVERY_QUARANTINED'"
                "))) "
                "ORDER BY created_at LIMIT 1",
                (repository_root, repository_ref),
            ).fetchone()
            if (
                repository_blocker is not None
                and str(repository_blocker["transaction_id"]) != transaction_id
            ):
                reason = (
                    "quarantined"
                    if str(repository_blocker["current_state"]) == "RECOVERY_QUARANTINED"
                    else "unfinished"
                )
                raise CodeEvolutionConflict(
                    f"repository ref is blocked by {reason} transaction"
                )
            try:
                self.conn.execute(
                    f"INSERT INTO code_evolution_transactions ({','.join(columns)}) VALUES ({placeholders})",
                    tuple(row_values[column] for column in columns),
                )
            except sqlite3.IntegrityError as exc:
                existing = self.conn.execute(
                    "SELECT * FROM code_evolution_transactions WHERE transaction_id=? OR idempotency_key=?",
                    (transaction_id, idempotency_key),
                ).fetchone()
                if existing is None:
                    raise CodeEvolutionConflict("transaction identity conflict") from exc
                if any(
                    existing[column] != row_values[column]
                    for column in _TRANSACTION_IDENTITY_COLUMNS
                ):
                    raise CodeEvolutionConflict("transaction identity conflict") from exc
                result = _row_dict(existing) or {}
                result["idempotent"] = True
                return result
            result = _row_dict(
                self.conn.execute(
                    "SELECT * FROM code_evolution_transactions WHERE transaction_id=?",
                    (transaction_id,),
                ).fetchone()
            ) or {}
            result["idempotent"] = False
            return result

        return self._write(write)

    def get_transaction(self, transaction_id: str) -> dict[str, Any] | None:
        transaction_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        return self._read(
            lambda: _row_dict(
                self.conn.execute(
                    "SELECT * FROM code_evolution_transactions WHERE transaction_id=?",
                    (transaction_id,),
                ).fetchone()
            )
        )

    def list_transactions(self, *, current_state: str = "", limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(500, int(limit)))

        def read() -> list[dict[str, Any]]:
            if current_state:
                rows = self.conn.execute(
                    "SELECT * FROM code_evolution_transactions WHERE current_state=? ORDER BY updated_at DESC LIMIT ?",
                    (_text(current_state, field="current_state", max_chars=64), bounded),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM code_evolution_transactions ORDER BY updated_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
            return [_row_dict(row) or {} for row in rows]

        return self._read(read)

    def cas_transition(
        self,
        transaction_id: str,
        *,
        expected_state: str,
        expected_state_version: int,
        target_state: str,
        updates: Mapping[str, Any] | None = None,
        terminal: bool | None = None,
        now: str = "",
    ) -> dict[str, Any]:
        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        expected_state = _text(expected_state, field="expected_state", required=True, max_chars=64)
        target_state = _text(target_state, field="target_state", required=True, max_chars=64)
        update_values = dict(updates or {})
        unknown = set(update_values).difference(_TRANSACTION_UPDATE_COLUMNS)
        if unknown:
            raise CodeEvolutionStoreError(f"unsupported transaction update fields: {sorted(unknown)}")
        timestamp = _text(now or utc_now(), field="now", required=True, max_chars=64)
        assignments = ["current_state=?", "state_version=state_version+1", "updated_at=?"]
        values: list[Any] = [target_state, timestamp]
        for column, value in update_values.items():
            if column.endswith("_json") and not isinstance(value, str):
                value = canonical_json(value)
            if column in {"known_before_detection", "prior_user_reported", "manual_bootstrap"}:
                value = _bool_int(value)
            assignments.append(f"{column}=?")
            values.append(value)
        if terminal is not None:
            assignments.append("terminal=?")
            values.append(_bool_int(terminal))
        values.extend([tx_id, expected_state, int(expected_state_version)])

        def write() -> dict[str, Any]:
            cursor = self.conn.execute(
                f"UPDATE code_evolution_transactions SET {','.join(assignments)} "
                "WHERE transaction_id=? AND current_state=? AND state_version=? AND terminal=0",
                tuple(values),
            )
            if cursor.rowcount != 1:
                current = self.conn.execute(
                    "SELECT current_state,state_version FROM code_evolution_transactions WHERE transaction_id=?",
                    (tx_id,),
                ).fetchone()
                if current is None:
                    raise CodeEvolutionStoreError("transaction not found")
                raise CodeEvolutionConflict(
                    f"transaction CAS failed at {current['current_state']}:{current['state_version']}"
                )
            return _row_dict(
                self.conn.execute(
                    "SELECT * FROM code_evolution_transactions WHERE transaction_id=?",
                    (tx_id,),
                ).fetchone()
            ) or {}

        return self._write(write)

    def acquire_lease(self, transaction_id: str, *, owner: str, now: str = "", seconds: int = LEASE_SECONDS) -> dict[str, Any]:
        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        owner = _text(owner, field="owner", required=True, max_chars=256)
        checked_at = _text(now or utc_now(), field="now", required=True, max_chars=64)
        expires = (datetime.fromisoformat(checked_at.replace("Z", "+00:00")) + timedelta(seconds=max(1, int(seconds)))).isoformat(timespec="seconds")

        def write() -> dict[str, Any]:
            cursor = self.conn.execute(
                "UPDATE code_evolution_transactions SET lease_owner=?,lease_expires_at=?,"
                "state_version=state_version+1,updated_at=? WHERE transaction_id=? AND "
                "terminal=0 AND (lease_owner=? OR lease_expires_at='' OR lease_expires_at<=?)",
                (owner, expires, checked_at, tx_id, owner, checked_at),
            )
            if cursor.rowcount != 1:
                raise CodeEvolutionConflict("transaction lease is held or terminal")
            return _row_dict(self.conn.execute("SELECT * FROM code_evolution_transactions WHERE transaction_id=?", (tx_id,)).fetchone()) or {}

        return self._write(write)

    def renew_lease(self, transaction_id: str, *, owner: str, now: str = "", seconds: int = LEASE_SECONDS) -> dict[str, Any]:
        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        owner = _text(owner, field="owner", required=True, max_chars=256)
        checked_at = _text(now or utc_now(), field="now", required=True, max_chars=64)
        expires = (datetime.fromisoformat(checked_at.replace("Z", "+00:00")) + timedelta(seconds=max(1, int(seconds)))).isoformat(timespec="seconds")

        def write() -> dict[str, Any]:
            cursor = self.conn.execute(
                "UPDATE code_evolution_transactions SET lease_expires_at=?,state_version=state_version+1,updated_at=? "
                "WHERE transaction_id=? AND terminal=0 AND lease_owner=? AND lease_expires_at>?",
                (expires, checked_at, tx_id, owner, checked_at),
            )
            if cursor.rowcount != 1:
                raise CodeEvolutionConflict("transaction lease renewal failed")
            return _row_dict(self.conn.execute("SELECT * FROM code_evolution_transactions WHERE transaction_id=?", (tx_id,)).fetchone()) or {}

        return self._write(write)

    def release_lease(self, transaction_id: str, *, owner: str, now: str = "") -> dict[str, Any]:
        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        owner = _text(owner, field="owner", required=True, max_chars=256)
        checked_at = _text(now or utc_now(), field="now", required=True, max_chars=64)

        def write() -> dict[str, Any]:
            cursor = self.conn.execute(
                "UPDATE code_evolution_transactions SET lease_owner='',lease_expires_at='',"
                "state_version=state_version+1,updated_at=? WHERE transaction_id=? AND lease_owner=? AND terminal=0",
                (checked_at, tx_id, owner),
            )
            if cursor.rowcount != 1:
                raise CodeEvolutionConflict("transaction lease release failed")
            return _row_dict(self.conn.execute("SELECT * FROM code_evolution_transactions WHERE transaction_id=?", (tx_id,)).fetchone()) or {}

        return self._write(write)

    def append_step_event(self, transaction_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        if not isinstance(payload, Mapping):
            raise CodeEvolutionStoreError("step event must be an object")
        step = _text(payload.get("step"), field="step", required=True, max_chars=128)
        phase = _text(payload.get("phase"), field="phase", required=True, max_chars=16)
        if phase not in {"intent", "result", "reconcile"}:
            raise CodeEvolutionStoreError("step event phase is not allowed")
        attempt = int(payload.get("attempt", 1))
        if attempt < 1:
            raise CodeEvolutionStoreError("step event attempt must be positive")
        supplied_created_at = bool(str(payload.get("created_at") or "").strip())
        now = _text(payload.get("created_at") or utc_now(), field="created_at", required=True, max_chars=64)
        summary = _text(payload.get("summary") or "", field="summary", max_chars=MAX_SUMMARY_BYTES)
        normalized = {
            "transaction_id": tx_id,
            "step": step,
            "phase": phase,
            "attempt": attempt,
            "idempotency_key": _text(
                payload.get("idempotency_key") or f"{tx_id}:{step}:{attempt}:{phase}",
                field="idempotency_key",
                required=True,
                max_chars=512,
            ),
            "from_state": _text(payload.get("from_state") or "", field="from_state", max_chars=64),
            "to_state": _text(payload.get("to_state") or "", field="to_state", max_chars=64),
            "input_digest": _text(payload.get("input_digest") or "", field="input_digest", max_chars=128),
            "output_digest": _text(payload.get("output_digest") or "", field="output_digest", max_chars=128),
            "artifact_digest": _text(payload.get("artifact_digest") or "", field="artifact_digest", max_chars=128),
            "evidence_digest": _text(payload.get("evidence_digest") or "", field="evidence_digest", max_chars=128),
            "summary": summary,
        }
        supplied_digest = str(payload.get("event_digest") or "").strip()

        def write() -> dict[str, Any]:
            tx = self.conn.execute("SELECT 1 FROM code_evolution_transactions WHERE transaction_id=?", (tx_id,)).fetchone()
            if tx is None:
                raise CodeEvolutionStoreError("transaction not found")
            existing = self.conn.execute(
                "SELECT * FROM code_evolution_step_events WHERE transaction_id=? AND step=? AND attempt=? AND phase=?",
                (tx_id, step, attempt, phase),
            ).fetchone()
            if existing is not None:
                sequence = int(existing["sequence"])
                created_at = now if supplied_created_at else str(existing["created_at"])
            else:
                sequence_row = self.conn.execute(
                    "SELECT COALESCE(MAX(\"sequence\"),0)+1 AS next_sequence "
                    "FROM code_evolution_step_events WHERE transaction_id=?",
                    (tx_id,),
                ).fetchone()
                sequence = int(sequence_row["next_sequence"])
                created_at = now
            prior_query = (
                "SELECT event_digest FROM code_evolution_step_events "
                "WHERE transaction_id=? AND \"sequence\"<? ORDER BY \"sequence\" DESC LIMIT 1"
            )
            prior_params = (tx_id, sequence)
            prior_row = self.conn.execute(prior_query, prior_params).fetchone()
            prior_digest = str(prior_row["event_digest"] if prior_row is not None else "")
            event_body = {
                **normalized,
                "sequence": sequence,
                "prior_event_digest": prior_digest,
                "created_at": created_at,
            }
            computed_digest = digest_json(event_body)
            if supplied_digest and supplied_digest != computed_digest:
                raise CodeEvolutionConflict("step event digest identity conflict")
            event_digest = computed_digest
            if existing is not None:
                if str(existing["event_digest"]) != event_digest:
                    raise CodeEvolutionConflict("step event identity conflict")
                result = dict(existing)
                result["idempotent"] = True
                return result
            self.conn.execute(
                "INSERT INTO code_evolution_step_events (transaction_id,\"sequence\",step,phase,attempt,idempotency_key,from_state,to_state,input_digest,output_digest,artifact_digest,evidence_digest,summary,prior_event_digest,event_digest,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tx_id,
                    sequence,
                    step,
                    phase,
                    attempt,
                    normalized["idempotency_key"],
                    normalized["from_state"],
                    normalized["to_state"],
                    normalized["input_digest"],
                    normalized["output_digest"],
                    normalized["artifact_digest"],
                    normalized["evidence_digest"],
                    normalized["summary"],
                    prior_digest,
                    event_digest,
                    created_at,
                ),
            )
            result = dict(self.conn.execute("SELECT * FROM code_evolution_step_events WHERE transaction_id=? AND \"sequence\"=?", (tx_id, sequence)).fetchone())
            result["idempotent"] = False
            return result

        return self._write(write)

    def list_step_events(self, transaction_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        bounded = max(1, min(2_000, int(limit)))
        return self._read(
            lambda: _validated_step_event_rows(
                list(self.conn.execute(
                    "SELECT * FROM code_evolution_step_events WHERE transaction_id=? ORDER BY \"sequence\" LIMIT ?",
                    (tx_id, bounded),
                ).fetchall())
            )
        )

    def commit_observation_result(
        self,
        transaction_id: str,
        *,
        owner: str,
        sample_key: str,
        normalized_sample: Mapping[str, Any],
        transaction_payload: Mapping[str, Any],
        observation_started_at: str,
        observation_deadline: str,
        next_action: str = "",
        created_at: str = "",
    ) -> dict[str, Any]:
        """Atomically persist an observation and its required next intent."""

        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        lease_owner = _text(owner, field="owner", required=True, max_chars=256)
        key = _text(sample_key, field="sample_key", required=True, max_chars=256)
        if next_action not in {"", "rollback", "sedimentation"}:
            raise CodeEvolutionStoreError("observation next action is invalid")
        payload_value = dict(transaction_payload)
        samples = payload_value.get("observation_samples")
        if not isinstance(samples, list) or not samples or str((samples[-1] or {}).get("sample_key") or "") != key:
            raise CodeEvolutionStoreError("observation payload sample is invalid")
        failed = payload_value.get("observation_failure") is True
        valid = payload_value.get("observation_valid") is True
        if (next_action == "rollback") != failed:
            raise CodeEvolutionStoreError("observation rollback intent does not match result")
        if next_action == "sedimentation" and (not valid or failed):
            raise CodeEvolutionStoreError("observation sedimentation intent does not match result")
        if not next_action and (failed or valid):
            raise CodeEvolutionStoreError("observation terminal action is missing")
        checked_at = _text(created_at or utc_now(), field="created_at", required=True, max_chars=64)
        started_at = _text(observation_started_at, field="observation_started_at", required=True, max_chars=64)
        deadline = _text(observation_deadline, field="observation_deadline", required=True, max_chars=64)
        sample_digest = digest_json(dict(normalized_sample))
        idempotency_key = f"code-evolution-observation:{tx_id}:{key}"

        def insert_event(event: Mapping[str, Any]) -> dict[str, Any]:
            sequence_row = self.conn.execute(
                'SELECT COALESCE(MAX("sequence"),0)+1 AS next_sequence FROM code_evolution_step_events WHERE transaction_id=?',
                (tx_id,),
            ).fetchone()
            sequence = int(sequence_row["next_sequence"])
            prior_row = self.conn.execute(
                'SELECT event_digest FROM code_evolution_step_events WHERE transaction_id=? ORDER BY "sequence" DESC LIMIT 1',
                (tx_id,),
            ).fetchone()
            prior_digest = str(prior_row["event_digest"] if prior_row is not None else "")
            body = {
                "transaction_id": tx_id,
                "sequence": sequence,
                "step": str(event["step"]),
                "phase": str(event["phase"]),
                "attempt": int(event["attempt"]),
                "idempotency_key": str(event["idempotency_key"]),
                "from_state": str(event["from_state"]),
                "to_state": str(event["to_state"]),
                "input_digest": str(event.get("input_digest") or ""),
                "output_digest": str(event.get("output_digest") or ""),
                "artifact_digest": "",
                "evidence_digest": "",
                "summary": str(event.get("summary") or ""),
                "prior_event_digest": prior_digest,
                "created_at": checked_at,
            }
            event_digest = digest_json(body)
            self.conn.execute(
                'INSERT INTO code_evolution_step_events (transaction_id,"sequence",step,phase,attempt,idempotency_key,from_state,to_state,input_digest,output_digest,artifact_digest,evidence_digest,summary,prior_event_digest,event_digest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                tuple(body[field] for field in _STEP_EVENT_DIGEST_FIELDS[:-1])
                + (event_digest, body["created_at"]),
            )
            return dict(self.conn.execute(
                'SELECT * FROM code_evolution_step_events WHERE transaction_id=? AND "sequence"=?',
                (tx_id, sequence),
            ).fetchone())

        def write() -> dict[str, Any]:
            tx = self.conn.execute(
                "SELECT * FROM code_evolution_transactions WHERE transaction_id=?",
                (tx_id,),
            ).fetchone()
            if tx is None:
                raise CodeEvolutionStoreError("transaction not found")
            if str(tx["current_state"] or "") != "OBSERVING" or str(tx["lease_owner"] or "") != lease_owner:
                raise CodeEvolutionConflict("observation transaction state or lease changed")
            existing = self.conn.execute(
                "SELECT * FROM code_evolution_step_events WHERE transaction_id=? AND idempotency_key=?",
                (tx_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing["input_digest"] or "") != sample_digest:
                    raise CodeEvolutionConflict("observation sample identity conflict")
                return {
                    "transaction": _row_dict(tx) or {},
                    "observation_event": dict(existing),
                    "next_intent": next(
                        (
                            dict(row)
                            for row in self.conn.execute(
                                "SELECT * FROM code_evolution_step_events WHERE transaction_id=? AND phase='intent' ORDER BY \"sequence\" DESC",
                                (tx_id,),
                            ).fetchall()
                            if str(row["step"] or "") == next_action
                        ),
                        None,
                    ),
                    "idempotent": True,
                }
            attempt_row = self.conn.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 AS next_attempt FROM code_evolution_step_events WHERE transaction_id=? AND step='observation' AND phase='result'",
                (tx_id,),
            ).fetchone()
            observation_event = insert_event(
                {
                    "step": "observation",
                    "phase": "result",
                    "attempt": int(attempt_row["next_attempt"]),
                    "idempotency_key": idempotency_key,
                    "from_state": "OBSERVING",
                    "to_state": "OBSERVING",
                    "input_digest": sample_digest,
                    "output_digest": digest_json({"sample_key": key, "health_ok": normalized_sample.get("health_ok") is True}),
                    "summary": f"observation:{key[:16]}",
                }
            )
            samples[-1]["event_sequence"] = int(observation_event["sequence"])
            payload_value["observation_samples"] = samples
            if payload_value.get("observation_valid") is True:
                payload_value["observation_digest"] = digest_json(samples)
            next_intent = None
            target_state = "ROLLBACK_INTENT" if next_action == "rollback" else "OBSERVING"
            if next_action:
                next_intent = insert_event(
                    {
                        "step": next_action,
                        "phase": "intent",
                        "attempt": 1,
                        "idempotency_key": f"code-evolution-{next_action}:{tx_id}",
                        "from_state": "OBSERVING",
                        "to_state": target_state,
                        "input_digest": digest_json(
                            {
                                "transaction_id": tx_id,
                                "sample_key": key,
                                "observation_digest": str(payload_value.get("observation_digest") or ""),
                            }
                        ),
                        "summary": f"intent:{next_action}",
                    }
                )
            cursor = self.conn.execute(
                "UPDATE code_evolution_transactions SET current_state=?,payload_json=?,observation_started_at=?,observation_deadline=?,state_version=state_version+1,updated_at=? "
                "WHERE transaction_id=? AND current_state='OBSERVING' AND lease_owner=? AND state_version=? AND terminal=0",
                (
                    target_state,
                    canonical_json(payload_value),
                    started_at,
                    deadline,
                    checked_at,
                    tx_id,
                    lease_owner,
                    int(tx["state_version"]),
                ),
            )
            if cursor.rowcount != 1:
                raise CodeEvolutionConflict("observation result transaction CAS failed")
            current = self.conn.execute(
                "SELECT * FROM code_evolution_transactions WHERE transaction_id=?",
                (tx_id,),
            ).fetchone()
            return {
                "transaction": _row_dict(current) or {},
                "observation_event": observation_event,
                "next_intent": next_intent,
                "idempotent": False,
            }

        return self._write(write)

    def store_artifact(
        self,
        transaction_id: str,
        *,
        artifact_kind: str,
        artifact_schema: str,
        data: bytes,
        max_bytes: int = MAX_ARTIFACT_BYTES,
        created_at: str = "",
    ) -> dict[str, Any]:
        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        kind = _text(artifact_kind, field="artifact_kind", required=True, max_chars=128)
        schema = _text(artifact_schema, field="artifact_schema", required=True, max_chars=128)
        if not isinstance(data, bytes):
            raise CodeEvolutionStoreError("artifact data must be bytes")
        evidence_bound = max(1, min(MAX_VERIFICATION_LOG_BYTES, int(max_bytes)))
        if len(data) > evidence_bound:
            raise CodeEvolutionStoreError("artifact exceeds its evidence bound")
        digest = hashlib.sha256(data).hexdigest()
        compressed = _compress_artifact(data)
        timestamp = _text(created_at or utc_now(), field="created_at", required=True, max_chars=64)

        def write() -> dict[str, Any]:
            if self.conn.execute("SELECT 1 FROM code_evolution_transactions WHERE transaction_id=?", (tx_id,)).fetchone() is None:
                raise CodeEvolutionStoreError("transaction not found")
            existing = self.conn.execute(
                "SELECT * FROM code_evolution_artifacts WHERE transaction_id=? AND artifact_kind=?",
                (tx_id, kind),
            ).fetchone()
            if existing is not None:
                if str(existing["sha256"]) != digest:
                    raise CodeEvolutionConflict("artifact identity conflict")
                result = _validated_artifact_row(existing)
                result["idempotent"] = True
                return result
            self.conn.execute(
                "INSERT INTO code_evolution_artifacts(transaction_id,artifact_kind,artifact_schema,byte_count,sha256,compressed_bytes,created_at) VALUES(?,?,?,?,?,?,?)",
                (tx_id, kind, schema, len(data), digest, compressed, timestamp),
            )
            result = _validated_artifact_row(self.conn.execute("SELECT * FROM code_evolution_artifacts WHERE transaction_id=? AND artifact_kind=?", (tx_id, kind)).fetchone())
            result["idempotent"] = False
            return result

        return self._write(write)

    def get_artifact(self, transaction_id: str, artifact_kind: str) -> dict[str, Any] | None:
        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        kind = _text(artifact_kind, field="artifact_kind", required=True, max_chars=128)
        return self._read(
            lambda: (
                _validated_artifact_row(row)
                if (row := self.conn.execute("SELECT * FROM code_evolution_artifacts WHERE transaction_id=? AND artifact_kind=?", (tx_id, kind)).fetchone()) is not None
                else None
            )
        )

    def add_verification_receipt(self, transaction_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        if not isinstance(receipt, Mapping):
            raise CodeEvolutionStoreError("verification receipt must be an object")
        kind = _text(receipt.get("verification_kind") or receipt.get("kind"), field="verification_kind", required=True, max_chars=32)
        if kind not in {"focused", "regression", "full_suite"}:
            raise CodeEvolutionStoreError("verification kind is not protected")
        supplied_digest = str(receipt.get("receipt_digest") or "").strip().lower()
        values: dict[str, Any] = {
            "transaction_id": tx_id,
            "verification_kind": kind,
            "base_commit": _text(receipt.get("base_commit") or "", field="base_commit", max_chars=128),
            "patch_digest": _text(receipt.get("patch_digest") or "", field="patch_digest", max_chars=128),
            "candidate_tree_digest": _text(receipt.get("candidate_tree_digest") or "", field="candidate_tree_digest", max_chars=128),
            "test_plan_id": _text(receipt.get("test_plan_id") or "", field="test_plan_id", max_chars=256),
            "test_plan_digest": _text(receipt.get("test_plan_digest") or "", field="test_plan_digest", max_chars=128),
            "command_digest": _text(receipt.get("command_digest") or "", field="command_digest", max_chars=128),
            "environment_digest": _text(receipt.get("environment_digest") or "", field="environment_digest", max_chars=128),
            "verifier_id": _text(receipt.get("verifier_id") or "", field="verifier_id", max_chars=256),
            "verifier_revision": _text(receipt.get("verifier_revision") or "", field="verifier_revision", max_chars=128),
            "started_at": _text(receipt.get("started_at") or "", field="started_at", max_chars=64),
            "finished_at": _text(receipt.get("finished_at") or "", field="finished_at", max_chars=64),
            "exit_status": int(receipt.get("exit_status", 1)),
            "test_count": int(receipt.get("test_count", 0)),
            "passed_count": int(receipt.get("passed_count", 0)),
            "failed_count": int(receipt.get("failed_count", 0)),
            "skipped_count": int(receipt.get("skipped_count", 0)),
            "result": _text(receipt.get("result") or "", field="result", required=True, max_chars=32),
            "log_artifact_digest": _text(receipt.get("log_artifact_digest") or "", field="log_artifact_digest", max_chars=128),
            "created_at": _text(receipt.get("created_at") or utc_now(), field="created_at", required=True, max_chars=64),
        }
        receipt_body = dict(values)
        digest = digest_json(receipt_body)
        if supplied_digest and supplied_digest != digest:
            raise CodeEvolutionConflict("verification receipt digest mismatch")
        values["receipt_digest"] = digest
        values["payload_json"] = canonical_json(receipt_body)
        columns = tuple(values)

        def write() -> dict[str, Any]:
            if self.conn.execute("SELECT 1 FROM code_evolution_transactions WHERE transaction_id=?", (tx_id,)).fetchone() is None:
                raise CodeEvolutionStoreError("transaction not found")
            existing = self.conn.execute("SELECT * FROM code_evolution_verification_receipts WHERE transaction_id=? AND verification_kind=?", (tx_id, kind)).fetchone()
            if existing is not None:
                if str(existing["receipt_digest"]) != digest:
                    raise CodeEvolutionConflict("verification receipt identity conflict")
                result = dict(existing)
                result["idempotent"] = True
                return result
            placeholders = ",".join("?" for _ in columns)
            self.conn.execute(
                f"INSERT INTO code_evolution_verification_receipts({','.join(columns)}) VALUES({placeholders})",
                tuple(values[column] for column in columns),
            )
            result = dict(self.conn.execute("SELECT * FROM code_evolution_verification_receipts WHERE transaction_id=? AND verification_kind=?", (tx_id, kind)).fetchone())
            result["idempotent"] = False
            return result

        return self._write(write)

    def list_verification_receipts(self, transaction_id: str) -> list[dict[str, Any]]:
        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        return self._read(
            lambda: [
                _verification_row_dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM code_evolution_verification_receipts WHERE transaction_id=? ORDER BY verification_kind",
                    (tx_id,),
                ).fetchall()
            ]
        )

    def consume_policy(
        self,
        *,
        transaction_id: str,
        policy_digest: str,
        authorization_receipt_digest: str,
        payload: Mapping[str, Any] | None = None,
        consumed_at: str = "",
    ) -> dict[str, Any]:
        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        policy_digest = _text(policy_digest, field="policy_digest", required=True, max_chars=128)
        auth_digest = _text(authorization_receipt_digest, field="authorization_receipt_digest", required=True, max_chars=128)
        timestamp = _text(consumed_at or utc_now(), field="consumed_at", required=True, max_chars=64)
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in (policy_digest, auth_digest)
        ):
            raise CodeEvolutionStoreError("policy consumption digest is invalid")
        payload_value = dict(payload or {})
        authorization_material = payload_value.get("authorization_material")
        authorized_policy = payload_value.get("authorized_policy")
        if not isinstance(authorization_material, Mapping):
            raise CodeEvolutionStoreError("policy authorization material is required")
        if (
            not isinstance(authorized_policy, Mapping)
            or str(authorized_policy.get("policy_digest") or "") != policy_digest
            or authorized_policy.get("ok") is not True
        ):
            raise CodeEvolutionStoreError("authorized policy snapshot is required")
        if (
            str(authorization_material.get("transaction_id") or "") != tx_id
            or str(authorization_material.get("policy_digest") or "") != policy_digest
            or digest_json(authorization_material) != auth_digest
        ):
            raise CodeEvolutionStoreError("policy authorization material digest mismatch")
        payload_json = canonical_json(payload_value)

        def write() -> dict[str, Any]:
            tx = self.conn.execute("SELECT * FROM code_evolution_transactions WHERE transaction_id=?", (tx_id,)).fetchone()
            if tx is None:
                raise CodeEvolutionStoreError("transaction not found")
            transaction_payload = json.loads(str(tx["payload_json"] or "{}"))
            if not isinstance(transaction_payload, dict):
                raise CodeEvolutionStoreError("transaction payload is invalid")
            transaction_payload["authorized_policy"] = dict(authorized_policy)
            transaction_payload_json = canonical_json(transaction_payload)
            existing = self.conn.execute("SELECT * FROM code_evolution_policy_consumptions WHERE policy_digest=?", (policy_digest,)).fetchone()
            if existing is not None:
                if str(existing["transaction_id"]) != tx_id or str(existing["authorization_receipt_digest"]) != auth_digest:
                    raise CodeEvolutionConflict("policy digest was already consumed")
                if str(existing["payload_json"]) != payload_json:
                    raise CodeEvolutionConflict("policy consumption payload identity conflict")
                idempotent = True
            else:
                self.conn.execute(
                    "INSERT INTO code_evolution_policy_consumptions(policy_digest,transaction_id,authorization_receipt_digest,consumed_at,payload_json) VALUES(?,?,?,?,?)",
                    (policy_digest, tx_id, auth_digest, timestamp, payload_json),
                )
                idempotent = False
            if not str(tx["policy_digest"] or ""):
                cursor = self.conn.execute(
                    "UPDATE code_evolution_transactions SET policy_digest=?,authorization_digest=?,payload_json=?,"
                    "state_version=state_version+1,updated_at=? WHERE transaction_id=? "
                    "AND terminal=0 AND state_version=? AND policy_digest='' AND authorization_digest=''",
                    (policy_digest, auth_digest, transaction_payload_json, timestamp, tx_id, int(tx["state_version"])),
                )
                if cursor.rowcount != 1:
                    raise CodeEvolutionConflict("policy consumption transaction CAS failed")
            elif (
                str(tx["policy_digest"] or "") != policy_digest
                or str(tx["authorization_digest"] or "") != auth_digest
            ):
                raise CodeEvolutionConflict("transaction authorization identity conflict")
            elif str(tx["payload_json"] or "") != transaction_payload_json:
                cursor = self.conn.execute(
                    "UPDATE code_evolution_transactions SET payload_json=?,state_version=state_version+1,updated_at=? "
                    "WHERE transaction_id=? AND terminal=0 AND state_version=?",
                    (transaction_payload_json, timestamp, tx_id, int(tx["state_version"])),
                )
                if cursor.rowcount != 1:
                    raise CodeEvolutionConflict("policy snapshot transaction CAS failed")
            result = dict(self.conn.execute("SELECT * FROM code_evolution_policy_consumptions WHERE policy_digest=?", (policy_digest,)).fetchone())
            result["idempotent"] = idempotent
            return result

        return self._write(write)

    def get_policy_consumption(self, policy_digest: str) -> dict[str, Any] | None:
        digest = _text(
            policy_digest,
            field="policy_digest",
            required=True,
            max_chars=128,
        )
        return self._read(
            lambda: (
                dict(row)
                if (
                    row := self.conn.execute(
                        "SELECT * FROM code_evolution_policy_consumptions WHERE policy_digest=?",
                        (digest,),
                    ).fetchone()
                )
                is not None
                else None
            )
        )

    def add_terminal_receipt(
        self,
        transaction_id: str,
        receipt: Mapping[str, Any],
        *,
        terminal_state: str,
        expected_state: str | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        if not isinstance(receipt, Mapping):
            raise CodeEvolutionStoreError("terminal receipt must be an object")
        outcome = _text(receipt.get("outcome"), field="outcome", required=True, max_chars=64)
        supplied_digest = str(receipt.get("receipt_digest") or "").strip().lower()
        values: dict[str, Any] = {
            "transaction_id": tx_id,
            "outcome": outcome,
            "incident_digest": _text(receipt.get("incident_digest") or "", field="incident_digest", max_chars=128),
            "provider_digest": _text(receipt.get("provider_digest") or "", field="provider_digest", max_chars=128),
            "policy_digest": _text(receipt.get("policy_digest") or "", field="policy_digest", max_chars=128),
            "authorization_digest": _text(receipt.get("authorization_digest") or "", field="authorization_digest", max_chars=128),
            "base_commit": _text(receipt.get("base_commit") or "", field="base_commit", max_chars=128),
            "candidate_commit": _text(receipt.get("candidate_commit") or "", field="candidate_commit", max_chars=128),
            "deployed_commit": _text(receipt.get("deployed_commit") or "", field="deployed_commit", max_chars=128),
            "observation_digest": _text(receipt.get("observation_digest") or "", field="observation_digest", max_chars=128),
            "rollback_digest": _text(receipt.get("rollback_digest") or "", field="rollback_digest", max_chars=128),
            "evidence_digest": _text(receipt.get("evidence_digest") or "", field="evidence_digest", required=True, max_chars=128),
            "created_at": _text(receipt.get("created_at") or utc_now(), field="created_at", required=True, max_chars=64),
        }
        body = {
            **values,
            **{
                key: value
                for key, value in receipt.items()
                if key not in {"receipt_digest", "transaction_id", "created_at"}
            },
        }
        digest = digest_json(body)
        if supplied_digest and supplied_digest != digest:
            raise CodeEvolutionConflict("terminal receipt digest mismatch")
        values["receipt_digest"] = digest
        values["payload_json"] = canonical_json(body)
        columns = tuple(values)

        def write() -> dict[str, Any]:
            tx = self.conn.execute("SELECT * FROM code_evolution_transactions WHERE transaction_id=?", (tx_id,)).fetchone()
            if tx is None:
                raise CodeEvolutionStoreError("transaction not found")
            existing = self.conn.execute("SELECT * FROM code_evolution_terminal_receipts WHERE transaction_id=?", (tx_id,)).fetchone()
            if existing is not None:
                if str(existing["receipt_digest"]) != digest:
                    raise CodeEvolutionConflict("terminal receipt identity conflict")
                result = dict(existing)
                result["idempotent"] = True
                return result
            if expected_state is not None and str(tx["current_state"]) != expected_state:
                raise CodeEvolutionConflict("terminal receipt state conflict")
            if expected_state_version is not None and int(tx["state_version"]) != int(expected_state_version):
                raise CodeEvolutionConflict("terminal receipt version conflict")
            placeholders = ",".join("?" for _ in columns)
            self.conn.execute(
                f"INSERT INTO code_evolution_terminal_receipts({','.join(columns)}) VALUES({placeholders})",
                tuple(values[column] for column in columns),
            )
            now = values["created_at"]
            cursor = self.conn.execute(
                "UPDATE code_evolution_transactions SET current_state=?,terminal=1,"
                "terminal_receipt_digest=?,state_version=state_version+1,updated_at=? "
                "WHERE transaction_id=? AND terminal=0 AND current_state=? AND state_version=?",
                (
                    terminal_state,
                    digest,
                    now,
                    tx_id,
                    str(tx["current_state"]),
                    int(tx["state_version"]),
                ),
            )
            if cursor.rowcount != 1:
                raise CodeEvolutionConflict("terminal receipt transaction CAS failed")
            result = dict(self.conn.execute("SELECT * FROM code_evolution_terminal_receipts WHERE transaction_id=?", (tx_id,)).fetchone())
            result["idempotent"] = False
            return result

        return self._write(write)

    def get_terminal_receipt(self, transaction_id: str) -> dict[str, Any] | None:
        tx_id = _text(transaction_id, field="transaction_id", required=True, max_chars=256)
        def read() -> dict[str, Any] | None:
            row = self.conn.execute(
                "SELECT * FROM code_evolution_terminal_receipts WHERE transaction_id=?",
                (tx_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            payload = _parse_json(str(result.get("payload_json") or "{}"), field="payload_json")
            if not isinstance(payload, dict):
                raise CodeEvolutionStoreError("invalid terminal receipt payload")
            if digest_json(payload) != str(result.get("receipt_digest") or ""):
                raise CodeEvolutionStoreError("terminal receipt digest mismatch")
            for field in (
                "transaction_id",
                "outcome",
                "incident_digest",
                "provider_digest",
                "policy_digest",
                "authorization_digest",
                "base_commit",
                "candidate_commit",
                "deployed_commit",
                "observation_digest",
                "rollback_digest",
                "evidence_digest",
                "created_at",
            ):
                if payload.get(field) != result.get(field):
                    raise CodeEvolutionStoreError("terminal receipt row identity mismatch")
            result["payload"] = payload
            return result

        return self._read(read)


__all__ = [
    "CODE_EVOLUTION_STORE_SCHEMA",
    "CodeEvolutionConflict",
    "CodeEvolutionStore",
    "CodeEvolutionStoreError",
    "LEASE_SECONDS",
    "MAX_ARTIFACT_BYTES",
    "MAX_VERIFICATION_LOG_BYTES",
    "canonical_json",
    "digest_json",
    "utc_now",
]
