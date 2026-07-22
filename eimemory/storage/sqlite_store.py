from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any
from hashlib import sha256
from eimemory.recall import analyze_lexical_signal, build_recall_index_document

from eimemory.embeddings.local import cosine_similarity, embed_text
from eimemory.events import (
    DEFAULT_INTENT_PATTERNS,
    ensure_event_payload,
    ensure_outcome_payload,
    ensure_pattern_payload,
    event_similarity,
    normalize_scope,
    pattern_matches,
)
from eimemory.identity import hongtu_query_scopes
from eimemory.models.memory_edges import MEMORY_EDGE_TYPES, MemoryEdge
from eimemory.models.identity_aliases import (
    IDENTITY_ALIASES_VERSION,
    normalize_identity_text,
    normalize_record_aliases,
)
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.models.source_partitions import DEFAULT_SOURCE_ID, normalize_source_id, normalize_source_ids
from eimemory.governance.tool_receipts import MAX_ELIGIBLE_RECEIPTS_PER_RUN
from eimemory.governance.policy_rollout import (
    AUTO_PROMOTION_BUDGET_PER_DAY,
    AUTO_ROLLBACK_BUDGET_PER_DAY,
    budget_decision_for_promotion,
    budget_decision_for_rollback,
    build_rollout_ledger_record,
    follow_up_opportunities_from_rollback,
    should_auto_rollback_from_repeated_bad_outcomes,
    now_utc,
    next_rollout_id,
    outcome_triggers_immediate_rollback,
    extract_pattern_ids_from_outcome,
)
from eimemory.scoring import ScoreContext, evaluate_recall_score, extract_memory_score, score_from_legacy_quality
from eimemory.metadata import business_metadata
from eimemory.storage.jsonl import canonical_payload_json, payload_digest


MAX_QUERY_LIMIT = 1000
_MAX_LEXICAL_ADJUSTMENT = 0.18
_DEFAULT_CANDIDATE_LIMIT = 360
_MAX_CANDIDATE_LIMIT = 1200
_RECORD_META_KEYS_MIGRATION = "records.meta_keys.v1"
_INTENT_PATTERN_STATUS_MIGRATION = "intent_patterns.payload_status.v1"
_STORAGE_SCHEMA_MIGRATION = "storage.schema.v1"
_SOURCE_PARTITION_MIGRATION = "records.source_partition.v1"
_RECALL_IDENTITY_MIGRATION = "recall.identity_index.v1"
_RECALL_LANE_MEMORY_TYPE_ALIASES = {
    "audit": "audit_record",
    "audit_record": "audit_record",
    "diagnostic": "audit_record",
    "incident": "incident_report",
    "incident_report": "incident_report",
    "log": "run_log",
    "run_log": "run_log",
    "runtime_log": "run_log",
    "evolution": "evolution_artifact",
    "evolution_artifact": "evolution_artifact",
    "preference": "user_preference",
    "user_preference": "user_preference",
    "rule": "system_rule",
    "system_rule": "system_rule",
    "fact": "durable_fact",
    "durable_fact": "durable_fact",
    "knowledge": "external_knowledge",
    "external_knowledge": "external_knowledge",
    "conversation": "task_context",
    "context": "task_context",
    "task_context": "task_context",
}


class SqliteRecordStore:
    def __init__(self, path: Path, *, auxiliary_log_dir: Path | None = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.auxiliary_log_dir = Path(auxiliary_log_dir) if auxiliary_log_dir is not None else None
        self.suppress_auxiliary_logging = False
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self._configure_connection()
        self._init_db()
        self._create_adapter_receipt_tables()
        self._create_proactive_recall_tables()
        self.conn.commit()
        self.preload_report = self.preload_hot_pages()

    def _configure_connection(self) -> None:
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=FILE")
        self.conn.execute("PRAGMA wal_autocheckpoint=1000")
        self.conn.execute(f"PRAGMA journal_size_limit={64 * 1024 * 1024}")
        self.conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        try:
            cache_kib = int(os.environ.get("EIMEMORY_SQLITE_CACHE_KIB") or 16_384)
        except ValueError:
            cache_kib = 16_384
        cache_kib = max(4_096, min(65_536, cache_kib))
        self.conn.execute(f"PRAGMA cache_size=-{cache_kib}")

    def _init_db(self) -> None:
        existing = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='records'"
        ).fetchone()
        if not existing:
            self._create_records_table()
            self._create_schema_migrations_table()
            self._mark_schema_migration(_RECORD_META_KEYS_MIGRATION)
            self._create_indexes()
            self._create_recall_index_tables()
            self._create_recall_identity_tables()
            self._create_memory_edge_tables()
            self._create_event_memory_tables()
            self._create_policy_rollout_tables()
            self._create_export_outbox_table()
            self._create_replay_manifest_sequence_table()
            self._seed_default_intent_patterns()
            self._mark_schema_migration(_INTENT_PATTERN_STATUS_MIGRATION)
            self._mark_schema_migration(_STORAGE_SCHEMA_MIGRATION)
            self._mark_schema_migration(_SOURCE_PARTITION_MIGRATION)
            self._mark_schema_migration(_RECALL_IDENTITY_MIGRATION)
            self.conn.commit()
            return
        migrations_ready = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        schema_ready = bool(
            migrations_ready
            and self._schema_migration_applied(_STORAGE_SCHEMA_MIGRATION)
            and self._schema_migration_applied(_RECORD_META_KEYS_MIGRATION)
            and self._schema_migration_applied(_INTENT_PATTERN_STATUS_MIGRATION)
            and self._schema_migration_applied(_SOURCE_PARTITION_MIGRATION)
            and self._source_partition_physical_ready()
            and self._schema_migration_applied(_RECALL_IDENTITY_MIGRATION)
            and self._recall_identity_physical_ready()
        )
        if schema_ready:
            self._seed_default_intent_patterns()
            if str(os.environ.get("EIMEMORY_RECALL_INDEX_BACKFILL_ON_START") or "").strip() == "1":
                self._backfill_recall_index_if_needed()
            self.conn.commit()
            return
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(records)").fetchall()
        }
        if "storage_key" not in columns:
            self._migrate_to_scoped_storage_key(columns)
            columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(records)").fetchall()
            }
        if "embedding_json" not in columns:
            self.conn.execute("ALTER TABLE records ADD COLUMN embedding_json TEXT NOT NULL DEFAULT '[]'")
        if "idempotency_key" not in columns:
            self.conn.execute("ALTER TABLE records ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''")
        if "semantic_key" not in columns:
            self.conn.execute("ALTER TABLE records ADD COLUMN semantic_key TEXT NOT NULL DEFAULT ''")
        self._create_schema_migrations_table()
        self._migrate_source_partition_schema()
        self._migrate_recall_identity_schema()
        self._create_indexes()
        self.conn.commit()
        self._backfill_record_meta_keys_if_needed()
        self._create_recall_index_tables()
        self._create_memory_edge_tables()
        self._create_event_memory_tables()
        self.conn.commit()
        self._migrate_intent_patterns_schema()
        self._create_policy_rollout_tables()
        self._create_export_outbox_table()
        self._create_replay_manifest_sequence_table()
        self._seed_default_intent_patterns()
        self._mark_schema_migration(_STORAGE_SCHEMA_MIGRATION)
        if str(os.environ.get("EIMEMORY_RECALL_INDEX_BACKFILL_ON_START") or "").strip() == "1":
            self._backfill_recall_index_if_needed()
        self.conn.commit()

    def _create_records_table(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                storage_key TEXT PRIMARY KEY,
                record_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                detail TEXT NOT NULL,
                content_text TEXT NOT NULL,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT 'default',
                agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                embedding_json TEXT NOT NULL DEFAULT '[]',
                idempotency_key TEXT NOT NULL DEFAULT '',
                semantic_key TEXT NOT NULL DEFAULT '',
                meta_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._create_recall_index_tables()

    def _create_adapter_receipt_tables(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS adapter_tool_receipts (
                receipt_id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                source TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                eligible INTEGER NOT NULL,
                consumed_trace_id TEXT NOT NULL DEFAULT '',
                receipt_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(channel, tenant_id, agent_id, workspace_id, user_id, session_id, run_id, tool_call_id)
            )
            """
        )
        consumed_indexes = [
            row
            for row in self.conn.execute("PRAGMA index_list(adapter_tool_receipts)").fetchall()
            if str(row["name"]) == "idx_adapter_receipts_consumed_trace"
        ]
        if consumed_indexes and int(consumed_indexes[0]["unique"]) == 1:
            self.conn.execute("DROP INDEX idx_adapter_receipts_consumed_trace")
            consumed_indexes = []
        if not consumed_indexes:
            self.conn.execute(
                "CREATE INDEX idx_adapter_receipts_consumed_trace "
                "ON adapter_tool_receipts(consumed_trace_id) WHERE consumed_trace_id != ''"
            )

    def _create_proactive_recall_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS proactive_turns (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                source_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                entities_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(channel, tenant_id, agent_id, workspace_id, user_id, source_key, session_id, turn_id)
            );
            CREATE INDEX IF NOT EXISTS idx_proactive_turns_session
              ON proactive_turns(channel, tenant_id, agent_id, workspace_id, user_id, source_key, session_id, entry_id DESC);

            CREATE TABLE IF NOT EXISTS proactive_decisions (
                decision_id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                source_key TEXT NOT NULL,
                source_ids_json TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                query_id TEXT NOT NULL,
                query_digest TEXT NOT NULL,
                query_text TEXT NOT NULL,
                task_type TEXT NOT NULL DEFAULT '',
                effective_query_digest TEXT NOT NULL DEFAULT '',
                policy_version TEXT NOT NULL,
                release_commit TEXT NOT NULL,
                release_version TEXT NOT NULL,
                deployment_receipt_id TEXT NOT NULL,
                release_session_id TEXT NOT NULL,
                release_bound INTEGER NOT NULL,
                control_cohort INTEGER NOT NULL,
                pair_id TEXT NOT NULL,
                context_text TEXT NOT NULL DEFAULT '',
                terminal INTEGER NOT NULL DEFAULT 0,
                outcome_success INTEGER,
                outcome_verified INTEGER NOT NULL DEFAULT 0,
                outcome_quality REAL,
                outcome_latency_ms REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_proactive_decisions_exact_turn
              ON proactive_decisions(channel, tenant_id, agent_id, workspace_id, user_id, source_key,
                                     session_id, turn_id, release_commit, decision_id DESC);
            CREATE INDEX IF NOT EXISTS idx_proactive_decisions_pair
              ON proactive_decisions(pair_id, control_cohort, decision_id);

            CREATE TABLE IF NOT EXISTS proactive_decision_items (
                decision_id TEXT NOT NULL,
                citation TEXT NOT NULL,
                record_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                confidence REAL NOT NULL,
                state TEXT NOT NULL,
                ever_injected INTEGER NOT NULL DEFAULT 0,
                mandatory INTEGER NOT NULL DEFAULT 0,
                item_order INTEGER NOT NULL DEFAULT 0,
                title_text TEXT NOT NULL DEFAULT '',
                content_text TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(decision_id, citation),
                FOREIGN KEY(decision_id) REFERENCES proactive_decisions(decision_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_proactive_items_record
              ON proactive_decision_items(record_id, source_id, state, decision_id);

            CREATE TABLE IF NOT EXISTS proactive_bypass_diagnostics (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                session_digest TEXT NOT NULL,
                query_digest TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        decision_columns = {
            str(row["name"]) for row in self.conn.execute("PRAGMA table_info(proactive_decisions)")
        }
        for name, definition in (
            ("context_text", "TEXT NOT NULL DEFAULT ''"),
            ("task_type", "TEXT NOT NULL DEFAULT ''"),
            ("effective_query_digest", "TEXT NOT NULL DEFAULT ''"),
            ("outcome_success", "INTEGER"),
            ("outcome_verified", "INTEGER NOT NULL DEFAULT 0"),
            ("outcome_quality", "REAL"),
            ("outcome_latency_ms", "REAL"),
        ):
            if name not in decision_columns:
                self.conn.execute(f"ALTER TABLE proactive_decisions ADD COLUMN {name} {definition}")
        item_columns = {
            str(row["name"]) for row in self.conn.execute("PRAGMA table_info(proactive_decision_items)")
        }
        for name, definition in (
            ("ever_injected", "INTEGER NOT NULL DEFAULT 0"),
            ("item_order", "INTEGER NOT NULL DEFAULT 0"),
            ("title_text", "TEXT NOT NULL DEFAULT ''"),
            ("content_text", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in item_columns:
                self.conn.execute(
                    f"ALTER TABLE proactive_decision_items ADD COLUMN {name} {definition}"
                )

    def append_proactive_turn(
        self,
        payload: dict[str, Any],
        *,
        max_session_turns: int = 4,
        max_global_turns: int = 512,
        commit: bool = True,
    ) -> list[dict[str, Any]]:
        scope = normalize_scope(payload.get("scope"))
        identity = (
            str(payload.get("channel") or ""), scope.tenant_id, scope.agent_id,
            scope.workspace_id, scope.user_id, str(payload.get("source_key") or ""),
            str(payload.get("session_id") or ""),
        )
        turn_id = str(payload.get("turn_id") or "")
        self.conn.execute(
            "DELETE FROM proactive_turns WHERE channel=? AND tenant_id=? AND agent_id=? AND workspace_id=? "
            "AND user_id=? AND source_key=? AND session_id=? AND turn_id=?",
            (*identity, turn_id),
        )
        self.conn.execute(
            "INSERT INTO proactive_turns(channel,tenant_id,agent_id,workspace_id,user_id,source_key,session_id,"
            "turn_id,summary,entities_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                *identity, turn_id, str(payload.get("summary") or ""),
                json.dumps(list(payload.get("entities") or []), ensure_ascii=False, sort_keys=True),
                str(payload.get("created_at") or datetime.now(timezone.utc).isoformat()),
            ),
        )
        session_rows = self.conn.execute(
            "SELECT entry_id FROM proactive_turns WHERE channel=? AND tenant_id=? AND agent_id=? AND workspace_id=? "
            "AND user_id=? AND source_key=? AND session_id=? ORDER BY entry_id DESC LIMIT ?",
            (*identity, max(1, int(max_session_turns)) + 1),
        ).fetchall()
        if len(session_rows) > max_session_turns:
            boundary = int(session_rows[max_session_turns - 1]["entry_id"])
            self.conn.execute(
                "DELETE FROM proactive_turns WHERE channel=? AND tenant_id=? AND agent_id=? AND workspace_id=? "
                "AND user_id=? AND source_key=? AND session_id=? AND entry_id < ?",
                (*identity, boundary),
            )
        global_rows = self.conn.execute(
            "SELECT entry_id FROM proactive_turns ORDER BY entry_id DESC LIMIT ?",
            (max(1, int(max_global_turns)) + 1,),
        ).fetchall()
        if len(global_rows) > max_global_turns:
            boundary = int(global_rows[max_global_turns - 1]["entry_id"])
            self.conn.execute("DELETE FROM proactive_turns WHERE entry_id < ?", (boundary,))
        if commit:
            self.conn.commit()
        return self.load_proactive_turns(payload)

    def load_proactive_turns(self, payload: dict[str, Any], *, limit: int = 4) -> list[dict[str, Any]]:
        scope = normalize_scope(payload.get("scope"))
        rows = self.conn.execute(
            "SELECT turn_id,summary,entities_json,created_at FROM proactive_turns WHERE channel=? AND tenant_id=? "
            "AND agent_id=? AND workspace_id=? AND user_id=? AND source_key=? AND session_id=? "
            "ORDER BY entry_id DESC LIMIT ?",
            (
                str(payload.get("channel") or ""), scope.tenant_id, scope.agent_id, scope.workspace_id,
                scope.user_id, str(payload.get("source_key") or ""), str(payload.get("session_id") or ""),
                max(1, min(4, int(limit))),
            ),
        ).fetchall()
        return [
            {
                "turn_id": str(row["turn_id"]), "summary": str(row["summary"]),
                "entities": [str(item) for item in json.loads(str(row["entities_json"] or "[]"))],
                "created_at": str(row["created_at"]),
            }
            for row in reversed(rows)
        ]

    def insert_proactive_decision(
        self,
        payload: dict[str, Any],
        items: list[dict[str, Any]],
        *,
        max_global_decisions: int = 512,
        commit: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        existing = self.load_proactive_decision(str(payload.get("decision_id") or ""))
        if existing is not None:
            stable = (
                "channel", "scope", "source_key", "session_id", "turn_id", "query_id",
                "query_digest", "policy_version", "release_identity", "control_cohort",
                "pair_id", "context", "task_type", "effective_query_digest",
            )
            if any(existing.get(key) != payload.get(key) for key in stable):
                raise ValueError("proactive decision identity conflict")
            requested_items = sorted(
                (
                    str(item.get("citation") or ""), str(item.get("record_id") or ""),
                    normalize_source_id(item.get("source_id")), round(float(item.get("confidence") or 0.0), 6),
                    bool(item.get("mandatory")), str(item.get("title") or ""),
                    str(item.get("text") or ""), int(item.get("order") or 0),
                )
                for item in items
            )
            stored_items = sorted(
                (
                    str(item.get("citation") or ""), str(item.get("record_id") or ""),
                    normalize_source_id(item.get("source_id")), round(float(item.get("confidence") or 0.0), 6),
                    bool(item.get("mandatory")), str(item.get("title") or ""),
                    str(item.get("text") or ""), int(item.get("order") or 0),
                )
                for item in existing.get("items", [])
            )
            if requested_items != stored_items:
                raise ValueError("proactive decision item conflict")
            return existing, True
        scope = normalize_scope(payload.get("scope"))
        release = dict(payload.get("release_identity") or {})
        created_at = str(payload.get("created_at") or datetime.now(timezone.utc).isoformat())
        self.conn.execute(
            "INSERT INTO proactive_decisions(decision_id,channel,tenant_id,agent_id,workspace_id,user_id,source_key,"
            "source_ids_json,session_id,turn_id,query_id,query_digest,query_text,task_type,effective_query_digest,policy_version,release_commit,"
            "release_version,deployment_receipt_id,release_session_id,release_bound,control_cohort,pair_id,context_text,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(payload["decision_id"]), str(payload["channel"]), scope.tenant_id, scope.agent_id,
                scope.workspace_id, scope.user_id, str(payload["source_key"]),
                json.dumps(list(payload.get("source_ids") or []), ensure_ascii=False), str(payload["session_id"]),
                str(payload.get("turn_id") or payload["query_id"]), str(payload["query_id"]),
                str(payload["query_digest"]), str(payload.get("query") or ""),
                str(payload.get("task_type") or ""), str(payload.get("effective_query_digest") or ""),
                str(payload["policy_version"]),
                str(release.get("release_commit") or ""), str(release.get("release_version") or ""),
                str(release.get("deployment_receipt_id") or ""), str(release.get("release_session_id") or ""),
                int(bool(payload.get("release_bound"))), int(bool(payload.get("control_cohort"))),
                str(payload.get("pair_id") or ""), str(payload.get("context") or ""),
                created_at, created_at,
            ),
        )
        for item in items:
            self.conn.execute(
                "INSERT INTO proactive_decision_items(decision_id,citation,record_id,source_id,confidence,state,ever_injected,mandatory,item_order,title_text,content_text,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(payload["decision_id"]), str(item["citation"]), str(item["record_id"]),
                    normalize_source_id(item["source_id"]), float(item.get("confidence") or 0.0),
                    str(item.get("state") or "volunteered"), int(str(item.get("state") or "") == "injected"),
                    int(bool(item.get("mandatory"))),
                    int(item.get("order") or 0), str(item.get("title") or ""),
                    str(item.get("text") or ""), created_at,
                ),
            )
        cap = max(1, int(max_global_decisions))
        boundary = self.conn.execute(
            "SELECT created_at,decision_id FROM proactive_decisions "
            "ORDER BY created_at DESC,decision_id DESC LIMIT 1 OFFSET ?",
            (cap - 1,),
        ).fetchone()
        if boundary is not None:
            stale_rows = self.conn.execute(
                "SELECT decision_id FROM proactive_decisions WHERE created_at < ? "
                "OR (created_at=? AND decision_id < ?)",
                (str(boundary["created_at"]), str(boundary["created_at"]), str(boundary["decision_id"])),
            ).fetchall()
            stale_ids = [str(row["decision_id"]) for row in stale_rows]
            for stale_id in stale_ids:
                # Delete children explicitly; deployments may have inherited a
                # connection where SQLite foreign_keys was not enabled.
                self.conn.execute(
                    "DELETE FROM proactive_decision_items WHERE decision_id=?", (stale_id,)
                )
                self.conn.execute("DELETE FROM proactive_decisions WHERE decision_id=?", (stale_id,))
        if commit:
            self.conn.commit()
        loaded = self.load_proactive_decision(str(payload["decision_id"]))
        if loaded is None:
            raise RuntimeError("proactive decision insert was not visible")
        return loaded, False

    def load_proactive_decision(self, decision_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM proactive_decisions WHERE decision_id=?", (str(decision_id or ""),)
        ).fetchone()
        if row is None:
            return None
        item_rows = self.conn.execute(
            "SELECT citation,record_id,source_id,confidence,state,ever_injected,mandatory,item_order AS 'order',title_text AS title,"
            "content_text AS text,updated_at "
            "FROM proactive_decision_items WHERE decision_id=? ORDER BY item_order,citation",
            (str(decision_id),),
        ).fetchall()
        return {
            "decision_id": str(row["decision_id"]), "channel": str(row["channel"]),
            "scope": {"tenant_id": str(row["tenant_id"]), "agent_id": str(row["agent_id"]),
                      "workspace_id": str(row["workspace_id"]), "user_id": str(row["user_id"])},
            "source_key": str(row["source_key"]),
            "source_ids": [str(item) for item in json.loads(str(row["source_ids_json"] or "[]"))],
            "session_id": str(row["session_id"]), "turn_id": str(row["turn_id"]),
            "query_id": str(row["query_id"]), "query_digest": str(row["query_digest"]),
            "query": str(row["query_text"]), "task_type": str(row["task_type"]),
            "effective_query_digest": str(row["effective_query_digest"]),
            "policy_version": str(row["policy_version"]),
            "release_identity": {"release_commit": str(row["release_commit"]),
                                 "release_version": str(row["release_version"]),
                                 "deployment_receipt_id": str(row["deployment_receipt_id"]),
                                 "release_session_id": str(row["release_session_id"])},
            "release_bound": bool(row["release_bound"]), "control_cohort": bool(row["control_cohort"]),
            "pair_id": str(row["pair_id"]), "terminal": bool(row["terminal"]),
            "context": str(row["context_text"] or ""),
            "outcome_success": None if row["outcome_success"] is None else bool(row["outcome_success"]),
            "outcome_verified": bool(row["outcome_verified"]),
            "outcome_quality": None if row["outcome_quality"] is None else float(row["outcome_quality"]),
            "outcome_latency_ms": None if row["outcome_latency_ms"] is None else float(row["outcome_latency_ms"]),
            "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"]),
            "items": [dict(item) for item in item_rows],
        }

    def find_proactive_decision(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Find only the newest decision in one exact host turn namespace."""

        scope = normalize_scope(payload.get("scope"))
        params: list[Any] = [
            str(payload.get("channel") or ""), scope.tenant_id, scope.agent_id,
            scope.workspace_id, scope.user_id, str(payload.get("source_key") or ""),
            str(payload.get("session_id") or ""), str(payload.get("turn_id") or ""),
        ]
        release = dict(payload.get("release_identity") or {})
        release_commit = str(release.get("release_commit") or payload.get("release_commit") or "")
        release_clause = ""
        if release_commit:
            release_clause = (
                " AND release_commit=? AND release_version=? AND deployment_receipt_id=? "
                "AND release_session_id=?"
            )
            params.extend(
                [
                    release_commit,
                    str(release.get("release_version") or ""),
                    str(release.get("deployment_receipt_id") or ""),
                    str(release.get("release_session_id") or ""),
                ]
            )
        row = self.conn.execute(
            "SELECT decision_id FROM proactive_decisions WHERE channel=? AND tenant_id=? AND agent_id=? "
            "AND workspace_id=? AND user_id=? AND source_key=? AND session_id=? AND turn_id=?"
            + release_clause
            + " ORDER BY created_at DESC,decision_id DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        return None if row is None else self.load_proactive_decision(str(row["decision_id"]))

    def list_stale_proactive_decisions(
        self,
        payload: dict[str, Any],
        *,
        before_created_at: str,
        before_injected_updated_at: str,
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        """List expired nonterminal decisions in one authoritative namespace."""

        scope = normalize_scope(payload.get("scope"))
        rows = self.conn.execute(
            "SELECT d.decision_id FROM proactive_decisions d WHERE d.channel=? AND d.tenant_id=? "
            "AND d.agent_id=? AND d.workspace_id=? AND d.user_id=? AND d.source_key=? "
            "AND d.terminal=0 AND ((d.created_at<? AND NOT EXISTS ("
            "SELECT 1 FROM proactive_decision_items i WHERE i.decision_id=d.decision_id AND i.state='injected'"
            ")) OR (d.updated_at<? AND EXISTS ("
            "SELECT 1 FROM proactive_decision_items i WHERE i.decision_id=d.decision_id AND i.state='injected'"
            "))) ORDER BY d.created_at,d.decision_id LIMIT ?",
            (
                str(payload.get("channel") or ""), scope.tenant_id, scope.agent_id,
                scope.workspace_id, scope.user_id, str(payload.get("source_key") or ""),
                str(before_created_at or ""), str(before_injected_updated_at or ""),
                max(1, min(512, int(limit))),
            ),
        ).fetchall()
        return [
            decision
            for row in rows
            if (decision := self.load_proactive_decision(str(row["decision_id"]))) is not None
        ]

    def proactive_session_refs(self, payload: dict[str, Any], *, limit: int = 512) -> set[tuple[str, str]]:
        scope = normalize_scope(payload.get("scope"))
        rows = self.conn.execute(
            "SELECT DISTINCT i.record_id,i.source_id FROM proactive_decision_items i JOIN proactive_decisions d "
            "ON d.decision_id=i.decision_id WHERE d.channel=? AND d.tenant_id=? AND d.agent_id=? "
            "AND d.workspace_id=? AND d.user_id=? AND d.source_key=? AND d.session_id=? "
            "ORDER BY d.created_at DESC,d.decision_id DESC LIMIT ?",
            (
                str(payload.get("channel") or ""), scope.tenant_id, scope.agent_id, scope.workspace_id,
                scope.user_id, str(payload.get("source_key") or ""), str(payload.get("session_id") or ""),
                max(1, min(24_576, int(limit))),
            ),
        ).fetchall()
        return {(str(row["record_id"]), str(row["source_id"])) for row in rows}

    def transition_proactive_items(
        self,
        decision_id: str,
        targets: dict[str, str],
        *,
        expected: dict[str, Any] | None = None,
        stale_lease_guard: dict[str, str] | None = None,
        commit: bool = True,
    ) -> list[dict[str, Any]] | None:
        decision = self.load_proactive_decision(decision_id)
        if decision is None:
            raise ValueError("exact proactive decision is required")
        for key, value in dict(expected or {}).items():
            if decision.get(key) != value:
                raise ValueError("proactive decision namespace mismatch")
        if stale_lease_guard is not None:
            has_injected = any(
                str(item.get("state") or "") == "injected"
                for item in decision.get("items") or []
            )
            cutoff = str(
                stale_lease_guard.get(
                    "before_injected_updated_at" if has_injected else "before_created_at"
                )
                or ""
            )
            observed = str(
                (
                    decision.get("updated_at")
                    if has_injected
                    else decision.get("created_at")
                )
                or ""
            )
            if not cutoff or not observed or observed >= cutoff:
                return None
        allowed = {
            "volunteered": {"injected", "not_used", "rejected", "suppressed"},
            "injected": {"used", "not_used", "rejected"},
            "used": {"rejected"}, "not_used": {"rejected"}, "suppressed": set(), "rejected": set(),
        }
        changed: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc).isoformat()
        by_citation = {str(item["citation"]): dict(item) for item in decision["items"]}
        for citation, target in targets.items():
            item = by_citation.get(str(citation))
            if item is None:
                raise ValueError("proactive citation does not belong to decision")
            current = str(item["state"])
            target = str(target)
            if current == target:
                continue
            if target not in allowed.get(current, set()):
                continue
            self.conn.execute(
                "UPDATE proactive_decision_items SET state=?,ever_injected=CASE WHEN ?='injected' THEN 1 ELSE ever_injected END,updated_at=? "
                "WHERE decision_id=? AND citation=? AND state=?",
                (target, target, now, str(decision_id), str(citation), current),
            )
            if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeError("proactive transition lost an atomic compare-and-swap")
            changed.append({**item, "previous_state": current, "state": target})
        remaining = self.conn.execute(
            "SELECT 1 FROM proactive_decision_items WHERE decision_id=? AND state NOT IN ('used','not_used','suppressed','rejected') LIMIT 1",
            (str(decision_id),),
        ).fetchone()
        self.conn.execute(
            "UPDATE proactive_decisions SET terminal=?,updated_at=? WHERE decision_id=?",
            (int(remaining is None), now, str(decision_id)),
        )
        if commit:
            self.conn.commit()
        return changed

    def update_proactive_outcome(
        self,
        decision_id: str,
        outcome: dict[str, Any],
        *,
        expected: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> bool:
        decision = self.load_proactive_decision(decision_id)
        if decision is None:
            raise ValueError("exact proactive decision is required")
        for key, value in dict(expected or {}).items():
            if decision.get(key) != value:
                raise ValueError("proactive decision namespace mismatch")
        if outcome.get("verified") is not True:
            raise ValueError("proactive outcome must be explicitly verified")
        success = outcome.get("success")
        if not isinstance(success, bool):
            raise ValueError("verified proactive outcome success must be boolean")
        quality = None if outcome.get("quality") is None else float(outcome["quality"])
        latency = None if outcome.get("latency_ms") is None else float(outcome["latency_ms"])
        requested = (success, quality, latency)
        if decision.get("outcome_verified") is True:
            existing = (
                decision.get("outcome_success"), decision.get("outcome_quality"),
                decision.get("outcome_latency_ms"),
            )
            if existing == requested:
                return False
            raise ValueError("verified proactive outcome conflict")
        self.conn.execute(
            "UPDATE proactive_decisions SET outcome_success=?,outcome_verified=?,outcome_quality=?,"
            "outcome_latency_ms=?,terminal=1,updated_at=? WHERE decision_id=? AND outcome_verified=0",
            (
                int(success), 1, quality, latency,
                datetime.now(timezone.utc).isoformat(), str(decision_id),
            ),
        )
        if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise RuntimeError("verified proactive outcome lost an atomic compare-and-swap")
        if commit:
            self.conn.commit()
        return True

    def list_proactive_outcomes(self, payload: dict[str, Any], *, limit: int = 500) -> list[dict[str, Any]]:
        scope = normalize_scope(payload.get("scope"))
        rows = self.conn.execute(
            "SELECT decision_id FROM proactive_decisions WHERE channel=? AND tenant_id=? AND agent_id=? "
            "AND workspace_id=? AND user_id=? AND source_key=? AND policy_version=? "
            "AND release_commit=? AND release_version=? AND deployment_receipt_id=? AND release_session_id=? "
            "ORDER BY created_at DESC,decision_id DESC LIMIT ?",
            (
                str(payload.get("channel") or ""), scope.tenant_id, scope.agent_id,
                scope.workspace_id, scope.user_id, str(payload.get("source_key") or ""),
                str(payload.get("policy_version") or ""), str(payload.get("release_commit") or ""),
                str(payload.get("release_version") or ""), str(payload.get("deployment_receipt_id") or ""),
                str(payload.get("release_session_id") or ""), max(1, min(5000, int(limit))),
            ),
        ).fetchall()
        return [
            decision for row in rows
            if (decision := self.load_proactive_decision(str(row["decision_id"]))) is not None
        ]

    def append_proactive_bypass(self, payload: dict[str, Any], *, max_entries: int = 64, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO proactive_bypass_diagnostics(channel,session_digest,query_digest,reason,created_at) VALUES(?,?,?,?,?)",
            (str(payload.get("channel") or ""), str(payload.get("session_digest") or ""),
             str(payload.get("query_digest") or ""), str(payload.get("reason") or ""),
             datetime.now(timezone.utc).isoformat()),
        )
        rows = self.conn.execute(
            "SELECT entry_id FROM proactive_bypass_diagnostics ORDER BY entry_id DESC LIMIT ?",
            (max(1, int(max_entries)) + 1,),
        ).fetchall()
        if len(rows) > max_entries:
            self.conn.execute(
                "DELETE FROM proactive_bypass_diagnostics WHERE entry_id < ?", (int(rows[max_entries - 1]["entry_id"]),)
            )
        if commit:
            self.conn.commit()

    def list_proactive_bypasses(self, *, limit: int = 64) -> list[dict[str, str]]:
        rows = self.conn.execute(
            "SELECT channel,session_digest,query_digest,reason,created_at FROM proactive_bypass_diagnostics "
            "ORDER BY entry_id DESC LIMIT ?", (max(1, min(512, int(limit))),)
        ).fetchall()
        return [dict(row) for row in rows]

    def register_adapter_tool_receipt(
        self,
        receipt: dict[str, Any],
        *,
        scope: ScopeRef | dict | None = None,
        commit: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        from eimemory.governance.tool_receipts import canonical_tool_receipt

        scope_ref = normalize_scope(scope)
        receipt = {
            **canonical_tool_receipt(receipt),
            "signature": str(receipt.get("signature") or "").strip().lower(),
        }
        channel = str(receipt.get("channel") or "").strip()
        session_id = str(receipt.get("session_id") or "").strip()
        run_id = str(receipt.get("run_id") or "").strip()
        tool_call_id = str(receipt.get("tool_call_id") or "").strip()
        row = self.conn.execute(
            """SELECT receipt_json FROM adapter_tool_receipts
               WHERE channel = ? AND tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?
                 AND session_id = ? AND run_id = ? AND tool_call_id = ?""",
            (channel, scope_ref.tenant_id, scope_ref.agent_id, scope_ref.workspace_id, scope_ref.user_id, session_id, run_id, tool_call_id),
        ).fetchone()
        if row is not None:
            existing = json.loads(str(row["receipt_json"]))
            conflict_fields = (
                "receipt_version",
                "channel",
                "source",
                "session_id",
                "run_id",
                "tool_call_id",
                "tool_name",
                "invocation_digest",
                "result_digest",
                "passed",
                "verification_policy_id",
                "retrieval_policy_digest",
                "release_commit",
                "release_version",
                "deployment_receipt_id",
                "release_session_id",
            )
            if any(existing.get(name) != receipt.get(name) for name in conflict_fields):
                raise ValueError("tool receipt conflict for an existing tool call")
            return existing, True
        self.conn.execute(
            """INSERT INTO adapter_tool_receipts (
                receipt_id, channel, source, tenant_id, agent_id, workspace_id, user_id,
                session_id, run_id, tool_call_id, eligible, receipt_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(receipt["receipt_id"]), channel, str(receipt.get("source") or ""),
                scope_ref.tenant_id, scope_ref.agent_id, scope_ref.workspace_id, scope_ref.user_id,
                session_id, run_id, tool_call_id, int(receipt.get("passed") is True),
                json.dumps(receipt, ensure_ascii=False, sort_keys=True), str(receipt.get("issued_at") or ""),
            ),
        )
        self.conn.execute(
            """UPDATE adapter_tool_receipts SET eligible = 0
               WHERE receipt_id IN (
                   SELECT receipt_id FROM adapter_tool_receipts
                   WHERE channel = ? AND tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?
                     AND session_id = ? AND run_id = ? AND eligible = 1 AND consumed_trace_id = ''
                   ORDER BY receipt_id DESC LIMIT -1 OFFSET ?
               )""",
            (
                channel,
                scope_ref.tenant_id,
                scope_ref.agent_id,
                scope_ref.workspace_id,
                scope_ref.user_id,
                session_id,
                run_id,
                MAX_ELIGIBLE_RECEIPTS_PER_RUN,
            ),
        )
        if commit:
            self.conn.commit()
        return dict(receipt), False

    def load_adapter_tool_receipts(
        self,
        receipt_ids: list[str],
        *,
        channel: str,
        session_id: str,
        run_id: str,
        scope: ScopeRef | dict | None = None,
    ) -> list[dict[str, Any]]:
        scope_ref = normalize_scope(scope)
        loaded: list[dict[str, Any]] = []
        for receipt_id in dict.fromkeys(str(item).strip() for item in receipt_ids if str(item).strip()):
            row = self.conn.execute(
                """SELECT receipt_json FROM adapter_tool_receipts
                   WHERE receipt_id = ? AND channel = ? AND tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?
                     AND session_id = ? AND run_id = ? AND eligible = 1""",
                (
                    receipt_id,
                    channel,
                    scope_ref.tenant_id,
                    scope_ref.agent_id,
                    scope_ref.workspace_id,
                    scope_ref.user_id,
                    session_id,
                    run_id,
                ),
            ).fetchone()
            if row is not None:
                loaded.append(json.loads(str(row["receipt_json"])))
        return loaded

    def load_claimable_adapter_tool_receipts(
        self,
        *,
        channel: str,
        session_id: str,
        run_id: str,
        trace_id: str,
        scope: ScopeRef | dict | None = None,
    ) -> list[dict[str, Any]]:
        scope_ref = normalize_scope(scope)
        rows = self.conn.execute(
            """SELECT receipt_json FROM adapter_tool_receipts
               WHERE channel = ? AND tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?
                 AND session_id = ? AND run_id = ? AND eligible = 1
                 AND consumed_trace_id IN ('', ?)
               ORDER BY created_at, receipt_id""",
            (
                channel,
                scope_ref.tenant_id,
                scope_ref.agent_id,
                scope_ref.workspace_id,
                scope_ref.user_id,
                session_id,
                run_id,
                trace_id,
            ),
        ).fetchall()
        return [json.loads(str(row["receipt_json"])) for row in rows]

    def load_adapter_tool_receipt_states(
        self,
        receipt_ids: list[str],
        *,
        channel: str,
        session_id: str,
        run_id: str,
        scope: ScopeRef | dict | None = None,
    ) -> dict[str, dict[str, Any]]:
        scope_ref = normalize_scope(scope)
        states: dict[str, dict[str, Any]] = {}
        for receipt_id in dict.fromkeys(
            str(item).strip() for item in receipt_ids if str(item).strip()
        ):
            row = self.conn.execute(
                """SELECT receipt_id, eligible, consumed_trace_id FROM adapter_tool_receipts
                   WHERE receipt_id = ? AND channel = ?
                     AND tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?
                     AND session_id = ? AND run_id = ?""",
                (
                    receipt_id,
                    channel,
                    scope_ref.tenant_id,
                    scope_ref.agent_id,
                    scope_ref.workspace_id,
                    scope_ref.user_id,
                    session_id,
                    run_id,
                ),
            ).fetchone()
            if row is not None:
                states[receipt_id] = {
                    "eligible": bool(row["eligible"]),
                    "consumed_trace_id": str(row["consumed_trace_id"] or ""),
                }
        return states

    def quarantine_adapter_tool_receipts(
        self,
        receipt_ids: list[str],
        *,
        channel: str,
        session_id: str,
        run_id: str,
        scope: ScopeRef | dict | None = None,
        commit: bool = True,
    ) -> None:
        scope_ref = normalize_scope(scope)
        clean_ids = list(
            dict.fromkeys(str(item).strip() for item in receipt_ids if str(item).strip())
        )
        if not clean_ids:
            return
        placeholders = ",".join("?" for _ in clean_ids)
        self.conn.execute(
            f"""UPDATE adapter_tool_receipts SET eligible = 0
                WHERE receipt_id IN ({placeholders}) AND channel = ?
                  AND tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?
                  AND session_id = ? AND run_id = ? AND consumed_trace_id = ''""",
            (
                *clean_ids,
                channel,
                scope_ref.tenant_id,
                scope_ref.agent_id,
                scope_ref.workspace_id,
                scope_ref.user_id,
                session_id,
                run_id,
            ),
        )
        if commit:
            self.conn.commit()

    def consume_adapter_tool_receipts(
        self,
        receipt_ids: list[str],
        *,
        channel: str,
        session_id: str,
        run_id: str,
        trace_id: str,
        scope: ScopeRef | dict | None = None,
        commit: bool = True,
    ) -> list[dict[str, Any]]:
        scope_ref = normalize_scope(scope)
        accepted: list[dict[str, Any]] = []
        for receipt_id in dict.fromkeys(str(item).strip() for item in receipt_ids if str(item).strip()):
            row = self.conn.execute(
                """SELECT receipt_json, consumed_trace_id FROM adapter_tool_receipts
                   WHERE receipt_id = ? AND channel = ? AND tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?
                     AND session_id = ? AND run_id = ? AND eligible = 1""",
                (receipt_id, channel, scope_ref.tenant_id, scope_ref.agent_id, scope_ref.workspace_id, scope_ref.user_id, session_id, run_id),
            ).fetchone()
            if row is None:
                continue
            consumed_trace_id = str(row["consumed_trace_id"] or "")
            if consumed_trace_id not in {"", trace_id}:
                raise ValueError("tool receipt was consumed by a different terminal trace")
            self.conn.execute(
                "UPDATE adapter_tool_receipts SET consumed_trace_id = ? WHERE receipt_id = ? AND consumed_trace_id IN ('', ?)",
                (trace_id, receipt_id, trace_id),
            )
            accepted.append(json.loads(str(row["receipt_json"])))
        if commit:
            self.conn.commit()
        return accepted

    def _create_indexes(self) -> None:
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_records_record_id ON records(record_id)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_scope ON records(tenant_id, agent_id, workspace_id, user_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_scope_updated "
            "ON records(tenant_id, agent_id, workspace_id, user_id, updated_at DESC, record_id DESC)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_scope_source_updated "
            "ON records(tenant_id, agent_id, workspace_id, user_id, source_id, updated_at DESC, record_id DESC, status, storage_key)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_records_kind_scope ON records(kind, tenant_id, agent_id, workspace_id, user_id)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_kind_scope_updated "
            "ON records(kind, tenant_id, agent_id, workspace_id, user_id, updated_at DESC, record_id DESC)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_kind_scope_status_updated "
            "ON records(kind, tenant_id, agent_id, workspace_id, user_id, status, updated_at DESC, record_id DESC)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_kind_scope_created "
            "ON records(kind, tenant_id, agent_id, workspace_id, user_id, created_at DESC, record_id DESC)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_records_source_kind ON records(source, kind)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_idempotency "
            "ON records(kind, tenant_id, agent_id, workspace_id, user_id, idempotency_key, updated_at DESC, record_id DESC)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_semantic "
            "ON records(kind, tenant_id, agent_id, workspace_id, user_id, semantic_key, updated_at DESC, record_id DESC)"
        )
        try:
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_records_meta_capability "
                "ON records(kind, tenant_id, agent_id, workspace_id, user_id, "
                "CAST(json_extract(meta_json, '$.capability') AS TEXT), updated_at DESC, record_id DESC)"
            )
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_records_meta_report_type "
                "ON records(kind, tenant_id, agent_id, workspace_id, user_id, "
                "CAST(json_extract(meta_json, '$.report_type') AS TEXT), updated_at DESC, record_id DESC)"
            )
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_records_meta_session_id "
                "ON records(kind, tenant_id, agent_id, workspace_id, user_id, "
                "CAST(json_extract(meta_json, '$.session_id') AS TEXT), updated_at DESC, record_id DESC)"
            )
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_records_policy_task "
                "ON records(kind, status, tenant_id, agent_id, workspace_id, user_id, "
                "CAST(COALESCE(json_extract(meta_json, '$.task_type'), "
                "json_extract(meta_json, '$.business_meta.task_type')) AS TEXT), updated_at DESC)"
            )
        except sqlite3.OperationalError:
            pass

    def _create_recall_index_tables(self, *, create_indexes: bool = True) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recall_index (
                storage_key TEXT PRIMARY KEY,
                record_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT 'default',
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                lane TEXT NOT NULL,
                visibility TEXT NOT NULL,
                source_class TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                projection_type TEXT NOT NULL,
                quality_score REAL NOT NULL DEFAULT 0.0,
                title_text TEXT NOT NULL,
                title_normalized TEXT NOT NULL DEFAULT '',
                body_text TEXT NOT NULL,
                anchor_terms TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        if create_indexes:
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_recall_index_scope_lane ON recall_index(tenant_id, agent_id, workspace_id, user_id, lane, visibility)"
            )
            self._create_source_partition_indexes()
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_recall_index_kind ON recall_index(kind, visibility)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_recall_index_source_class ON recall_index(source_class, visibility)")
        try:
            self.conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS recall_index_fts
                USING fts5(storage_key UNINDEXED, title_text, body_text, anchor_terms, tokenize='unicode61')
                """
            )
        except sqlite3.OperationalError:
            # Some embedded SQLite builds omit FTS5. Anchor candidates still keep recall functional.
            pass

    def _create_recall_identity_tables(self, *, rebuild_indexes: bool = False) -> None:
        if rebuild_indexes:
            self.conn.execute("DROP INDEX IF EXISTS idx_recall_index_storage_key")
            self.conn.execute("DROP INDEX IF EXISTS idx_recall_alias_exact")
            self.conn.execute("DROP INDEX IF EXISTS idx_recall_alias_exact_kind")
            self.conn.execute("DROP INDEX IF EXISTS idx_recall_title_exact")
            self.conn.execute("DROP INDEX IF EXISTS idx_recall_title_exact_kind")
        alias_table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recall_alias_index'"
        ).fetchone()
        if alias_table and not self._recall_alias_table_ready():
            self.conn.execute("DROP TABLE recall_alias_index")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recall_alias_index (
                storage_key TEXT NOT NULL,
                normalized_alias TEXT NOT NULL,
                alias_ordinal INTEGER NOT NULL,
                record_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                source_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                PRIMARY KEY (storage_key, normalized_alias)
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recall_index_storage_key ON recall_index(storage_key)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recall_alias_exact "
            "ON recall_alias_index(tenant_id, agent_id, workspace_id, user_id, normalized_alias, status, source_id, kind, storage_key)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recall_title_exact "
            "ON recall_index(tenant_id, agent_id, workspace_id, user_id, title_normalized, status, source_id, kind, storage_key)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recall_alias_exact_kind "
            "ON recall_alias_index(tenant_id, agent_id, workspace_id, user_id, normalized_alias, status, kind, source_id, storage_key)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recall_title_exact_kind "
            "ON recall_index(tenant_id, agent_id, workspace_id, user_id, title_normalized, status, kind, source_id, storage_key)"
        )

    def _recall_alias_table_ready(self) -> bool:
        expected = [
            ("storage_key", "TEXT", 1, 1),
            ("normalized_alias", "TEXT", 1, 2),
            ("alias_ordinal", "INTEGER", 1, 0),
            ("record_id", "TEXT", 1, 0),
            ("kind", "TEXT", 1, 0),
            ("status", "TEXT", 1, 0),
            ("source_id", "TEXT", 1, 0),
            ("tenant_id", "TEXT", 1, 0),
            ("agent_id", "TEXT", 1, 0),
            ("workspace_id", "TEXT", 1, 0),
            ("user_id", "TEXT", 1, 0),
        ]
        rows = self.conn.execute("PRAGMA table_info(recall_alias_index)").fetchall()
        actual = [
            (str(row["name"]), str(row["type"]).upper(), int(row["notnull"]), int(row["pk"]))
            for row in rows
        ]
        return actual == expected

    def _identity_index_ready(self, *, table: str, index_name: str, expected_columns: list[str]) -> bool:
        index_rows = [
            row for row in self.conn.execute(f"PRAGMA index_list({table})") if str(row["name"]) == index_name
        ]
        if len(index_rows) != 1:
            return False
        index_row = index_rows[0]
        if int(index_row["unique"]) != 0 or int(index_row["partial"]) != 0 or str(index_row["origin"]) != "c":
            return False
        key_rows = [row for row in self.conn.execute(f"PRAGMA index_xinfo({index_name})") if int(row["key"])]
        return [str(row["name"]) for row in key_rows] == expected_columns and all(
            str(row["coll"] or "").upper() == "BINARY" and int(row["desc"]) == 0
            for row in key_rows
        )

    def _create_memory_edge_tables(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_edges (
                edge_id TEXT PRIMARY KEY,
                from_id TEXT NOT NULL,
                to_id TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.0,
                evidence_id TEXT NOT NULL DEFAULT '',
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_edges_scope_type "
            "ON memory_edges(tenant_id, agent_id, workspace_id, user_id, edge_type, updated_at)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_edges_from ON memory_edges(from_id, edge_type)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_edges_to ON memory_edges(to_id, edge_type)")

    def _create_event_memory_tables(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                user_phrase TEXT NOT NULL,
                event_type TEXT NOT NULL,
                interpreted_intent TEXT NOT NULL,
                goal TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.0,
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS intent_patterns (
                id TEXT PRIMARY KEY,
                pattern TEXT NOT NULL,
                default_event_type TEXT NOT NULL,
                interpreted_intent TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'active',
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                last_rollback_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_outcomes (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason TEXT NOT NULL,
                correction_from_user TEXT NOT NULL,
                policy_update TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_scope_type ON events(tenant_id, agent_id, workspace_id, user_id, event_type, timestamp)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_intent_patterns_scope_type ON intent_patterns(tenant_id, agent_id, workspace_id, user_id, default_event_type)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_event_outcomes_event ON event_outcomes(event_id)")

    def _create_policy_rollout_tables(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS policy_rollout_ledger (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                record_date TEXT NOT NULL,
                action_type TEXT NOT NULL,
                promotion_id TEXT NOT NULL,
                is_auto INTEGER NOT NULL DEFAULT 1,
                source_opportunity_id TEXT NOT NULL DEFAULT '',
                source_opportunity_json TEXT NOT NULL DEFAULT '{}',
                trust_report_json TEXT NOT NULL DEFAULT '{}',
                replay_report_json TEXT NOT NULL DEFAULT '{}',
                applied_pattern_id TEXT NOT NULL DEFAULT '',
                budget_decision TEXT NOT NULL DEFAULT '',
                rollback_policy_id TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_policy_rollout_scope_date "
            "ON policy_rollout_ledger(tenant_id, agent_id, workspace_id, user_id, action_type, record_date, created_at)"
        )

    def _create_export_outbox_table(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS export_outbox (
                operation_id TEXT PRIMARY KEY,
                stream TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                exported_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_export_outbox_pending "
            "ON export_outbox(state, created_at, operation_id)"
        )

    def _create_replay_manifest_sequence_table(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS replay_manifest_sequences (
                scope_key TEXT NOT NULL,
                capability TEXT NOT NULL,
                high_water INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope_key, capability)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS replay_manifest_evidence (
                scope_key TEXT NOT NULL,
                capability TEXT NOT NULL,
                manifest_sequence INTEGER NOT NULL,
                manifest_record_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (
                    scope_key, capability, manifest_sequence, manifest_record_id
                )
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_replay_manifest_evidence_high_water "
            "ON replay_manifest_evidence(scope_key, capability, manifest_sequence DESC)"
        )
        try:
            self.conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_replay_pack_scope_sequence_case
                ON records(
                    tenant_id, agent_id, workspace_id, user_id,
                    CAST(json_extract(meta_json, '$.capability') AS TEXT),
                    CAST(json_extract(meta_json, '$.manifest_sequence') AS INTEGER),
                    CAST(json_extract(meta_json, '$.case_id') AS TEXT)
                )
                WHERE kind = 'replay_result'
                  AND CAST(json_extract(meta_json, '$.report_type') AS TEXT) = 'capability_replay_pack'
                  AND json_extract(meta_json, '$.manifest_sequence') IS NOT NULL
                """
            )
        except sqlite3.OperationalError:
            pass

    def allocate_replay_manifest_sequences(
        self,
        *,
        scope: ScopeRef,
        capabilities: list[str],
        floor_by_capability: dict[str, int] | None = None,
    ) -> dict[str, int]:
        selected = sorted({str(item).strip() for item in capabilities if str(item).strip()})
        if not selected:
            return {}
        scope_key = self._replay_scope_key(scope)
        floors = dict(floor_by_capability or {})
        allocated: dict[str, int] = {}
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for capability in selected:
                try:
                    floor = max(0, int(floors.get(capability) or 0))
                except (TypeError, ValueError):
                    floor = 0
                updated_at = datetime.now(timezone.utc).isoformat()
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO replay_manifest_sequences (
                        scope_key, capability, high_water, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (scope_key, capability, floor, updated_at),
                )
                self.conn.execute(
                    """
                    UPDATE replay_manifest_sequences
                    SET high_water = CASE
                            WHEN high_water < ? THEN ? + 1
                            ELSE high_water + 1
                        END,
                        updated_at = ?
                    WHERE scope_key = ? AND capability = ?
                    """,
                    (floor, floor, updated_at, scope_key, capability),
                )
                row = self.conn.execute(
                    "SELECT high_water FROM replay_manifest_sequences "
                    "WHERE scope_key = ? AND capability = ?",
                    (scope_key, capability),
                ).fetchone()
                if row is None:
                    raise RuntimeError("replay manifest sequence allocation was not persisted")
                allocated[capability] = int(row["high_water"])
            self.conn.commit()
            return allocated
        except Exception:
            self.conn.rollback()
            raise

    def replay_manifest_sequence_state(
        self,
        *,
        scope: ScopeRef,
        capabilities: list[str] | set[str],
    ) -> dict[str, dict[str, object]]:
        selected = sorted({str(value or "").strip() for value in capabilities if str(value or "").strip()})
        state: dict[str, dict[str, object]] = {
            capability: {"sequence": 0, "manifest_record_ids": set()}
            for capability in selected
        }
        scope_key = self._replay_scope_key(scope)
        for capability in selected:
            rows = self.conn.execute(
                """
                SELECT manifest_sequence, manifest_record_id
                FROM replay_manifest_evidence
                WHERE scope_key = ? AND capability = ?
                  AND manifest_sequence = (
                      SELECT MAX(manifest_sequence)
                      FROM replay_manifest_evidence
                      WHERE scope_key = ? AND capability = ?
                  )
                ORDER BY manifest_record_id ASC
                """,
                (scope_key, capability, scope_key, capability),
            ).fetchall()
            if rows:
                state[capability] = {
                    "sequence": int(rows[0]["manifest_sequence"]),
                    "manifest_record_ids": {
                        str(row["manifest_record_id"])
                        for row in rows
                        if str(row["manifest_record_id"] or "").strip()
                    },
                }
        return state

    @staticmethod
    def _replay_scope_key(scope: ScopeRef) -> str:
        return canonical_payload_json(
            {
                "tenant_id": scope.tenant_id,
                "agent_id": scope.agent_id,
                "workspace_id": scope.workspace_id,
                "user_id": scope.user_id,
            }
        )

    def enqueue_export(
        self,
        *,
        stream: str,
        payload: dict[str, Any],
        operation_id: str = "",
        commit: bool = True,
    ) -> dict[str, Any]:
        clean_stream = str(stream or "").strip()
        if not clean_stream or not re.fullmatch(r"[a-z0-9_.-]+", clean_stream):
            raise ValueError("export stream must be a nonempty safe name")
        raw_payload = canonical_payload_json(dict(payload or {}))
        digest = payload_digest(dict(payload or {}))
        resolved_operation_id = str(operation_id or "").strip() or sha256(
            f"{clean_stream}\0{digest}".encode("utf-8")
        ).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO export_outbox (
                operation_id, stream, payload_json, payload_digest, state,
                created_at, exported_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, '')
            """,
            (resolved_operation_id, clean_stream, raw_payload, digest, created_at),
        )
        existing = self.conn.execute(
            "SELECT stream, payload_digest, state, created_at, exported_at "
            "FROM export_outbox WHERE operation_id = ?",
            (resolved_operation_id,),
        ).fetchone()
        if existing is None:
            raise RuntimeError("failed to persist export outbox row")
        if str(existing["stream"]) != clean_stream or str(existing["payload_digest"]) != digest:
            raise ValueError("export operation id collision")
        if commit:
            self.conn.commit()
        return {
            "operation_id": resolved_operation_id,
            "stream": clean_stream,
            "payload_digest": digest,
            "state": str(existing["state"]),
            "created_at": str(existing["created_at"]),
            "exported_at": str(existing["exported_at"]),
        }

    def pending_exports(
        self,
        *,
        limit: int = 100,
        operation_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        max_limit = max(1, min(1_000, int(limit)))
        where = ["state = 'pending'"]
        params: list[Any] = []
        clean_ids = [str(item) for item in (operation_ids or []) if str(item)]
        if clean_ids:
            where.append(f"operation_id IN ({','.join('?' for _ in clean_ids)})")
            params.extend(clean_ids)
        rows = self.conn.execute(
            "SELECT operation_id, stream, payload_json, payload_digest, created_at "
            "FROM export_outbox WHERE "
            + " AND ".join(where)
            + " ORDER BY created_at, operation_id LIMIT ?",
            [*params, max_limit],
        ).fetchall()
        return [
            {
                "operation_id": str(row["operation_id"]),
                "stream": str(row["stream"]),
                "payload": json.loads(str(row["payload_json"])),
                "payload_digest": str(row["payload_digest"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def mark_exported(
        self,
        operation_id: str,
        *,
        commit: bool = True,
    ) -> bool:
        result = self.conn.execute(
            "UPDATE export_outbox SET state = 'exported', exported_at = ? "
            "WHERE operation_id = ? AND state = 'pending'",
            (datetime.now(timezone.utc).isoformat(), str(operation_id)),
        )
        if commit:
            self.conn.commit()
        return result.rowcount > 0

    def newest_pending_export_ids(self, *, limit: int = 100) -> list[str]:
        rows = self.conn.execute(
            "SELECT operation_id FROM export_outbox WHERE state = 'pending' "
            "ORDER BY rowid DESC LIMIT ?",
            (max(1, min(1_000, int(limit))),),
        ).fetchall()
        return [str(row["operation_id"]) for row in rows]

    def prune_exported(self, *, keep: int = 10_000, commit: bool = True) -> int:
        keep_count = max(0, min(100_000, int(keep)))
        result = self.conn.execute(
            """
            DELETE FROM export_outbox
            WHERE state = 'exported'
              AND operation_id NOT IN (
                SELECT operation_id
                FROM export_outbox
                WHERE state = 'exported'
                ORDER BY exported_at DESC, operation_id DESC
                LIMIT ?
              )
            """,
            (keep_count,),
        )
        if commit:
            self.conn.commit()
        return max(0, int(result.rowcount))

    def preload_hot_pages(self, *, limit: int | None = None) -> dict[str, int]:
        if limit is None:
            try:
                limit = int(os.environ.get("EIMEMORY_PRELOAD_HOT_ROWS") or 128)
            except ValueError:
                limit = 128
        bounded = max(0, min(1_000, int(limit)))
        if bounded == 0:
            return {"limit": 0, "records": 0, "patterns": 0, "events": 0}
        counts: dict[str, int] = {"limit": bounded}
        for name, query in (
            ("records", "SELECT substr(payload_json, 1, 256) FROM records ORDER BY rowid DESC LIMIT ?"),
            ("patterns", "SELECT substr(payload_json, 1, 256) FROM intent_patterns ORDER BY rowid DESC LIMIT ?"),
            ("events", "SELECT substr(payload_json, 1, 256) FROM events ORDER BY rowid DESC LIMIT ?"),
        ):
            rows = self.conn.execute(query, (bounded,)).fetchall()
            counts[name] = len(rows)
        return counts

    def maintain(self, *, outbox_keep: int = 10_000) -> dict[str, Any]:
        pruned = self.prune_exported(keep=outbox_keep, commit=False)
        page_count = int(self.conn.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
        vacuumed_pages = 0
        if freelist_count >= 1_000 and freelist_count * 5 >= max(1, page_count):
            vacuumed_pages = min(freelist_count, 2_000)
            self.conn.execute(f"PRAGMA incremental_vacuum({vacuumed_pages})")
        self.conn.execute("PRAGMA optimize")
        self.conn.commit()
        checkpoint = tuple(self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
        return {
            "ok": True,
            "outbox_pruned": pruned,
            "page_count": page_count,
            "freelist_count": freelist_count,
            "incremental_vacuum_pages": vacuumed_pages,
            "wal_checkpoint": [int(value) for value in checkpoint],
        }

    def _migrate_intent_patterns_schema(self) -> None:
        if self._schema_migration_applied(_INTENT_PATTERN_STATUS_MIGRATION):
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self._schema_migration_applied(_INTENT_PATTERN_STATUS_MIGRATION):
                self.conn.commit()
                return
            columns = {
                row["name"] for row in self.conn.execute("PRAGMA table_info(intent_patterns)").fetchall()
            }
            if "status" not in columns:
                self.conn.execute("ALTER TABLE intent_patterns ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            if "last_rollback_reason" not in columns:
                self.conn.execute(
                    "ALTER TABLE intent_patterns ADD COLUMN last_rollback_reason TEXT NOT NULL DEFAULT ''"
                )
            if "payload_json" in columns:
                cursor = self.conn.execute("SELECT id, status, payload_json FROM intent_patterns")
                while True:
                    rows = cursor.fetchmany(200)
                    if not rows:
                        break
                    updates: list[tuple[str, str, str]] = []
                    for row in rows:
                        raw = str(row["payload_json"])
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(payload, dict):
                            continue
                        status = str(payload.get("status") or "active")
                        if status not in {"candidate", "shadow", "active", "rolled_back", "quarantined"}:
                            status = "active"
                        payload["status"] = status
                        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                        if normalized != raw or str(row["status"] or "") != status:
                            updates.append((normalized, status, str(row["id"])))
                    if updates:
                        self.conn.executemany(
                            "UPDATE intent_patterns SET payload_json = ?, status = ? WHERE id = ?",
                            updates,
                        )
            self._mark_schema_migration(_INTENT_PATTERN_STATUS_MIGRATION)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _seed_default_intent_patterns(self) -> None:
        scope = ScopeRef()
        for payload in DEFAULT_INTENT_PATTERNS:
            pattern = ensure_pattern_payload(payload, scope)
            existing = self.conn.execute(
                "SELECT 1 FROM intent_patterns WHERE id = ?",
                (pattern["id"],),
            ).fetchone()
            if existing:
                continue
            self.upsert_intent_pattern(pattern, scope=scope, commit=False)

    def _migrate_to_scoped_storage_key(self, columns: set[str]) -> None:
        self.conn.execute("ALTER TABLE records RENAME TO records_legacy")
        self._create_records_table()
        select_columns = [
            "record_id", "kind", "status", "title", "summary", "detail",
            "content_text", "source", "agent_id", "workspace_id", "user_id",
            "tenant_id", "meta_json", "payload_json", "created_at", "updated_at",
        ]
        if "embedding_json" in columns:
            select_columns.insert(12, "embedding_json")
        cursor = self.conn.execute(f"SELECT {', '.join(select_columns)} FROM records_legacy")
        while True:
            rows = cursor.fetchmany(500)
            if not rows:
                break
            for row in rows:
                row_data = dict(row)
                idempotency_key, semantic_key = _record_meta_keys_from_json(row_data["meta_json"])
                storage_key = self._storage_key_from_values(
                    record_id=str(row_data["record_id"]),
                    tenant_id=str(row_data.get("tenant_id") or "default"),
                    agent_id=str(row_data.get("agent_id") or ""),
                    workspace_id=str(row_data.get("workspace_id") or ""),
                    user_id=str(row_data.get("user_id") or ""),
                )
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO records (
                        storage_key, record_id, kind, status, title, summary, detail,
                        content_text, source, agent_id, workspace_id, user_id, tenant_id,
                        embedding_json, idempotency_key, semantic_key, meta_json, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        storage_key,
                        row_data["record_id"],
                        row_data["kind"],
                        row_data["status"],
                        row_data["title"],
                        row_data["summary"],
                        row_data["detail"],
                        row_data["content_text"],
                        row_data["source"],
                        row_data["agent_id"],
                        row_data["workspace_id"],
                        row_data["user_id"],
                        row_data["tenant_id"],
                        row_data.get("embedding_json", "[]"),
                        idempotency_key,
                        semantic_key,
                        row_data["meta_json"],
                        row_data["payload_json"],
                        row_data["created_at"],
                        row_data["updated_at"],
                    ),
                )
        self.conn.execute("DROP TABLE records_legacy")
        self.conn.commit()

    def _create_schema_migrations_table(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )

    def _mark_schema_migration(self, migration_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
            (migration_id, datetime.now(timezone.utc).isoformat()),
        )

    def _schema_migration_applied(self, migration_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
            (migration_id,),
        ).fetchone() is not None

    def _backfill_record_meta_keys_if_needed(self) -> None:
        if self._schema_migration_applied(_RECORD_META_KEYS_MIGRATION):
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self._schema_migration_applied(_RECORD_META_KEYS_MIGRATION):
                self.conn.commit()
                return
            last_storage_key = ""
            while True:
                rows = self.conn.execute(
                    """
                    SELECT storage_key, meta_json
                    FROM records
                    WHERE storage_key > ?
                      AND (idempotency_key = '' OR semantic_key = '')
                    ORDER BY storage_key
                    LIMIT 500
                    """,
                    (last_storage_key,),
                ).fetchall()
                if not rows:
                    break
                updates = []
                for row in rows:
                    idempotency_key, semantic_key = _record_meta_keys_from_json(str(row["meta_json"] or "{}"))
                    if idempotency_key or semantic_key:
                        updates.append((idempotency_key, semantic_key, str(row["storage_key"])))
                if updates:
                    self.conn.executemany(
                        "UPDATE records SET idempotency_key = ?, semantic_key = ? WHERE storage_key = ?",
                        updates,
                    )
                last_storage_key = str(rows[-1]["storage_key"])
            self._mark_schema_migration(_RECORD_META_KEYS_MIGRATION)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _migrate_source_partition_schema(self) -> None:
        """Add the explicit partition projection without trusting provenance IDs."""

        legacy_mapping_required = not self._schema_migration_applied(_SOURCE_PARTITION_MIGRATION)
        if not legacy_mapping_required and self._source_partition_physical_ready():
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            record_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(records)")}
            records_source_added = "source_id" not in record_columns
            if "source_id" not in record_columns:
                self.conn.execute(
                    "ALTER TABLE records ADD COLUMN source_id TEXT NOT NULL DEFAULT 'default'"
                )
            self._create_recall_index_tables(create_indexes=False)
            recall_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(recall_index)")}
            if "source_id" not in recall_columns:
                self.conn.execute(
                    "ALTER TABLE recall_index ADD COLUMN source_id TEXT NOT NULL DEFAULT 'default'"
                )
            if "title_normalized" not in recall_columns:
                self.conn.execute(
                    "ALTER TABLE recall_index ADD COLUMN title_normalized TEXT NOT NULL DEFAULT ''"
                )
            if not self._recall_index_table_ready():
                self.conn.execute("DROP TABLE recall_index")
                self._create_recall_index_tables(create_indexes=False)
            self._create_recall_index_tables()
            self._create_recall_identity_tables()
            self._create_indexes()
            self._create_source_partition_indexes(rebuild=True)

            diagnostics: dict[str, int] = {"ambiguous": 0, "invalid": 0}
            if legacy_mapping_required or records_source_added:
                last_storage_key = ""
                while True:
                    rows = self.conn.execute(
                        "SELECT storage_key, kind, payload_json FROM records "
                        "WHERE storage_key > ? ORDER BY storage_key LIMIT 200",
                        (last_storage_key,),
                    ).fetchall()
                    if not rows:
                        break
                    updates: list[tuple[str, str, str]] = []
                    for row in rows:
                        payload = self._payload_dict_from_json(row["payload_json"])
                        if payload is None:
                            diagnostics["corrupt"] = diagnostics.get("corrupt", 0) + 1
                            continue
                        if legacy_mapping_required:
                            source_id, reason = self._legacy_source_partition(payload, str(row["kind"] or ""))
                            if reason:
                                diagnostics[reason] = diagnostics.get(reason, 0) + 1
                            payload["source_id"] = source_id
                            updates.append((source_id, json.dumps(payload, ensure_ascii=False), str(row["storage_key"])))
                        else:
                            source_id = normalize_source_id(payload.get("source_id", DEFAULT_SOURCE_ID))
                            updates.append((source_id, str(row["payload_json"]), str(row["storage_key"])))
                    self.conn.executemany(
                        "UPDATE records SET source_id = ?, payload_json = ? WHERE storage_key = ?", updates
                    )
                    last_storage_key = str(rows[-1]["storage_key"])
            # Rebuild the projection in this migration transaction so the two tables cannot disagree.
            self.conn.execute("DELETE FROM recall_index")
            self.conn.execute("DELETE FROM recall_alias_index")
            if self._has_fts_table():
                self.conn.execute("DELETE FROM recall_index_fts")
            self._backfill_recall_index_if_needed()
            self._mark_schema_migration(_SOURCE_PARTITION_MIGRATION)
            self.conn.commit()
            self.source_partition_migration_diagnostics = {
                key: min(value, 1_000) for key, value in diagnostics.items() if value
            }
        except Exception:
            self.conn.rollback()
            raise

    def _migrate_recall_identity_schema(self) -> None:
        if self._schema_migration_applied(_RECALL_IDENTITY_MIGRATION) and self._recall_identity_physical_ready():
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._create_recall_index_tables(create_indexes=False)
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(recall_index)")}
            if "title_normalized" not in columns:
                self.conn.execute(
                    "ALTER TABLE recall_index ADD COLUMN title_normalized TEXT NOT NULL DEFAULT ''"
                )
            if not self._recall_index_table_ready():
                # recall_index is a derived projection. Rebuilding it inside
                # this migration transaction is safer than repeatedly trying
                # to write through an incompatible affinity/nullability.
                self.conn.execute("DROP TABLE recall_index")
                self._create_recall_index_tables(create_indexes=False)
            self._create_recall_identity_tables(rebuild_indexes=True)
            self.conn.execute("DELETE FROM recall_index")
            self.conn.execute("DELETE FROM recall_alias_index")
            if self._has_fts_table():
                self.conn.execute("DELETE FROM recall_index_fts")
            last_storage_key = ""
            while True:
                rows = self.conn.execute(
                    "SELECT storage_key, payload_json, content_text FROM records "
                    "WHERE storage_key > ? ORDER BY storage_key LIMIT 200",
                    (last_storage_key,),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    payload = self._payload_dict_from_json(row["payload_json"])
                    record = self._record_from_payload_dict(payload)
                    if record is not None:
                        self._upsert_recall_index(
                            record=record,
                            storage_key=str(row["storage_key"]),
                            content_text=str(row["content_text"] or ""),
                        )
                last_storage_key = str(rows[-1]["storage_key"])
            self._create_recall_index_tables()
            self._create_source_partition_indexes(rebuild=True)
            self._mark_schema_migration(_RECALL_IDENTITY_MIGRATION)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _recall_identity_physical_ready(self) -> bool:
        try:
            columns = {row["name"]: row for row in self.conn.execute("PRAGMA table_info(recall_index)")}
            alias_table = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recall_alias_index'"
            ).fetchone()
            if "title_normalized" not in columns or not alias_table:
                return False
            if not self._recall_index_table_ready():
                return False
            if not self._recall_alias_table_ready():
                return False
            expected = {
                "idx_recall_index_storage_key": ["storage_key"],
                "idx_recall_title_exact": [
                    "tenant_id", "agent_id", "workspace_id", "user_id", "title_normalized",
                    "status", "source_id", "kind", "storage_key",
                ],
                "idx_recall_alias_exact": [
                    "tenant_id", "agent_id", "workspace_id", "user_id", "normalized_alias",
                    "status", "source_id", "kind", "storage_key",
                ],
                "idx_recall_title_exact_kind": [
                    "tenant_id", "agent_id", "workspace_id", "user_id", "title_normalized",
                    "status", "kind", "source_id", "storage_key",
                ],
                "idx_recall_alias_exact_kind": [
                    "tenant_id", "agent_id", "workspace_id", "user_id", "normalized_alias",
                    "status", "kind", "source_id", "storage_key",
                ],
            }
            return all(
                self._identity_index_ready(
                    table="recall_alias_index" if "alias" in index_name else "recall_index",
                    index_name=index_name,
                    expected_columns=expected_columns,
                )
                for index_name, expected_columns in expected.items()
            )
        except sqlite3.OperationalError:
            return False

    def _recall_title_column_ready(self) -> bool:
        columns = {row["name"]: row for row in self.conn.execute("PRAGMA table_info(recall_index)")}
        title_column = columns.get("title_normalized")
        return bool(
            title_column is not None
            and str(title_column["type"]).upper() == "TEXT"
            and int(title_column["notnull"]) == 1
        )

    def _recall_index_table_ready(self) -> bool:
        expected = [
            ("storage_key", "TEXT", 0, 1),
            ("record_id", "TEXT", 1, 0),
            ("kind", "TEXT", 1, 0),
            ("status", "TEXT", 1, 0),
            ("source", "TEXT", 1, 0),
            ("source_id", "TEXT", 1, 0),
            ("tenant_id", "TEXT", 1, 0),
            ("agent_id", "TEXT", 1, 0),
            ("workspace_id", "TEXT", 1, 0),
            ("user_id", "TEXT", 1, 0),
            ("lane", "TEXT", 1, 0),
            ("visibility", "TEXT", 1, 0),
            ("source_class", "TEXT", 1, 0),
            ("memory_type", "TEXT", 1, 0),
            ("projection_type", "TEXT", 1, 0),
            ("quality_score", "REAL", 1, 0),
            ("title_text", "TEXT", 1, 0),
            ("title_normalized", "TEXT", 1, 0),
            ("body_text", "TEXT", 1, 0),
            ("anchor_terms", "TEXT", 1, 0),
            ("updated_at", "TEXT", 1, 0),
        ]
        rows = self.conn.execute("PRAGMA table_info(recall_index)").fetchall()
        actual = [
            (str(row["name"]), str(row["type"]).upper(), int(row["notnull"]), int(row["pk"]))
            for row in rows
        ]
        return actual == expected

    @staticmethod
    def _legacy_source_partition(payload: dict[str, Any], kind: str) -> tuple[str, str]:
        if kind != "knowledge_page":
            return DEFAULT_SOURCE_ID, ""
        candidates: list[object] = []
        for container_name in ("content", "meta", "provenance"):
            container = payload.get(container_name)
            if not isinstance(container, dict):
                continue
            raw_source_ids = container.get("source_ids")
            if isinstance(raw_source_ids, (list, tuple)):
                candidates.extend(raw_source_ids)
            elif raw_source_ids is not None:
                candidates.append(raw_source_ids)
        if not candidates:
            return DEFAULT_SOURCE_ID, ""
        normalized: set[str] = set()
        try:
            for candidate in candidates:
                normalized.add(normalize_source_id(candidate))
        except ValueError:
            return DEFAULT_SOURCE_ID, "invalid"
        if len(normalized) != 1:
            return DEFAULT_SOURCE_ID, "ambiguous"
        return next(iter(normalized)), ""

    def _create_source_partition_indexes(self, *, rebuild: bool = False) -> None:
        if rebuild:
            self.conn.execute("DROP INDEX IF EXISTS idx_records_scope_source_updated")
            self.conn.execute("DROP INDEX IF EXISTS idx_recall_index_scope_source_updated")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_scope_source_updated "
            "ON records(tenant_id, agent_id, workspace_id, user_id, source_id, updated_at DESC, record_id DESC, status, storage_key)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recall_index_scope_source_updated "
            "ON recall_index(tenant_id, agent_id, workspace_id, user_id, source_id, updated_at DESC, quality_score, status, lane, visibility, storage_key)"
        )

    def _source_partition_physical_ready(self) -> bool:
        try:
            record_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(records)")}
            recall_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(recall_index)")}
            indexes = {
                "idx_records_scope_source_updated": [
                    "tenant_id", "agent_id", "workspace_id", "user_id", "source_id",
                    "updated_at", "record_id", "status", "storage_key",
                ],
                "idx_recall_index_scope_source_updated": [
                    "tenant_id", "agent_id", "workspace_id", "user_id", "source_id",
                    "updated_at", "quality_score", "status", "lane", "visibility", "storage_key",
                ],
            }
            if "source_id" not in record_columns or "source_id" not in recall_columns:
                return False
            for index_name, expected_columns in indexes.items():
                columns = [row[2] for row in self.conn.execute(f"PRAGMA index_info({index_name})")]
                if columns != expected_columns:
                    return False
                if "status" not in columns:
                    return False
            return True
        except sqlite3.OperationalError:
            return False

    def upsert(self, record: RecordEnvelope, *, commit: bool = True) -> None:
        if str(record.aliases_version or "") != IDENTITY_ALIASES_VERSION:
            raise ValueError(f"unsupported aliases_version: {record.aliases_version}")
        record.aliases = normalize_record_aliases(
            record.aliases,
            kind=record.kind,
            content=record.content,
        )
        payload = record.to_dict()
        raw_index_parts = []
        if record.kind == "raw_chunk":
            raw_index_parts = [
                record.record_id,
                str(record.content.get("source_event_id", "")),
                str(record.content.get("session_id", "")),
                str(record.content.get("turn_id", "")),
                str(record.content.get("chunk_id", "")),
                str(record.content.get("raw_text_hash", "")),
            ]
        content_text = "\n".join(
            part for part in [
                record.title,
                record.summary,
                record.detail,
                str(record.content.get("text", "")),
                str(record.content.get("excerpt", "")),
                *raw_index_parts,
            ] if part
        )
        embedding = json.dumps(embed_text(content_text), ensure_ascii=False)
        storage_key = self._storage_key(record)
        existing = self.conn.execute(
            "SELECT source_id FROM records WHERE storage_key = ?", (storage_key,)
        ).fetchone()
        if existing is not None and str(existing["source_id"]) != record.source_id:
            raise ValueError("source_id move requires an explicit mutation path")
        idempotency_key = str(record.meta.get("idempotency_key") or "")
        semantic_key = str(record.meta.get("semantic_key") or "")
        self.conn.execute(
            """
            INSERT INTO records (
                storage_key, record_id, kind, status, title, summary, detail, content_text,
                source, source_id, agent_id, workspace_id, user_id, tenant_id,
                embedding_json, idempotency_key, semantic_key, meta_json, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(storage_key) DO UPDATE SET
                kind=excluded.kind,
                status=excluded.status,
                title=excluded.title,
                summary=excluded.summary,
                detail=excluded.detail,
                content_text=excluded.content_text,
                source=excluded.source,
                source_id=excluded.source_id,
                agent_id=excluded.agent_id,
                workspace_id=excluded.workspace_id,
                user_id=excluded.user_id,
                tenant_id=excluded.tenant_id,
                embedding_json=excluded.embedding_json,
                idempotency_key=excluded.idempotency_key,
                semantic_key=excluded.semantic_key,
                meta_json=excluded.meta_json,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                storage_key,
                record.record_id,
                record.kind,
                record.status,
                record.title,
                record.summary,
                record.detail,
                content_text,
                record.source,
                record.source_id,
                record.scope.agent_id,
                record.scope.workspace_id,
                record.scope.user_id,
                record.scope.tenant_id,
                embedding,
                idempotency_key,
                semantic_key,
                json.dumps(record.meta, ensure_ascii=False),
                json.dumps(payload, ensure_ascii=False),
                record.time.created_at,
                record.time.updated_at,
            ),
        )
        self._upsert_recall_index(record=record, storage_key=storage_key, content_text=content_text)
        self._upsert_replay_manifest_evidence(record)
        if commit:
            self.conn.commit()

    def _upsert_replay_manifest_evidence(self, record: RecordEnvelope) -> None:
        content = record.content if isinstance(record.content, dict) else {}
        meta = record.meta if isinstance(record.meta, dict) else {}
        if (
            record.kind != "replay_result"
            or record.source != "eimemory.capability_replay"
            or str(meta.get("report_type") or content.get("report_type") or "")
            != "capability_replay_manifest"
        ):
            return
        sequences = content.get("sequence_by_capability")
        if not isinstance(sequences, dict):
            return
        scope_key = self._replay_scope_key(record.scope)
        for raw_capability, raw_sequence in sequences.items():
            capability = str(raw_capability or "").strip()
            try:
                sequence = int(raw_sequence)
            except (TypeError, ValueError):
                continue
            if not capability or sequence <= 0:
                continue
            self.conn.execute(
                """
                INSERT OR IGNORE INTO replay_manifest_evidence (
                    scope_key, capability, manifest_sequence,
                    manifest_record_id, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    scope_key,
                    capability,
                    sequence,
                    record.record_id,
                    record.time.updated_at,
                ),
            )

    def _upsert_recall_index(self, *, record: RecordEnvelope, storage_key: str, content_text: str) -> None:
        lane, visibility, source_class, memory_type, projection_type, quality_score = self._recall_index_traits(record)
        title_text = str(record.title or "")
        title_normalized = normalize_identity_text(title_text)
        body_text = "\n".join(
            part
            for part in [
                str(record.summary or ""),
                str(record.detail or ""),
                str(record.content.get("text", "") or ""),
                str(record.content.get("excerpt", "") or ""),
            ]
            if part
        )
        if not body_text:
            body_text = content_text
        anchor_terms = " ".join(self._recall_anchor_terms(record=record, content_text=content_text))
        self.conn.execute(
            """
            INSERT INTO recall_index (
                storage_key, record_id, kind, status, source, source_id,
                tenant_id, agent_id, workspace_id, user_id,
                lane, visibility, source_class, memory_type, projection_type,
                quality_score, title_text, title_normalized, body_text, anchor_terms, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(storage_key) DO UPDATE SET
                record_id=excluded.record_id,
                kind=excluded.kind,
                status=excluded.status,
                source=excluded.source,
                source_id=excluded.source_id,
                tenant_id=excluded.tenant_id,
                agent_id=excluded.agent_id,
                workspace_id=excluded.workspace_id,
                user_id=excluded.user_id,
                lane=excluded.lane,
                visibility=excluded.visibility,
                source_class=excluded.source_class,
                memory_type=excluded.memory_type,
                projection_type=excluded.projection_type,
                quality_score=excluded.quality_score,
                title_text=excluded.title_text,
                title_normalized=excluded.title_normalized,
                body_text=excluded.body_text,
                anchor_terms=excluded.anchor_terms,
                updated_at=excluded.updated_at
            """,
            (
                storage_key,
                record.record_id,
                record.kind,
                record.status,
                record.source,
                record.source_id,
                record.scope.tenant_id,
                record.scope.agent_id,
                record.scope.workspace_id,
                record.scope.user_id,
                lane,
                visibility,
                source_class,
                memory_type,
                projection_type,
                quality_score,
                title_text,
                title_normalized,
                body_text,
                anchor_terms,
                record.time.updated_at,
            ),
        )
        self.conn.execute("DELETE FROM recall_alias_index WHERE storage_key = ?", (storage_key,))
        if record.aliases:
            self.conn.executemany(
                "INSERT INTO recall_alias_index ("
                "storage_key, normalized_alias, alias_ordinal, record_id, kind, status, source_id, "
                "tenant_id, agent_id, workspace_id, user_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        storage_key,
                        alias,
                        ordinal,
                        record.record_id,
                        record.kind,
                        record.status,
                        record.source_id,
                        record.scope.tenant_id,
                        record.scope.agent_id,
                        record.scope.workspace_id,
                        record.scope.user_id,
                    )
                    for ordinal, alias in enumerate(record.aliases)
                ],
            )
        if self._has_fts_table():
            self.conn.execute("DELETE FROM recall_index_fts WHERE storage_key = ?", (storage_key,))
            self.conn.execute(
                "INSERT INTO recall_index_fts(storage_key, title_text, body_text, anchor_terms) VALUES (?, ?, ?, ?)",
                (storage_key, title_text, body_text, anchor_terms),
            )

    def _delete_recall_index(self, storage_key: str) -> None:
        self.conn.execute("DELETE FROM recall_index WHERE storage_key = ?", (storage_key,))
        self.conn.execute("DELETE FROM recall_alias_index WHERE storage_key = ?", (storage_key,))
        if self._has_fts_table():
            self.conn.execute("DELETE FROM recall_index_fts WHERE storage_key = ?", (storage_key,))

    def _backfill_recall_index_if_needed(self) -> None:
        try:
            record_count = int(self.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])
            index_count = int(self.conn.execute("SELECT COUNT(*) FROM recall_index").fetchone()[0])
        except sqlite3.OperationalError:
            return
        fts_count = index_count
        if self._has_fts_table():
            try:
                fts_count = int(self.conn.execute("SELECT COUNT(*) FROM recall_index_fts").fetchone()[0])
            except sqlite3.OperationalError:
                fts_count = 0
        if record_count == index_count == fts_count:
            return
        if index_count > record_count:
            self.conn.execute("DELETE FROM recall_index")
            self.conn.execute("DELETE FROM recall_alias_index")
            if self._has_fts_table():
                self.conn.execute("DELETE FROM recall_index_fts")
        cursor = self.conn.execute(
            "SELECT storage_key, payload_json, content_text FROM records ORDER BY storage_key"
        )
        while True:
            rows = cursor.fetchmany(500)
            if not rows:
                break
            for row in rows:
                record = self._record_from_payload_json(row["payload_json"])
                if record is None:
                    continue
                self._upsert_recall_index(
                    record=record,
                    storage_key=str(row["storage_key"]),
                    content_text=str(row["content_text"] or ""),
                )

    def _payload_dict_from_json(self, payload_json: Any) -> dict[str, Any] | None:
        try:
            payload = json.loads(str(payload_json))
        except (TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _record_from_payload_dict(self, payload: dict[str, Any] | None) -> RecordEnvelope | None:
        if not isinstance(payload, dict):
            return None
        try:
            return RecordEnvelope.from_dict(payload)
        except (KeyError, TypeError, ValueError):
            return None

    def _record_from_payload_json(self, payload_json: Any) -> RecordEnvelope | None:
        return self._record_from_payload_dict(self._payload_dict_from_json(payload_json))

    def _has_fts_table(self) -> bool:
        return bool(
            self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='recall_index_fts'"
            ).fetchone()
        )

    def _recall_index_traits(self, record: RecordEnvelope) -> tuple[str, str, str, str, str, float]:
        document = build_recall_index_document(record)
        return (
            document.lane,
            document.visibility,
            document.source_class,
            document.memory_type,
            document.projection_type,
            document.quality_score,
        )

    def _recall_anchor_terms(self, *, record: RecordEnvelope, content_text: str) -> tuple[str, ...]:
        return build_recall_index_document(record).anchor_terms

    def search(
        self,
        *,
        query: str,
        kinds: list[str] | None,
        scope: ScopeRef,
        limit: int,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[RecordEnvelope]:
        records, _ = self.search_with_diagnostics(
            query=query, kinds=kinds, scope=scope, limit=limit, source_ids=source_ids
        )
        return records

    def search_identity_candidates(
        self,
        *,
        query: str,
        kinds: list[str] | None,
        scope: ScopeRef,
        limit: int,
        recall_filters: dict | None = None,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, object]]:
        """Return bounded exact title/alias refs from indexed projections only."""

        normalized_query = normalize_identity_text(query)
        bounded_limit = max(0, min(MAX_QUERY_LIMIT, int(limit)))
        allowed_source_ids = normalize_source_ids(source_ids)
        if not normalized_query or bounded_limit <= 0 or allowed_source_ids == ():
            return []
        filters = self._normalized_recall_filters(recall_filters)
        filters["_exact_scope"] = True
        if allowed_source_ids is not None:
            filters["_source_ids"] = allowed_source_ids
        where, params = self._recall_index_where(
            kinds=kinds,
            scope=scope,
            recall_filters=filters,
            alias="i",
        )
        where.append("i.status = 'active'")
        alias_where = [
            "a.tenant_id = ?",
            "a.agent_id = ?",
            "a.workspace_id = ?",
            "a.user_id = ?",
            "a.normalized_alias = ?",
            "a.status = 'active'",
        ]
        alias_params: list[object] = [
            scope.tenant_id or "default",
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
            normalized_query,
        ]
        if allowed_source_ids is not None:
            alias_where.append(f"a.source_id IN ({','.join('?' for _ in allowed_source_ids)})")
            alias_params.extend(allowed_source_ids)
        if kinds:
            alias_where.append(f"a.kind IN ({','.join('?' for _ in kinds)})")
            alias_params.extend(kinds)
        projection = (
            "i.storage_key, i.record_id, i.kind, i.source_id, i.tenant_id, i.agent_id, "
            "i.workspace_id, i.user_id"
        )
        kind_first = bool(kinds) and allowed_source_ids is None
        title_index_name = "idx_recall_title_exact_kind" if kind_first else "idx_recall_title_exact"
        alias_index_name = "idx_recall_alias_exact_kind" if kind_first else "idx_recall_alias_exact"
        stable_tail = (
            "kind, source_id, storage_key" if kind_first else "source_id, kind, storage_key"
        )
        title_rows = self.conn.execute(
            "SELECT " + projection + f" FROM recall_index i INDEXED BY {title_index_name} WHERE "
            + " AND ".join(where)
            + " AND i.title_normalized = ? "
            + "ORDER BY i.tenant_id, i.agent_id, i.workspace_id, i.user_id, i.title_normalized, "
            + "i.status, i."
            + stable_tail.replace(", ", ", i.")
            + " LIMIT ?",
            [*params, normalized_query, bounded_limit],
        ).fetchall()
        alias_projection = (
            "a.storage_key, a.record_id, a.kind, a.status, a.source_id, a.tenant_id, "
            "a.agent_id, a.workspace_id, a.user_id"
        )
        unverified_alias_rows = self.conn.execute(
            "SELECT " + alias_projection + f" FROM recall_alias_index a INDEXED BY {alias_index_name} WHERE "
            + " AND ".join(alias_where)
            + " ORDER BY a.tenant_id, a.agent_id, a.workspace_id, a.user_id, a.normalized_alias, "
            + "a.status, a."
            + stable_tail.replace(", ", ", a.")
            + " LIMIT ?",
            [*alias_params, bounded_limit],
        ).fetchall()
        alias_rows: list[sqlite3.Row] = []
        if unverified_alias_rows:
            alias_storage_keys = [str(row["storage_key"]) for row in unverified_alias_rows]
            alias_key_placeholders = ",".join("?" for _ in alias_storage_keys)
            authoritative_rows = self.conn.execute(
                "SELECT " + projection
                + " FROM recall_index i INDEXED BY idx_recall_index_storage_key WHERE "
                + " AND ".join(where)
                + f" AND i.storage_key IN ({alias_key_placeholders})",
                [*params, *alias_storage_keys],
            ).fetchall()
            authoritative_by_key = {str(row["storage_key"]): row for row in authoritative_rows}
            for alias_row in unverified_alias_rows:
                authoritative = authoritative_by_key.get(str(alias_row["storage_key"]))
                if authoritative is None or any(
                    str(authoritative[column]) != str(alias_row[column])
                    for column in (
                        "record_id", "kind", "source_id", "tenant_id", "agent_id", "workspace_id", "user_id"
                    )
                ):
                    continue
                alias_rows.append(authoritative)
        evidence_by_key: dict[str, set[str]] = {}
        rows_by_key: dict[str, sqlite3.Row] = {}
        for evidence, rows in (("exact_title", title_rows), ("alias_hit", alias_rows)):
            for row in rows:
                storage_key = str(row["storage_key"])
                rows_by_key[storage_key] = row
                evidence_by_key.setdefault(storage_key, set()).add(evidence)
        return [
            {
                "storage_key": storage_key,
                "record_id": str(rows_by_key[storage_key]["record_id"]),
                "kind": str(rows_by_key[storage_key]["kind"]),
                "source_id": str(rows_by_key[storage_key]["source_id"]),
                "scope": {
                    "tenant_id": str(rows_by_key[storage_key]["tenant_id"]),
                    "agent_id": str(rows_by_key[storage_key]["agent_id"]),
                    "workspace_id": str(rows_by_key[storage_key]["workspace_id"]),
                    "user_id": str(rows_by_key[storage_key]["user_id"]),
                },
                "evidence": sorted(evidence_by_key[storage_key]),
            }
            for storage_key in sorted(rows_by_key)[:bounded_limit]
        ]

    def search_with_diagnostics(
        self,
        *,
        query: str,
        kinds: list[str] | None,
        scope: ScopeRef,
        limit: int,
        recall_filters: dict | None = None,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[list[RecordEnvelope], dict]:
        limit = self._normalize_limit(limit)
        recall_filters = self._normalized_recall_filters(recall_filters)
        allowed_source_ids = normalize_source_ids(source_ids)
        if allowed_source_ids == ():
            return [], {
                "vector_hits": 0,
                "retrieval_mode": "recall_index_hybrid",
                "scored_items": [],
                "candidate_count": 0,
                "candidate_limit": 0,
                "candidate_sources": {},
                "source_ids": [],
            }
        if allowed_source_ids is not None:
            recall_filters = {**recall_filters, "_source_ids": allowed_source_ids}
        rows, candidate_report = self._candidate_rows(
            query=query,
            kinds=kinds,
            scope=scope,
            limit=limit,
            recall_filters=recall_filters,
        )
        query_tokens_for_filter = [token for token in self._clean_text_for_query(query).split() if token]
        query_token_count = max(1, len(query_tokens_for_filter))
        query_ngrams = self._char_ngrams(query.lower())
        query_embedding = embed_text(query)
        scored: list[tuple[float, float, RecordEnvelope, dict]] = []
        vector_hits = 0
        blocked_counts: Counter[str] = Counter()
        for row in rows:
            haystack = str(row["content_text"] or "").lower()
            payload = self._payload_dict_from_json(row["payload_json"])
            record = self._record_from_payload_dict(payload)
            if record is None:
                blocked_counts["corrupt_record"] += 1
                continue
            if not self._record_matches_projection_row(record, row):
                blocked_counts["projection_payload_mismatch"] += 1
                continue
            if bool(recall_filters.get("_exact_scope")) and not self._record_matches_exact_ref(
                record,
                record_id=record.record_id,
                scope=scope,
                source_id=record.source_id,
            ):
                blocked_counts["request_scope_mismatch"] += 1
                continue
            if kinds and record.kind not in kinds:
                blocked_counts["request_kind_mismatch"] += 1
                continue
            if allowed_source_ids is not None and record.source_id not in allowed_source_ids:
                blocked_counts["request_source_mismatch"] += 1
                continue
            if record.status != "active":
                blocked_counts["inactive_record"] += 1
                continue
            lexical_signal = analyze_lexical_signal(
                query,
                haystack,
                record_kind=str(payload.get("kind", "")),
                record_source=str(payload.get("source", "")),
                recall_filters=recall_filters,
            )
            lexical_count = self._lexical_count_for_recall(
                lexical_signal=lexical_signal,
                query_token_count=query_token_count,
            )
            semantic_score = self._jaccard_score(query_ngrams, self._char_ngrams(haystack))
            stored_embedding = self._parse_embedding(row["embedding_json"])
            vector_score = max(0.0, cosine_similarity(query_embedding, stored_embedding))
            if vector_score >= 0.12:
                vector_hits += 1
            blocked_reason = self._record_recall_filter_block_reason(record, recall_filters)
            if blocked_reason:
                blocked_counts[blocked_reason] += 1
                continue
            quality = self._quality_from_record(record)
            if quality.get("capture_decision") == "reject":
                blocked_counts["quality_rejected"] += 1
                continue
            quality_score = float(quality.get("salience_score") or 0.0)
            living_memory = self._living_memory_metadata(record)
            living_adjustments = self._living_score_adjustments(
                living_memory=living_memory,
                query=query,
                recall_filters=recall_filters,
            )
            effective_lexical_count = float(lexical_count)
            if living_adjustments["stale_identity_penalty"] < 0:
                effective_lexical_count = 0.0
            if self._requires_lexical_grounding(recall_filters):
                if not self._has_required_lexical_anchor(lexical_signal):
                    continue
            elif query_tokens_for_filter and lexical_signal.score <= 0 and semantic_score < 0.08 and vector_score < 0.28:
                continue
            source_weight = self._source_weight(record, recall_filters)
            modality_boost = self._preferred_modality_boost(record, recall_filters)
            actionable_adjustment, actionable_reasons = self._actionable_intent_adjustment(
                record=record,
                recall_filters=recall_filters,
            )
            stored_score = extract_memory_score(record.meta) or score_from_legacy_quality(
                record=record,
                activity="quality.repair",
                source="quality.repair",
            )
            recall_score = evaluate_recall_score(
                record=record,
                query=query,
                semantic_score=semantic_score,
                vector_score=vector_score,
                lexical_score=effective_lexical_count,
                source_weight=source_weight,
                modality_boost=modality_boost,
                context=ScoreContext(
                    activity="sqlite.recall",
                    profile=str((recall_filters or {}).get("scoring_profile") or "balanced"),
                    source="sqlite.recall",
                    entity_id=record.record_id,
                    query=query,
                ),
                stored_score=stored_score,
            )
            base_score = recall_score.final_score
            kind_intent_adjustment, kind_intent_penalty = self._kind_intent_adjustment(
                record_kind=str(record.kind or "").strip().lower(),
                recall_filters=recall_filters,
                lexical_signal=lexical_signal,
            )
            lexical_adjustment = self._clamp_lexical_adjustment(
                lexical_signal.score + kind_intent_adjustment
            )
            if living_adjustments["stale_identity_penalty"] < 0 and lexical_adjustment > 0:
                lexical_adjustment = 0.0
            score = self._clamp_score(
                base_score
                + float(living_adjustments["total_adjustment"])
                + lexical_adjustment
                + actionable_adjustment
            )
            scored.append(
                (
                    score,
                    vector_score,
                    record,
                    {
                        "record_id": record.record_id,
                        "kind": record.kind,
                        "title": record.title,
                        "lexical_score": round(float(effective_lexical_count), 4),
                        "raw_lexical_score": round(float(lexical_count), 4),
                        "semantic_score": round(semantic_score, 4),
                        "vector_score": round(vector_score, 4),
                        "quality_score": round(quality_score, 4),
                        "quality": quality,
                        "source_weight": round(source_weight, 4),
                        "modality_boost": round(modality_boost, 4),
                        "lexical_adjustment": round(float(lexical_adjustment), 4),
                        "kind_intent_adjustment": round(float(kind_intent_adjustment), 4),
                        "kind_intent_penalty": kind_intent_penalty,
                        "actionable_intent_adjustment": round(float(actionable_adjustment), 4),
                        "actionable_intent_reasons": list(actionable_reasons),
                        "lexical_signal": lexical_signal.__dict__,
                        "base_final_score": round(base_score, 4),
                        "final_score": round(score, 4),
                        "living_memory": living_memory,
                        "living_score_adjustments": living_adjustments,
                        "scoring_version": recall_score.schema_version,
                        "memory_score": recall_score.to_dict(),
                        "components": recall_score.to_dict()["components"],
                        "labels": list(recall_score.labels),
                        "provenance": recall_score.provenance.to_dict(),
                    },
                )
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        selected_rows = scored[:limit]
        selected = [record for _, _, record, _ in selected_rows]
        return selected, {
            "vector_hits": vector_hits,
            "retrieval_mode": "recall_index_hybrid",
            "scored_items": [score_report for _, _, _, score_report in selected_rows],
            "blocked_counts": dict(blocked_counts),
            "recall_filters": {
                **dict(recall_filters or {}),
                **({"blocked_counts": dict(blocked_counts)} if blocked_counts else {}),
            },
            **candidate_report,
        }

    def _candidate_rows(
        self,
        *,
        query: str,
        kinds: list[str] | None,
        scope: ScopeRef,
        limit: int,
        recall_filters: dict,
    ) -> tuple[list[sqlite3.Row], dict]:
        candidate_limit = self._candidate_limit(limit, recall_filters)
        candidates: dict[str, dict[str, Any]] = {}
        fts_query = self._fts_query(query)
        if fts_query and self._has_fts_table():
            self._collect_fts_candidates(
                candidates,
                fts_query=fts_query,
                kinds=kinds,
                scope=scope,
                limit=candidate_limit,
                recall_filters=recall_filters,
            )
        self._collect_anchor_candidates(
            candidates,
            query=query,
            kinds=kinds,
            scope=scope,
            limit=max(80, candidate_limit // 2),
            recall_filters=recall_filters,
        )
        self._collect_lane_seed_candidates(
            candidates,
            kinds=kinds,
            scope=scope,
            limit=max(40, candidate_limit // 5),
            recall_filters=recall_filters,
        )
        if not candidates:
            self._collect_recent_candidates(
                candidates,
                kinds=kinds,
                scope=scope,
                limit=max(limit, min(candidate_limit, 120)),
                recall_filters=recall_filters,
            )
        if not candidates and self._recall_index_record_count() == 0:
            return self._legacy_candidate_rows(
                kinds=kinds,
                scope=scope,
                limit=candidate_limit,
                recall_filters=recall_filters,
            )

        ordered_keys = [
            key
            for key, _info in sorted(
                candidates.items(),
                key=lambda item: (
                    float(item[1].get("rank") or 9999.0),
                    -float(item[1].get("quality_score") or 0.0),
                    str(item[1].get("updated_at") or ""),
                ),
            )[:candidate_limit]
        ]
        if not ordered_keys:
            return [], {
                "candidate_count": 0,
                "candidate_limit": candidate_limit,
                "candidate_sources": {},
            }
        placeholders = ",".join("?" for _ in ordered_keys)
        rows = self.conn.execute(
            "SELECT r.storage_key, r.record_id, r.kind, r.status, r.tenant_id, r.agent_id, "
            "r.workspace_id, r.user_id, r.source_id, r.payload_json, r.content_text, r.embedding_json "
            "FROM records r JOIN recall_index i ON i.storage_key = r.storage_key "
            "AND i.record_id = r.record_id AND i.kind = r.kind AND i.status = r.status "
            "AND i.tenant_id = r.tenant_id AND i.agent_id = r.agent_id "
            "AND i.workspace_id = r.workspace_id AND i.user_id = r.user_id "
            "AND i.source_id = r.source_id WHERE r.storage_key IN ("
            + placeholders
            + ")",
            ordered_keys,
        ).fetchall()
        by_key = {str(row["storage_key"]): row for row in rows}
        source_counts: dict[str, int] = {}
        for info in candidates.values():
            for source in info.get("sources") or ():
                source_counts[str(source)] = source_counts.get(str(source), 0) + 1
        return [by_key[key] for key in ordered_keys if key in by_key], {
            "candidate_count": len(ordered_keys),
            "candidate_limit": candidate_limit,
            "candidate_sources": source_counts,
        }

    def _legacy_candidate_rows(
        self,
        *,
        kinds: list[str] | None,
        scope: ScopeRef,
        limit: int,
        recall_filters: dict,
    ) -> tuple[list[sqlite3.Row], dict]:
        where = ["1=1"]
        params: list[object] = []
        if kinds:
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if bool(recall_filters.get("_exact_scope")):
            self._apply_exact_scope_filters(where, params, scope)
        else:
            self._apply_scope_filters(where, params, scope)
        source_ids = recall_filters.get("_source_ids")
        if source_ids is not None:
            where.append(f"source_id IN ({','.join('?' for _ in source_ids)})")
            params.extend(source_ids)
        where.append("status != 'rejected'")
        sql = (
            "SELECT storage_key, record_id, kind, status, tenant_id, agent_id, workspace_id, user_id, source_id, "
            "payload_json, content_text, embedding_json FROM records WHERE "
            + " AND ".join(where)
            + " ORDER BY updated_at DESC LIMIT ?"
        )
        rows = self.conn.execute(sql, [*params, max(1, int(limit))]).fetchall()
        return rows, {
            "candidate_count": len(rows),
            "candidate_limit": limit,
            "candidate_sources": {"legacy_scan": len(rows)},
            "candidate_fallback": "legacy_scan",
        }

    def _recall_index_record_count(self) -> int:
        try:
            return int(self.conn.execute("SELECT COUNT(*) FROM recall_index").fetchone()[0])
        except sqlite3.OperationalError:
            return 0

    def _collect_fts_candidates(
        self,
        candidates: dict[str, dict[str, Any]],
        *,
        fts_query: str,
        kinds: list[str] | None,
        scope: ScopeRef,
        limit: int,
        recall_filters: dict,
    ) -> None:
        where, params = self._recall_index_where(kinds=kinds, scope=scope, recall_filters=recall_filters, alias="i")
        sql = (
            "SELECT i.storage_key, i.quality_score, i.updated_at, bm25(recall_index_fts) AS bm25_score "
            "FROM recall_index_fts JOIN recall_index i ON i.storage_key = recall_index_fts.storage_key "
            "WHERE recall_index_fts MATCH ? AND "
            + " AND ".join(where)
            + " ORDER BY bm25_score ASC, i.quality_score DESC, i.updated_at DESC LIMIT ?"
        )
        try:
            rows = self.conn.execute(sql, [fts_query, *params, limit]).fetchall()
        except sqlite3.OperationalError:
            return
        for index, row in enumerate(rows):
            self._add_candidate(
                candidates,
                storage_key=str(row["storage_key"]),
                source="fts",
                rank=10.0 + index,
                quality_score=self._float_value(row["quality_score"]),
                updated_at=str(row["updated_at"] or ""),
            )

    def _collect_anchor_candidates(
        self,
        candidates: dict[str, dict[str, Any]],
        *,
        query: str,
        kinds: list[str] | None,
        scope: ScopeRef,
        limit: int,
        recall_filters: dict,
    ) -> None:
        terms = self._candidate_query_terms(query)
        if not terms:
            return
        where, params = self._recall_index_where(kinds=kinds, scope=scope, recall_filters=recall_filters, alias="i")
        anchor_clauses: list[str] = []
        anchor_params: list[object] = []
        for term in terms[:8]:
            like = f"%{term}%"
            anchor_clauses.append("(i.title_text LIKE ? OR i.anchor_terms LIKE ? OR i.body_text LIKE ?)")
            anchor_params.extend([like, like, like])
        sql = (
            "SELECT i.storage_key, i.quality_score, i.updated_at FROM recall_index i WHERE "
            + " AND ".join(where)
            + " AND ("
            + " OR ".join(anchor_clauses)
            + ") ORDER BY i.quality_score DESC, i.updated_at DESC LIMIT ?"
        )
        rows = self.conn.execute(sql, [*params, *anchor_params, limit]).fetchall()
        for index, row in enumerate(rows):
            self._add_candidate(
                candidates,
                storage_key=str(row["storage_key"]),
                source="anchor",
                rank=30.0 + index,
                quality_score=self._float_value(row["quality_score"]),
                updated_at=str(row["updated_at"] or ""),
            )

    def _collect_lane_seed_candidates(
        self,
        candidates: dict[str, dict[str, Any]],
        *,
        kinds: list[str] | None,
        scope: ScopeRef,
        limit: int,
        recall_filters: dict,
    ) -> None:
        if self._intent_name_from_filters(recall_filters) not in {
            "project_delivery",
            "operator_preference",
            "living_posture",
            "research",
        }:
            return
        where, params = self._recall_index_where(kinds=kinds, scope=scope, recall_filters=recall_filters, alias="i")
        sql = (
            "SELECT i.storage_key, i.quality_score, i.updated_at FROM recall_index i WHERE "
            + " AND ".join(where)
            + " ORDER BY i.quality_score DESC, i.updated_at DESC LIMIT ?"
        )
        rows = self.conn.execute(sql, [*params, limit]).fetchall()
        for index, row in enumerate(rows):
            self._add_candidate(
                candidates,
                storage_key=str(row["storage_key"]),
                source="lane_seed",
                rank=70.0 + index,
                quality_score=self._float_value(row["quality_score"]),
                updated_at=str(row["updated_at"] or ""),
            )

    def _collect_recent_candidates(
        self,
        candidates: dict[str, dict[str, Any]],
        *,
        kinds: list[str] | None,
        scope: ScopeRef,
        limit: int,
        recall_filters: dict,
    ) -> None:
        where, params = self._recall_index_where(kinds=kinds, scope=scope, recall_filters=recall_filters, alias="i")
        sql = (
            "SELECT i.storage_key, i.quality_score, i.updated_at FROM recall_index i WHERE "
            + " AND ".join(where)
            + " ORDER BY i.updated_at DESC LIMIT ?"
        )
        rows = self.conn.execute(sql, [*params, limit]).fetchall()
        for index, row in enumerate(rows):
            self._add_candidate(
                candidates,
                storage_key=str(row["storage_key"]),
                source="recent",
                rank=120.0 + index,
                quality_score=self._float_value(row["quality_score"]),
                updated_at=str(row["updated_at"] or ""),
            )

    def _recall_index_where(
        self,
        *,
        kinds: list[str] | None,
        scope: ScopeRef,
        recall_filters: dict,
        alias: str,
    ) -> tuple[list[str], list[object]]:
        prefix = f"{alias}."
        where = [f"{prefix}status != 'rejected'"]
        params: list[object] = []
        if kinds:
            where.append(f"{prefix}kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if bool(recall_filters.get("_exact_scope")):
            self._apply_exact_scope_filters(where, params, scope, alias=alias)
        else:
            self._apply_recall_index_scope_filters(where, params, scope, alias=alias)
        source_ids = recall_filters.get("_source_ids")
        if source_ids is not None:
            where.append(f"{prefix}source_id IN ({','.join('?' for _ in source_ids)})")
            params.extend(source_ids)
        lanes = self._allowed_recall_lanes(kinds=kinds, recall_filters=recall_filters)
        if lanes:
            where.append(f"{prefix}lane IN ({','.join('?' for _ in lanes)})")
            params.extend(lanes)
        visibilities = self._allowed_recall_visibilities(kinds=kinds, recall_filters=recall_filters)
        if visibilities:
            where.append(f"{prefix}visibility IN ({','.join('?' for _ in visibilities)})")
            params.extend(visibilities)
        return where, params

    def _apply_recall_index_scope_filters(self, where: list[str], params: list[object], scope: ScopeRef, *, alias: str) -> None:
        prefix = f"{alias}."
        scopes = hongtu_query_scopes(scope)
        clauses: list[str] = []
        for item in scopes:
            clause = [
                f"{prefix}tenant_id = ?",
                f"{prefix}agent_id = ?",
                f"{prefix}workspace_id = ?",
            ]
            params.extend([item.tenant_id or "default", item.agent_id, item.workspace_id])
            if item.user_id:
                clause.append(f"({prefix}user_id = ? OR {prefix}user_id = '')")
                params.append(item.user_id)
            else:
                clause.append(f"{prefix}user_id = ''")
            clauses.append("(" + " AND ".join(clause) + ")")
        where.append("(" + " OR ".join(clauses) + ")")

    @staticmethod
    def _apply_exact_scope_filters(
        where: list[str],
        params: list[object],
        scope: ScopeRef,
        *,
        alias: str = "",
    ) -> None:
        prefix = f"{alias}." if alias else ""
        where.extend(
            [
                f"{prefix}tenant_id = ?",
                f"{prefix}agent_id = ?",
                f"{prefix}workspace_id = ?",
                f"{prefix}user_id = ?",
            ]
        )
        params.extend(
            [
                scope.tenant_id or "default",
                scope.agent_id,
                scope.workspace_id,
                scope.user_id,
            ]
        )

    def _allowed_recall_lanes(self, *, kinds: list[str] | None, recall_filters: dict) -> tuple[str, ...]:
        if kinds and set(kinds) <= {"raw_chunk"}:
            return ("raw",)
        if kinds and set(kinds) <= {"reflection", "incident", "replay_result", "feedback", "unknown"}:
            return ("operational",)
        intent_name = self._intent_name_from_filters(recall_filters)
        if intent_name == "research":
            return ("knowledge", "primary")
        if intent_name == "news":
            return ("news", "knowledge", "primary")
        if intent_name == "report":
            return ("operational", "primary", "knowledge")
        if intent_name in {"project_delivery", "operator_preference", "living_posture"}:
            if bool(recall_filters.get("include_evidence_only")):
                return ("primary", "knowledge", "operational")
            return ("primary", "knowledge")
        return ("primary", "knowledge", "news")

    def _allowed_recall_visibilities(self, *, kinds: list[str] | None, recall_filters: dict) -> tuple[str, ...]:
        if kinds and set(kinds) <= {"raw_chunk"}:
            return ("evidence_only", "default")
        if kinds and set(kinds) <= {"reflection", "incident", "replay_result", "feedback", "unknown"}:
            return ("report_only", "evidence_only", "default")
        intent_name = self._intent_name_from_filters(recall_filters)
        if intent_name == "report" or bool(recall_filters.get("include_report_records")):
            return ("default", "report_only", "evidence_only")
        if bool(recall_filters.get("include_evidence_only")):
            return ("default", "evidence_only")
        return ("default",)

    def _add_candidate(
        self,
        candidates: dict[str, dict[str, Any]],
        *,
        storage_key: str,
        source: str,
        rank: float,
        quality_score: float,
        updated_at: str,
    ) -> None:
        entry = candidates.setdefault(
            storage_key,
            {
                "rank": rank,
                "quality_score": quality_score,
                "updated_at": updated_at,
                "sources": [],
            },
        )
        entry["rank"] = min(float(entry.get("rank") or rank), rank)
        entry["quality_score"] = max(float(entry.get("quality_score") or 0.0), quality_score)
        if updated_at > str(entry.get("updated_at") or ""):
            entry["updated_at"] = updated_at
        sources = list(entry.get("sources") or [])
        if source not in sources:
            sources.append(source)
        entry["sources"] = sources

    def _candidate_limit(self, limit: int, recall_filters: dict) -> int:
        raw = recall_filters.get("candidate_limit")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = max(_DEFAULT_CANDIDATE_LIMIT, max(1, int(limit)) * 36)
        return max(max(1, int(limit)), min(_MAX_CANDIDATE_LIMIT, value))

    def _candidate_query_terms(self, query: str) -> tuple[str, ...]:
        raw_terms = self._clean_text_for_query(query).split()
        terms: list[str] = []
        for term in raw_terms:
            normalized = str(term or "").strip().lower()
            if len(normalized) < 2:
                continue
            terms.append(normalized)
            if re.fullmatch(r"[\u4e00-\u9fff]{3,}", normalized):
                terms.extend(normalized[index : index + 2] for index in range(0, len(normalized) - 1, 2))
        seen: set[str] = set()
        result: list[str] = []
        for term in terms:
            if term in seen:
                continue
            seen.add(term)
            result.append(term)
        return tuple(result)

    def _fts_query(self, query: str) -> str:
        terms = [
            term.replace('"', " ").strip()
            for term in self._candidate_query_terms(query)
            if not self._is_weak_version_anchor(term)
        ]
        terms = [term for term in terms if term]
        if not terms:
            return ""
        return " OR ".join(f'"{term}"' for term in terms[:12])

    def get_active_policy(
        self,
        *,
        task_type: str,
        scope: ScopeRef,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        where = [
            "kind = 'rule'",
            "status = 'active'",
        ]
        params: list[object] = []
        self._apply_scope_filters(where, params, scope)
        allowed_source_ids = normalize_source_ids(source_ids)
        if allowed_source_ids == ():
            return {"retrieval_policy": {}, "response_policy": {}}
        if allowed_source_ids is not None:
            where.append(f"source_id IN ({','.join('?' for _ in allowed_source_ids)})")
            params.extend(allowed_source_ids)
        where.append(
            "CAST(COALESCE(json_extract(meta_json, '$.task_type'), "
            "json_extract(meta_json, '$.business_meta.task_type')) AS TEXT) = ?"
        )
        params.append(str(task_type))
        order_by = "updated_at DESC"
        if scope.user_id:
            order_by = "CASE WHEN user_id = ? THEN 1 ELSE 0 END DESC, updated_at DESC"
            params = [*params, scope.user_id]
        row = self.conn.execute(
            "SELECT record_id, kind, status, tenant_id, agent_id, workspace_id, user_id, source_id, payload_json "
            "FROM records WHERE "
            + " AND ".join(where)
            + f" ORDER BY {order_by} LIMIT 1",
            params,
        ).fetchone()
        if row is not None:
            record = self._record_from_payload_json(row["payload_json"])
            if record is not None and self._record_matches_projection_row(record, row):
                return dict(business_metadata(record.meta))
        return {"retrieval_policy": {}, "response_policy": {}}

    def get_by_id(self, record_id: str, *, scope: ScopeRef | None = None) -> RecordEnvelope | None:
        where = ["record_id = ?"]
        params: list[object] = [record_id]
        if scope is not None:
            self._apply_scope_filters(where, params, scope)
        row = self.conn.execute(
            "SELECT payload_json FROM records WHERE "
            + " AND ".join(where)
            + " ORDER BY updated_at DESC LIMIT 1",
            params,
        ).fetchone()
        if not row:
            return None
        return self._record_from_payload_json(row["payload_json"])

    def get_by_exact_ref(
        self,
        record_id: str,
        *,
        scope: ScopeRef,
        source_id: str,
    ) -> RecordEnvelope | None:
        """Hydrate one authoritative record without alias or global-user expansion."""

        normalized_source_id = normalize_source_id(source_id)
        row = self.conn.execute(
            """
            SELECT record_id, kind, status, tenant_id, agent_id, workspace_id, user_id, source_id, payload_json
            FROM records
            WHERE record_id = ?
              AND tenant_id = ?
              AND agent_id = ?
              AND workspace_id = ?
              AND user_id = ?
              AND source_id = ?
            LIMIT 1
            """,
            (
                str(record_id or "").strip(),
                scope.tenant_id or "default",
                scope.agent_id,
                scope.workspace_id,
                scope.user_id,
                normalized_source_id,
            ),
        ).fetchone()
        if row is None:
            return None
        record = self._record_from_payload_json(row["payload_json"])
        if record is None or not self._record_matches_projection_row(record, row):
            return None
        return record

    def list_by_record_id_exact_scope(
        self,
        record_id: str,
        *,
        scope: ScopeRef,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[RecordEnvelope]:
        """List exact-scope matches for direct-ID/report and graph resolution."""

        allowed_source_ids = normalize_source_ids(source_ids)
        if allowed_source_ids == ():
            return []
        where = [
            "record_id = ?",
            "tenant_id = ?",
            "agent_id = ?",
            "workspace_id = ?",
            "user_id = ?",
        ]
        params: list[object] = [
            str(record_id or "").strip(),
            scope.tenant_id or "default",
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
        ]
        if allowed_source_ids is not None:
            where.append(f"source_id IN ({','.join('?' for _ in allowed_source_ids)})")
            params.extend(allowed_source_ids)
        rows = self.conn.execute(
            "SELECT record_id, kind, status, tenant_id, agent_id, workspace_id, user_id, source_id, payload_json "
            "FROM records WHERE " + " AND ".join(where) + " ORDER BY updated_at DESC",
            params,
        ).fetchall()
        records: list[RecordEnvelope] = []
        for row in rows:
            record = self._record_from_payload_json(row["payload_json"])
            if record is None or not self._record_matches_projection_row(record, row):
                continue
            records.append(record)
        return records

    @staticmethod
    def _record_matches_exact_ref(
        record: RecordEnvelope,
        *,
        record_id: str,
        scope: ScopeRef,
        source_id: str,
    ) -> bool:
        return (
            record.record_id == record_id
            and record.scope.tenant_id == (scope.tenant_id or "default")
            and record.scope.agent_id == scope.agent_id
            and record.scope.workspace_id == scope.workspace_id
            and record.scope.user_id == scope.user_id
            and record.source_id == source_id
        )

    @classmethod
    def _record_matches_projection_row(cls, record: RecordEnvelope, row: sqlite3.Row) -> bool:
        return (
            cls._record_matches_exact_ref(
                record,
                record_id=str(row["record_id"] or ""),
                scope=ScopeRef(
                    tenant_id=str(row["tenant_id"] or "default"),
                    agent_id=str(row["agent_id"] or ""),
                    workspace_id=str(row["workspace_id"] or ""),
                    user_id=str(row["user_id"] or ""),
                ),
                source_id=str(row["source_id"] or "default"),
            )
            and record.kind == str(row["kind"] or "")
            and record.status == str(row["status"] or "")
        )

    def get_by_idempotency_key(
        self,
        *,
        kinds: list[str],
        scope: ScopeRef,
        idempotency_key: str,
    ) -> RecordEnvelope | None:
        clean_kinds = [str(kind) for kind in list(kinds or []) if str(kind).strip()]
        key = str(idempotency_key or "").strip()
        if not clean_kinds or not key:
            return None
        placeholders = ",".join("?" for _ in clean_kinds)
        user_clause = "user_id = ?"
        user_params: list[object] = [scope.user_id]
        order_prefix = ""
        if scope.user_id:
            user_clause = "(user_id = ? OR user_id = '')"
            user_params = [scope.user_id]
            order_prefix = "CASE WHEN user_id = ? THEN 1 ELSE 0 END DESC, "
        row = self.conn.execute(
            f"""
            SELECT payload_json
            FROM records
            WHERE kind IN ({placeholders})
              AND tenant_id = ?
              AND agent_id = ?
              AND workspace_id = ?
              AND {user_clause}
              AND idempotency_key = ?
            ORDER BY {order_prefix}updated_at DESC, record_id DESC
            LIMIT 1
            """,
            [
                *clean_kinds,
                scope.tenant_id,
                scope.agent_id,
                scope.workspace_id,
                *user_params,
                key,
                *([scope.user_id] if scope.user_id else []),
            ],
        ).fetchone()
        if not row:
            return None
        return self._record_from_payload_json(row["payload_json"])

    def list_records(
        self,
        *,
        kinds: list[str] | None = None,
        scope: ScopeRef | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        since: str | None = None,
        until: str | None = None,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[RecordEnvelope]:
        limit = self._normalize_limit(limit)
        offset = max(0, int(offset))
        where = ["1=1"]
        params: list[object] = []
        if kinds:
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if status:
            where.append("status = ?")
            params.append(status)
        if scope:
            self._apply_scope_filters(where, params, scope)
        allowed_source_ids = normalize_source_ids(source_ids)
        if allowed_source_ids == ():
            return []
        if allowed_source_ids is not None:
            where.append(f"source_id IN ({','.join('?' for _ in allowed_source_ids)})")
            params.extend(allowed_source_ids)
        since_value = _normalize_datetime_bound(since, end_of_day=False)
        until_value = _normalize_datetime_bound(until, end_of_day=True)
        if since_value:
            where.append("updated_at >= ?")
            params.append(since_value)
        if until_value:
            where.append("updated_at <= ?")
            params.append(until_value)
        rows = self.conn.execute(
            "WITH selected_records AS ("
            "SELECT storage_key, updated_at, record_id FROM records WHERE "
            + " AND ".join(where)
            + " ORDER BY updated_at DESC, record_id DESC LIMIT ? OFFSET ?"
            + ") SELECT selected_records.storage_key, records.payload_json "
            + "FROM selected_records JOIN records USING (storage_key) "
            + "ORDER BY selected_records.updated_at DESC, selected_records.record_id DESC",
            [*params, limit, offset],
        ).fetchall()
        # The CTE keeps large payload_json values out of the unbounded scope
        # sort while preserving SQLite's single-statement read snapshot.  The
        # outer sort handles at most MAX_QUERY_LIMIT joined payloads.
        return [
            record
            for row in rows
            if (record := self._record_from_payload_json(row["payload_json"])) is not None
        ]

    def latest_record_by_meta_value_exact_scope(
        self,
        *,
        kind: str,
        source: str,
        status: str,
        scope: ScopeRef,
        meta_key: str,
        meta_value: object,
    ) -> RecordEnvelope | None:
        """Return insertion high-water evidence without trusting wall clocks."""

        if not str(meta_key or "").replace("_", "").isalnum():
            raise ValueError("meta_key must be a simple identifier")
        row = self.conn.execute(
            "SELECT payload_json FROM records WHERE kind=? AND source=? AND status=? "
            "AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? "
            f"AND CAST(json_extract(meta_json, '$.{meta_key}') AS TEXT)=? "
            "ORDER BY rowid DESC LIMIT 1",
            (
                str(kind), str(source), str(status), scope.tenant_id, scope.agent_id,
                scope.workspace_id, scope.user_id, str(meta_value),
            ),
        ).fetchone()
        return None if row is None else self._record_from_payload_json(row["payload_json"])

    def count_records(
        self,
        *,
        kinds: list[str] | None = None,
        scope: ScopeRef | None = None,
        status: str | None = None,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> int:
        where = ["1=1"]
        params: list[object] = []
        if kinds:
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if status:
            where.append("status = ?")
            params.append(status)
        if scope is not None:
            self._apply_scope_filters(where, params, scope)
        allowed_source_ids = normalize_source_ids(source_ids)
        if allowed_source_ids == ():
            return 0
        if allowed_source_ids is not None:
            where.append(f"source_id IN ({','.join('?' for _ in allowed_source_ids)})")
            params.extend(allowed_source_ids)
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM records WHERE " + " AND ".join(where),
                params,
            ).fetchone()[0]
        )

    def count_records_exact_scope(
        self,
        *,
        kinds: list[str] | None = None,
        scope: ScopeRef,
        status: str | None = None,
        statuses: list[str] | set[str] | tuple[str, ...] | None = None,
        since: str | None = None,
        until: str | None = None,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> int:
        """Count one canonical scope without decoding or alias-expanding payloads.

        SQLite errors intentionally propagate; L5 readiness catches them and
        treats the unavailable evidence count as zero (fail closed).
        """

        where = [
            "tenant_id = ?",
            "agent_id = ?",
            "workspace_id = ?",
            "user_id = ?",
        ]
        params: list[object] = [
            scope.tenant_id or "default",
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
        ]
        if kinds:
            clean_kinds = [str(kind).strip() for kind in kinds if str(kind).strip()]
            if not clean_kinds:
                return 0
            where.append(f"kind IN ({','.join('?' for _ in clean_kinds)})")
            params.extend(clean_kinds)
        clean_statuses = [str(value).strip() for value in (statuses or []) if str(value).strip()]
        if str(status or "").strip():
            clean_statuses.append(str(status).strip())
        clean_statuses = list(dict.fromkeys(clean_statuses))
        if clean_statuses:
            where.append(f"status IN ({','.join('?' for _ in clean_statuses)})")
            params.extend(clean_statuses)
        allowed_source_ids = normalize_source_ids(source_ids)
        if allowed_source_ids == ():
            return 0
        if allowed_source_ids is not None:
            where.append(f"source_id IN ({','.join('?' for _ in allowed_source_ids)})")
            params.extend(allowed_source_ids)
        since_value = _normalize_datetime_bound(since, end_of_day=False)
        until_value = _normalize_datetime_bound(until, end_of_day=True)
        if since_value:
            where.append("updated_at >= ?")
            params.append(since_value)
        if until_value:
            where.append("updated_at <= ?")
            params.append(until_value)
        row = self.conn.execute(
            "SELECT COUNT(*) FROM records WHERE " + " AND ".join(where),
            params,
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def list_capability_scores_compact(
        self,
        *,
        scope: ScopeRef,
        limit: int = 500,
        since: str | None = None,
        until: str | None = None,
    ) -> list[RecordEnvelope]:
        """Return the ledger fields without materializing stored evidence_items."""

        bounded = self._normalize_limit(limit)
        where = [
            "kind = 'capability_score'",
            "tenant_id = ?",
            "agent_id = ?",
            "workspace_id = ?",
            "user_id = ?",
        ]
        params: list[object] = [
            scope.tenant_id or "default",
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
        ]
        since_value = _normalize_datetime_bound(since, end_of_day=False)
        until_value = _normalize_datetime_bound(until, end_of_day=True)
        if since_value:
            where.append("updated_at >= ?")
            params.append(since_value)
        if until_value:
            where.append("updated_at <= ?")
            params.append(until_value)
        # The key-first CTE relies on records.storage_key remaining the table's
        # primary key, so the outer join cannot multiply the bounded key page.
        rows = self.conn.execute(
            "WITH selected_records AS ("
            "SELECT storage_key, updated_at, record_id FROM records WHERE "
            + " AND ".join(where)
            + " ORDER BY updated_at DESC, record_id DESC LIMIT ?"
            + ") SELECT r.record_id, r.status, r.title, r.summary, r.source, "
            + "r.tenant_id, r.agent_id, r.workspace_id, r.user_id, "
            + "r.meta_json, r.created_at, r.updated_at, "
            + "json_extract(r.payload_json, '$.content.capability') AS content_capability, "
            + "json_extract(r.payload_json, '$.content.score') AS content_score, "
            + "json_extract(r.payload_json, '$.content.score_sequence') AS content_score_sequence, "
            + "json_extract(r.payload_json, '$.content.regression_count') AS content_regression_count, "
            + "json_extract(r.payload_json, '$.content.evidence_record_ids') AS evidence_record_ids_json, "
            + "json_extract(r.payload_json, '$.content.evidence_tiers') AS evidence_tiers_json, "
            + "json_extract(r.payload_json, '$.content.evidence_sources') AS evidence_sources_json "
            + "FROM selected_records JOIN records AS r USING (storage_key) "
            + "ORDER BY selected_records.updated_at DESC, selected_records.record_id DESC",
            [*params, bounded],
        ).fetchall()
        records: list[RecordEnvelope] = []
        for row in rows:
            meta = self._payload_dict_from_json(row["meta_json"]) or {}
            content = {
                "capability": meta.get("capability") or row["content_capability"] or "",
                "score": meta.get("score") if meta.get("score") is not None else row["content_score"],
                "score_sequence": (
                    meta.get("score_sequence")
                    if meta.get("score_sequence") is not None
                    else row["content_score_sequence"]
                ),
                "regression_count": (
                    meta.get("regression_count")
                    if meta.get("regression_count") is not None
                    else row["content_regression_count"]
                ),
                "evidence_record_ids": _json_text_list(row["evidence_record_ids_json"]),
                "evidence_tiers": _json_text_list(row["evidence_tiers_json"]),
                "evidence_sources": _json_text_list(row["evidence_sources_json"]),
            }
            records.append(
                RecordEnvelope(
                    record_id=str(row["record_id"]),
                    kind="capability_score",
                    status=str(row["status"]),
                    title=str(row["title"]),
                    summary=str(row["summary"]),
                    detail="",
                    content=content,
                    tags=[],
                    links=[],
                    evidence=[],
                    source=str(row["source"]),
                    scope=ScopeRef(
                        tenant_id=str(row["tenant_id"] or "default"),
                        agent_id=str(row["agent_id"] or ""),
                        workspace_id=str(row["workspace_id"] or ""),
                        user_id=str(row["user_id"] or ""),
                    ),
                    time=TimeRef(
                        created_at=str(row["created_at"]),
                        updated_at=str(row["updated_at"]),
                        occurred_at=str(row["created_at"]),
                    ),
                    provenance={},
                    meta=meta,
                )
            )
        return records

    def count_records_by_meta_value(
        self,
        *,
        kinds: list[str] | None = None,
        scope: ScopeRef | None = None,
        meta_key: str,
        meta_value: Any,
        status: str | None = None,
    ) -> int | None:
        expression = _meta_json_text_expression(meta_key)
        if not expression:
            return None
        where = ["1=1", f"{expression} = ?"]
        params: list[object] = [str(meta_value)]
        if kinds:
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if status:
            where.append("status = ?")
            params.append(status)
        if scope:
            self._apply_scope_filters(where, params, scope)
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM records WHERE " + " AND ".join(where),
                params,
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return int(row[0]) if row is not None else 0

    def list_records_by_meta_value(
        self,
        *,
        kinds: list[str] | None = None,
        scope: ScopeRef | None = None,
        meta_key: str,
        meta_value: Any,
        status: str | None = None,
        limit: int = 100,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[RecordEnvelope] | None:
        expression = _meta_json_text_expression(meta_key)
        if not expression:
            return None
        limit = self._normalize_limit(limit)
        where = ["1=1", f"{expression} = ?"]
        params: list[object] = [str(meta_value)]
        if kinds:
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if status:
            where.append("status = ?")
            params.append(status)
        if scope:
            self._apply_scope_filters(where, params, scope)
        allowed_source_ids = normalize_source_ids(source_ids)
        if allowed_source_ids == ():
            return []
        if allowed_source_ids is not None:
            where.append(f"source_id IN ({','.join('?' for _ in allowed_source_ids)})")
            params.extend(allowed_source_ids)
        try:
            # The key-first CTE relies on records.storage_key remaining the
            # primary key, so the payload join preserves the bounded row count.
            rows = self.conn.execute(
                "WITH selected_records AS ("
                "SELECT storage_key, updated_at, record_id FROM records WHERE "
                + " AND ".join(where)
                + " ORDER BY updated_at DESC, record_id DESC LIMIT ?"
                + ") SELECT selected_records.storage_key, records.payload_json "
                + "FROM selected_records JOIN records USING (storage_key) "
                + "ORDER BY selected_records.updated_at DESC, selected_records.record_id DESC",
                [*params, limit],
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        return [
            record
            for row in rows
            if (record := self._record_from_payload_json(row["payload_json"])) is not None
        ]

    def upsert_memory_edge(self, edge: MemoryEdge, *, commit: bool = True) -> MemoryEdge:
        self.upsert_memory_edges([edge], commit=commit)
        return edge

    def upsert_memory_edges(
        self,
        edges: list[MemoryEdge],
        *,
        commit: bool = True,
    ) -> list[MemoryEdge]:
        clean_edges = []
        for edge in edges:
            if edge.edge_type not in MEMORY_EDGE_TYPES:
                raise ValueError(f"invalid memory edge type: {edge.edge_type}")
            clean_edges.append(edge)
        if not clean_edges:
            return []
        self.conn.executemany(
            """
            INSERT INTO memory_edges (
                edge_id, from_id, to_id, edge_type, confidence, evidence_id,
                tenant_id, agent_id, workspace_id, user_id,
                reason, meta_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(edge_id) DO UPDATE SET
                confidence=excluded.confidence,
                evidence_id=excluded.evidence_id,
                reason=excluded.reason,
                meta_json=excluded.meta_json,
                updated_at=excluded.updated_at
            """,
            [self._memory_edge_params(edge) for edge in clean_edges],
        )
        if commit:
            self.conn.commit()
        return clean_edges

    def _memory_edge_params(self, edge: MemoryEdge) -> tuple[Any, ...]:
        if edge.edge_type not in MEMORY_EDGE_TYPES:
            raise ValueError(f"invalid memory edge type: {edge.edge_type}")
        return (
            edge.edge_id,
            edge.from_id,
            edge.to_id,
            edge.edge_type,
            edge.confidence,
            edge.evidence_id,
            edge.scope.tenant_id,
            edge.scope.agent_id,
            edge.scope.workspace_id,
            edge.scope.user_id,
            edge.reason,
            json.dumps(edge.meta or {}, ensure_ascii=False, sort_keys=True),
            edge.created_at,
            edge.updated_at,
        )

    def list_memory_edges(
        self,
        *,
        scope: ScopeRef | None = None,
        edge_types: list[str] | None = None,
        record_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[MemoryEdge]:
        where = ["1=1"]
        params: list[object] = []
        if scope is not None:
            self._apply_scope_filters(where, params, scope)
        clean_types = [str(item) for item in list(edge_types or []) if str(item) in MEMORY_EDGE_TYPES]
        if clean_types:
            where.append(f"edge_type IN ({','.join('?' for _ in clean_types)})")
            params.extend(clean_types)
        clean_ids = [str(item) for item in list(record_ids or []) if str(item)]
        if clean_ids:
            placeholders = ",".join("?" for _ in clean_ids)
            where.append(f"(from_id IN ({placeholders}) OR to_id IN ({placeholders}))")
            params.extend(clean_ids)
            params.extend(clean_ids)
        rows = self.conn.execute(
            "SELECT * FROM memory_edges WHERE "
            + " AND ".join(where)
            + " ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            [*params, self._normalize_limit(limit)],
        ).fetchall()
        return [self._memory_edge_from_row(row) for row in rows]

    def _memory_edge_from_row(self, row: sqlite3.Row) -> MemoryEdge:
        try:
            meta = json.loads(str(row["meta_json"] or "{}"))
        except json.JSONDecodeError:
            meta = {}
        return MemoryEdge.from_dict(
            {
                "edge_id": row["edge_id"],
                "from_id": row["from_id"],
                "to_id": row["to_id"],
                "edge_type": row["edge_type"],
                "confidence": row["confidence"],
                "evidence_id": row["evidence_id"],
                "scope": {
                    "tenant_id": row["tenant_id"],
                    "agent_id": row["agent_id"],
                    "workspace_id": row["workspace_id"],
                    "user_id": row["user_id"],
                },
                "reason": row["reason"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "meta": meta if isinstance(meta, dict) else {},
            }
        )

    def _apply_scope_filters(self, where: list[str], params: list[object], scope: ScopeRef) -> None:
        scopes = hongtu_query_scopes(scope)
        clauses: list[str] = []
        for item in scopes:
            clause = [
                "tenant_id = ?",
                "agent_id = ?",
                "workspace_id = ?",
            ]
            params.extend([item.tenant_id or "default", item.agent_id, item.workspace_id])
            if item.user_id:
                clause.append("(user_id = ? OR user_id = '')")
                params.append(item.user_id)
            else:
                clause.append("user_id = ''")
            clauses.append("(" + " AND ".join(clause) + ")")
        where.append("(" + " OR ".join(clauses) + ")")

    def rewrite(
        self,
        record: RecordEnvelope,
        *,
        previous_scope: ScopeRef | None = None,
        commit: bool = True,
    ) -> None:
        previous_key = None
        if previous_scope is not None:
            previous_key = self._storage_key_from_values(
                record_id=record.record_id,
                tenant_id=previous_scope.tenant_id,
                agent_id=previous_scope.agent_id,
                workspace_id=previous_scope.workspace_id,
                user_id=previous_scope.user_id,
            )
        for key in {self._storage_key(record), previous_key} - {None}:
            existing = self.conn.execute("SELECT source_id FROM records WHERE storage_key = ?", (key,)).fetchone()
            if existing is not None and str(existing["source_id"]) != record.source_id:
                raise ValueError("source_id move requires an explicit mutation path")
        new_key = self._storage_key(record)
        if previous_key and previous_key != new_key:
            try:
                self.conn.execute("DELETE FROM records WHERE storage_key = ?", (previous_key,))
                self._delete_recall_index(previous_key)
                self.upsert(record, commit=False)
                if commit:
                    self.conn.commit()
            except Exception:
                if commit:
                    self.conn.rollback()
                raise
            return
        self.upsert(record, commit=commit)

    def _storage_key(self, record: RecordEnvelope) -> str:
        return self._storage_key_from_values(
            record_id=record.record_id,
            tenant_id=record.scope.tenant_id,
            agent_id=record.scope.agent_id,
            workspace_id=record.scope.workspace_id,
            user_id=record.scope.user_id,
        )

    def _storage_key_from_values(
        self,
        *,
        record_id: str,
        tenant_id: str,
        agent_id: str,
        workspace_id: str,
        user_id: str,
    ) -> str:
        return "\x1f".join([tenant_id or "default", agent_id, workspace_id, user_id, record_id])

    def _normalize_limit(self, limit: int) -> int:
        try:
            value = int(limit)
        except (TypeError, ValueError):
            value = 0
        return max(0, min(MAX_QUERY_LIMIT, value))

    def _char_ngrams(self, text: str, size: int = 3) -> set[str]:
        normalized = "".join(ch for ch in text.lower() if not ch.isspace())
        if not normalized:
            return set()
        if len(normalized) <= size:
            return {normalized}
        return {normalized[idx: idx + size] for idx in range(len(normalized) - size + 1)}

    def _jaccard_score(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        intersection = len(left & right)
        union = len(left | right)
        if union == 0:
            return 0.0
        return intersection / union

    def _parse_embedding(self, payload: str) -> list[float]:
        try:
            parsed = json.loads(payload or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [float(item) for item in parsed]

    def _living_memory_metadata(self, record: RecordEnvelope) -> dict[str, Any]:
        meta = record.meta if isinstance(record.meta, dict) else {}
        living = meta.get("living_memory_v1")
        if not isinstance(living, dict):
            living = business_metadata(meta).get("living_memory_v1")
        if not isinstance(living, dict):
            return {}
        return {
            key: dict(value)
            for key, value in living.items()
            if key in {"temporal", "motive", "affective", "action_posture"} and isinstance(value, dict)
        }

    def _living_score_adjustments(
        self,
        *,
        living_memory: dict[str, Any],
        query: str,
        recall_filters: dict | None,
    ) -> dict[str, float]:
        adjustments = {
            "motive_match_boost": 0.0,
            "affective_salience_boost": 0.0,
            "temporal_boost": 0.0,
            "stale_identity_penalty": 0.0,
            "total_adjustment": 0.0,
        }
        if not living_memory:
            return adjustments

        motive = living_memory.get("motive") if isinstance(living_memory.get("motive"), dict) else {}
        affective = living_memory.get("affective") if isinstance(living_memory.get("affective"), dict) else {}
        temporal = living_memory.get("temporal") if isinstance(living_memory.get("temporal"), dict) else {}

        query_text, query_terms = self._living_query_text_and_terms(query, recall_filters)
        matched_motive_labels = [
            label for label in self._living_label_strings(motive)
            if self._living_label_matches(label, query_text, query_terms)
        ]
        if matched_motive_labels:
            adjustments["motive_match_boost"] = min(0.08, 0.04 + (0.02 * min(2, len(matched_motive_labels))))

        pressure = self._living_pressure_score(affective.get("pressure"))
        affective_boost = min(0.04, max(0.0, pressure) * 0.04)
        if bool(affective.get("frustration_repeat")):
            affective_boost += 0.025
        if bool(affective.get("trust_building")):
            affective_boost += 0.02
        if bool(affective.get("repair_needed")):
            affective_boost += 0.05
        adjustments["affective_salience_boost"] = min(0.1, affective_boost)

        temporal_status = str(temporal.get("status") or temporal.get("state") or "").strip().lower().replace("_", "-")
        if temporal_status in {"active", "current", "future-intent", "future", "planned", "ongoing"}:
            adjustments["temporal_boost"] = 0.03
        temporal_distance = str(temporal.get("temporal_distance") or "").strip().lower().replace("_", "-")
        if temporal_distance == "future":
            adjustments["temporal_boost"] = max(adjustments["temporal_boost"], 0.03)

        stale_penalty = 0.0
        if bool(temporal.get("superseded")) or temporal_status in {"superseded", "expired", "stale"} or temporal_distance == "stale":
            stale_penalty -= 0.08
        if self._living_valid_until_is_past(temporal.get("valid_until")):
            stale_penalty -= 0.08
        adjustments["stale_identity_penalty"] = max(-0.14, stale_penalty)

        positive = min(
            0.16,
            adjustments["motive_match_boost"]
            + adjustments["affective_salience_boost"]
            + adjustments["temporal_boost"],
        )
        total = positive + adjustments["stale_identity_penalty"]
        adjustments["total_adjustment"] = round(max(-0.18, min(0.16, total)), 4)
        return {key: round(value, 4) for key, value in adjustments.items()}

    def _living_query_text_and_terms(self, query: str, recall_filters: dict | None) -> tuple[str, set[str]]:
        parts = [str(query or "")]
        filters = dict(recall_filters or {})
        for key in ("living_task_context_terms", "living_query_terms", "task_context_terms"):
            value = filters.get(key)
            if isinstance(value, list):
                parts.extend(str(item) for item in value if str(item).strip())
            elif isinstance(value, str):
                parts.append(value)
        text = " ".join(parts).lower()
        return text, set(self._normalized_terms(text))

    def _living_label_strings(self, value: Any) -> list[str]:
        labels: list[str] = []
        if isinstance(value, dict):
            for nested in value.values():
                labels.extend(self._living_label_strings(nested))
        elif isinstance(value, list):
            for nested in value:
                labels.extend(self._living_label_strings(nested))
        elif isinstance(value, str) and value.strip():
            labels.append(value.strip())
        return labels

    def _living_label_matches(self, label: str, query_text: str, query_terms: set[str]) -> bool:
        normalized_label = str(label or "").strip().lower()
        if not normalized_label:
            return False
        if normalized_label in query_text:
            return True
        label_terms = set(self._normalized_terms(self._living_label_search_text(normalized_label)))
        return bool(label_terms and label_terms & query_terms)

    def _living_valid_until_is_past(self, value: Any) -> bool:
        if not value:
            return False
        text = str(value).strip()
        if not text:
            return False
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed < datetime.now(timezone.utc)

    @staticmethod
    def _living_pressure_score(value: Any) -> float:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"elevated", "high", "urgent", "pressure"}:
                return 0.8
            if normalized in {"normal", "medium"}:
                return 0.35
            if normalized in {"low", "none"}:
                return 0.0
        return SqliteRecordStore._float_value(value)

    @staticmethod
    def _float_value(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _living_label_search_text(value: str) -> str:
        return str(value or "").replace("_", " ").replace("-", " ")

    @staticmethod
    def _normalized_terms(text: str) -> list[str]:
        return [term.lower() for term in re.findall(r"[\w]+", text, flags=re.UNICODE) if term.strip()]

    @staticmethod
    def _clamp_score(value: float) -> float:
        return round(max(0.0, min(1.0, value)), 4)

    @staticmethod
    def _clamp_lexical_adjustment(value: float) -> float:
        return round(max(-_MAX_LEXICAL_ADJUSTMENT, min(_MAX_LEXICAL_ADJUSTMENT, value)), 4)

    @staticmethod
    def _clean_text_for_query(query: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(query or "").lower(), flags=re.UNICODE).strip()

    @staticmethod
    def _cleaned_record_terms(text: str) -> list[str]:
        return [term for term in re.findall(r"[\w\u4e00-\u9fff]+", str(text or "").lower(), flags=re.UNICODE) if term.strip()]

    @staticmethod
    def _lexical_count_for_recall(
        *,
        lexical_signal: Any,
        query_token_count: int,
    ) -> float:
        query_token_count = max(1, int(query_token_count))
        weighted_hits = len(tuple(lexical_signal.token_hits))
        weighted_hits += 0.5 * len(lexical_signal.version_hits)
        weighted_hits += 0.4 * len(lexical_signal.entity_hits)
        weighted_hits += 0.5 * len(lexical_signal.exact_phrase_hits)
        return min(float(query_token_count), weighted_hits)

    @staticmethod
    def _intent_name_from_filters(filters: dict) -> str:
        return str(filters.get("intent_name") or filters.get("intent") or "").strip().lower()

    @classmethod
    def _requires_lexical_grounding(cls, recall_filters: dict | None) -> bool:
        return cls._intent_name_from_filters(dict(recall_filters or {})) in {
            "project_delivery",
            "operator_preference",
            "living_posture",
        }

    @classmethod
    def _has_required_lexical_anchor(cls, lexical_signal: Any) -> bool:
        for hit_group in (
            getattr(lexical_signal, "token_hits", ()),
            getattr(lexical_signal, "entity_hits", ()),
            getattr(lexical_signal, "exact_phrase_hits", ()),
        ):
            for hit in hit_group:
                normalized = str(hit or "").strip().lower()
                if normalized and not cls._is_weak_version_anchor(normalized):
                    return True
        return False

    @staticmethod
    def _is_weak_version_anchor(term: str) -> bool:
        return bool(re.fullmatch(r"(?:v\d+(?:\.\d+)?|\d+(?:\.\d+)?)", str(term or "").strip().lower()))

    @classmethod
    def _actionable_intent_adjustment(
        cls,
        *,
        record: RecordEnvelope,
        recall_filters: dict | None,
    ) -> tuple[float, tuple[str, ...]]:
        intent_name = cls._intent_name_from_filters(dict(recall_filters or {}))
        if intent_name not in {"project_delivery", "operator_preference", "living_posture"}:
            return 0.0, ()
        text = cls._record_actionable_text(record)
        if not text:
            return 0.0, ()
        adjustment = 0.0
        reasons: list[str] = []
        if cls._looks_like_serialized_tool_call(text):
            adjustment -= 0.1
            reasons.append("serialized_tool_call")
        if intent_name in {"project_delivery", "living_posture"} and cls._looks_like_actionable_memory(text):
            adjustment += 0.08
            reasons.append("actionable_preference")
        memory_type = str(
            business_metadata(record.meta).get("memory_type")
            or record.content.get("memory_type")
            or ""
        ).strip().lower()
        if memory_type in {"preference", "rule", "policy"}:
            adjustment += 0.04
            reasons.append(f"memory_type:{memory_type}")
        return max(-0.12, min(0.12, adjustment)), tuple(reasons)

    @staticmethod
    def _record_actionable_text(record: RecordEnvelope) -> str:
        return "\n".join(
            str(part or "")
            for part in [
                record.title,
                record.summary,
                record.detail,
                record.content.get("text", ""),
                record.content.get("excerpt", ""),
            ]
            if part
        ).strip()

    @staticmethod
    def _looks_like_serialized_tool_call(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or "").lower())
        return (
            compact.startswith('{"type":"toolcall"')
            or ('"type":"toolcall"' in compact and '"arguments"' in compact)
            or ('"name":"message"' in compact and '"arguments"' in compact and '"input"' in compact)
        )

    @staticmethod
    def _looks_like_actionable_memory(text: str) -> bool:
        value = str(text or "").lower()
        if re.search(r"以后.+先.+再", value):
            return True
        actionable_terms = (
            "长期记忆",
            "以后",
            "先对",
            "逐条验收",
            "验收清单",
            "交付要求",
            "硬规则",
            "偏好",
            "不要",
            "必须",
            "优先",
        )
        return sum(1 for term in actionable_terms if term in value) >= 2

    @classmethod
    def _normalized_recall_filters(cls, recall_filters: dict | None) -> dict:
        filters: dict = dict(recall_filters or {})
        filters["intent_name"] = cls._intent_name_from_filters(filters)
        filters["preferred_kinds"] = cls._as_tuple(filters.get("preferred_kinds") or filters.get("allowed_kinds") or ())
        filters["suppressed_kinds"] = cls._as_tuple(filters.get("suppressed_kinds") or filters.get("blocked_kinds") or ())
        filters["kind_weights"] = dict(filters.get("kind_weights") or {})
        if not filters["kind_weights"]:
            filters["kind_weights"] = {}
        if "memory_cube" not in filters:
            filters["memory_cube"] = str(filters.get("memory_cube") or "").strip()
        return filters

    def _kind_intent_adjustment(
        self,
        *,
        record_kind: str,
        recall_filters: dict | None,
        lexical_signal,
    ) -> tuple[float, str]:
        filters = dict(recall_filters or {})
        intent_name = self._intent_name_from_filters(filters)
        if not intent_name or intent_name == "research":
            return 0.0, ""
        suppressed_kinds = set(filters.get("suppressed_kinds") or ())
        preferred_kinds = set(filters.get("preferred_kinds") or ())
        kind_weights = dict(filters.get("kind_weights") or {})

        kind_adjustment = 0.0
        reason = ""
        if record_kind in suppressed_kinds:
            kind_adjustment -= 0.08
            reason = f"intent:{intent_name} suppresses kind:{record_kind}"
        elif record_kind not in preferred_kinds and preferred_kinds:
            kind_adjustment -= 0.02
            reason = f"intent:{intent_name} deprioritizes kind:{record_kind}"

        if kind_weights:
            raw_weight_text = kind_weights.get(record_kind, 1.0)
            try:
                raw_weight = float(raw_weight_text)
            except (TypeError, ValueError):
                raw_weight = 1.0
            if raw_weight > 1.0:
                kind_adjustment += min(0.06, (raw_weight - 1.0) * 0.06)
            elif raw_weight < 1.0:
                kind_adjustment -= min(0.06, (1.0 - raw_weight) * 0.06)
                if not reason:
                    reason = f"kind_weight:{record_kind}={raw_weight:.2f}"

        if record_kind == "knowledge_page" and intent_name in {"project_delivery", "operator_preference", "living_posture"}:
            kind_adjustment = min(0.0, kind_adjustment - 0.04)
            if not reason:
                reason = f"kind_intent:{intent_name} downweights knowledge_page"

        if lexical_signal.suppression_reason:
            reason = lexical_signal.suppression_reason if not reason else f"{reason}; {lexical_signal.suppression_reason}"
        return self._clamp_lexical_adjustment(kind_adjustment), reason

    @staticmethod
    def _as_tuple(value: object) -> tuple[str, ...]:
        if not value:
            return ()
        if isinstance(value, str):
            stripped = value.strip()
            return (stripped,) if stripped else ()
        if isinstance(value, (tuple, list, set)):
            return tuple(str(item).strip() for item in value if str(item).strip())
        return (str(value).strip(),) if str(value).strip() else ()

    def _record_matches_recall_filters(self, record: RecordEnvelope, recall_filters: dict | None) -> bool:
        return not bool(self._record_recall_filter_block_reason(record, recall_filters))

    def _record_recall_filter_block_reason(self, record: RecordEnvelope, recall_filters: dict | None) -> str:
        filters = dict(recall_filters or {})
        if not filters:
            return ""
        labels = self._record_filter_labels(record)
        blocked_projection_types = set(self._as_tuple(filters.get("blocked_projection_types") or ()))
        if blocked_projection_types:
            projection_type = str(
                business_metadata(record.meta).get("projection_type")
                or record.provenance.get("projection_type")
                or record.content.get("projection_type")
                or ""
            ).strip()
            if projection_type in blocked_projection_types:
                return f"projection:{projection_type or 'blocked'}"
        blocked_kinds = set(self._as_tuple(filters.get("blocked_kinds") or ()))
        if blocked_kinds and record.kind in blocked_kinds:
            return f"kind:{record.kind}"
        allowed_kinds = set(self._as_tuple(filters.get("allowed_kinds") or ()))
        if allowed_kinds and record.kind not in allowed_kinds:
            return "kind:not_allowed"
        blocked_sources = set(filters.get("blocked_sources") or [])
        if blocked_sources and labels["sources"] & blocked_sources:
            return "source:blocked"
        allowed_sources = set(filters.get("allowed_sources") or [])
        if allowed_sources and not labels["sources"] & allowed_sources:
            return "source:not_allowed"
        allowed_memory_types = set(filters.get("allowed_memory_types") or [])
        if allowed_memory_types and record.kind == "memory" and labels["memory_types"] and not labels["memory_types"] & allowed_memory_types:
            return "memory_type:not_allowed"
        organs = set(filters.get("organs") or [])
        if organs and labels["organs"] and not labels["organs"] & organs:
            return "organ:not_allowed"
        blocked_recall_lanes = set(filters.get("blocked_recall_lanes") or [])
        if blocked_recall_lanes and labels["recall_lanes"] & blocked_recall_lanes:
            return sorted(labels["recall_lanes"] & blocked_recall_lanes)[0]
        allowed_recall_lanes = set(filters.get("allowed_recall_lanes") or [])
        if allowed_recall_lanes and not labels["recall_lanes"] & allowed_recall_lanes:
            return "recall_lane:not_allowed"
        return ""

    def _source_weight(self, record: RecordEnvelope, recall_filters: dict | None) -> float:
        weights = dict((recall_filters or {}).get("source_weights") or {})
        if not weights:
            return 1.0
        labels = self._record_filter_labels(record)["sources"]
        matches = [float(weight) for source, weight in weights.items() if source in labels]
        return max(matches) if matches else 1.0

    def _preferred_modality_boost(self, record: RecordEnvelope, recall_filters: dict | None) -> float:
        preferred = set((recall_filters or {}).get("preferred_modalities") or [])
        if not preferred:
            return 0.0
        labels = self._record_filter_labels(record)
        return 0.18 if labels["modalities"] & preferred else 0.0

    def _record_filter_labels(self, record: RecordEnvelope) -> dict[str, set[str]]:
        meta = business_metadata(record.meta)
        content = record.content if isinstance(record.content, dict) else {}
        sources = {str(record.source or "").strip()}
        for key in ("source", "source_channel", "communication_channel"):
            value = meta.get(key) or content.get(key)
            if value:
                sources.add(str(value).strip())
        memory_types = set()
        for key in ("memory_type",):
            value = meta.get(key) or content.get(key)
            if value:
                memory_types.add(str(value).strip())
        organs = set()
        for key in ("organ",):
            value = meta.get(key) or content.get(key)
            if value:
                organs.add(str(value).strip())
        modalities = set()
        for key in ("modality",):
            value = meta.get(key) or content.get(key)
            if value:
                modalities.add(str(value).strip())
        return {
            "sources": {item for item in sources if item},
            "memory_types": {item for item in memory_types if item},
            "organs": {item for item in organs if item},
            "modalities": {item for item in modalities if item},
            "recall_lanes": {self._record_recall_lane(record)},
        }

    def _record_recall_lane(self, record: RecordEnvelope) -> str:
        labels_meta = business_metadata(record.meta)
        content = record.content if isinstance(record.content, dict) else {}
        memory_type = str(labels_meta.get("memory_type") or content.get("memory_type") or "").strip().lower()
        if memory_type in _RECALL_LANE_MEMORY_TYPE_ALIASES:
            return _RECALL_LANE_MEMORY_TYPE_ALIASES[memory_type]
        if record.kind == "rule":
            return "system_rule"
        if record.kind == "reflection":
            return self._reflection_recall_lane(record)
        if record.kind in {"recall_view", "feedback"}:
            return "audit_record"
        if record.kind == "incident":
            return "incident_report"
        if record.kind in {"replay_result", "learning_eval", "capability_candidate", "promotion_request", "skill_candidate"}:
            return "evolution_artifact"
        if record.kind in {"knowledge_page", "claim_card", "paper_source", "paper_extract", "knowledge_unit"}:
            return "external_knowledge"
        if record.kind == "memory":
            return "durable_fact"
        return str(record.kind or "")

    @staticmethod
    def _reflection_recall_lane(record: RecordEnvelope) -> str:
        meta = business_metadata(record.meta)
        content = record.content if isinstance(record.content, dict) else {}
        report_type = str(meta.get("report_type") or record.provenance.get("report_type") or content.get("report_type") or "").strip().lower()
        haystack = " ".join([report_type, str(record.source or ""), str(record.title or "")]).lower()
        if any(marker in haystack for marker in ("audit", "before_prompt_build", "injection")):
            return "audit_record"
        if "incident" in haystack:
            return "incident_report"
        if "outcome_trace" in haystack or "run_log" in haystack:
            return "run_log"
        if report_type:
            return "evolution_artifact"
        return "audit_record"

    def _quality_from_record(self, record: RecordEnvelope) -> dict:
        quality = business_metadata(record.meta).get("quality") if isinstance(record.meta, dict) else {}
        if not isinstance(quality, dict):
            return {}
        return dict(quality)

    def record_event(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopeRef | dict | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        scope_ref = normalize_scope(scope)
        data = ensure_event_payload(payload, scope_ref)
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO events (
                id, timestamp, source, user_phrase, event_type, interpreted_intent,
                goal, confidence, tenant_id, agent_id, workspace_id, user_id,
                payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                timestamp=excluded.timestamp,
                source=excluded.source,
                user_phrase=excluded.user_phrase,
                event_type=excluded.event_type,
                interpreted_intent=excluded.interpreted_intent,
                goal=excluded.goal,
                confidence=excluded.confidence,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                data["id"],
                data["timestamp"],
                data["source"],
                data["user_phrase"],
                data["event_type"],
                data["interpreted_intent"],
                data["goal"],
                float(data["confidence"]),
                scope_ref.tenant_id,
                scope_ref.agent_id,
                scope_ref.workspace_id,
                scope_ref.user_id,
                json.dumps(data, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        if commit:
            self.conn.commit()
        return data

    def record_outcome(
        self,
        event_id: str,
        payload: dict[str, Any],
        *,
        scope: ScopeRef | dict | None = None,
        commit: bool = True,
        apply_rollbacks: bool = True,
    ) -> dict[str, Any]:
        scope_ref = normalize_scope(scope)
        data = ensure_outcome_payload(event_id, payload)
        pattern_ids = extract_pattern_ids_from_outcome(data)
        self.conn.execute(
            """
            INSERT INTO event_outcomes (
                id, event_id, outcome, reason, correction_from_user, policy_update,
                tenant_id, agent_id, workspace_id, user_id, payload_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                outcome=excluded.outcome,
                reason=excluded.reason,
                correction_from_user=excluded.correction_from_user,
                policy_update=excluded.policy_update,
                payload_json=excluded.payload_json,
                recorded_at=excluded.recorded_at
            """,
            (
                data["id"],
                data["event_id"],
                data["outcome"],
                str(data.get("reason") or ""),
                str(data.get("correction_from_user") or ""),
                str(data.get("policy_update") or ""),
                scope_ref.tenant_id,
                scope_ref.agent_id,
                scope_ref.workspace_id,
                scope_ref.user_id,
                json.dumps(data, ensure_ascii=False, sort_keys=True),
                data["recorded_at"],
            ),
        )
        rollback_report = {}
        if apply_rollbacks and str(data.get("outcome") or "").lower() == "bad" and pattern_ids:
            rollback_report = self._apply_pattern_rollback_if_needed(
                event_id=event_id,
                pattern_ids=pattern_ids,
                outcome_payload=data,
                scope_ref=scope_ref,
            )
        if commit:
            self.conn.commit()
        if rollback_report:
            return {**data, **rollback_report}
        return data

    def get_policy_rollout_ledger(
        self,
        *,
        scope: ScopeRef | dict | None = None,
        action: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        scope_ref = normalize_scope(scope)
        max_limit = max(0, min(200, int(limit)))
        where = [
            "tenant_id = ?",
            "agent_id = ?",
            "workspace_id = ?",
            "user_id = ?",
        ]
        params: list[Any] = [scope_ref.tenant_id, scope_ref.agent_id, scope_ref.workspace_id, scope_ref.user_id]
        if action:
            where.append("action_type = ?")
            params.append(str(action))
        rows = self.conn.execute(
            "SELECT * FROM policy_rollout_ledger WHERE "
            + " AND ".join(where)
            + " ORDER BY created_at DESC, id DESC LIMIT ?",
            [*params, max_limit],
        ).fetchall()
        return [
            {
                "id": row["id"],
                "promotion_id": row["promotion_id"],
                "action_type": row["action_type"],
                "is_auto": bool(row["is_auto"]),
                "record_date": row["record_date"],
                "scope": {
                    "tenant_id": row["tenant_id"],
                    "agent_id": row["agent_id"],
                    "workspace_id": row["workspace_id"],
                    "user_id": row["user_id"],
                },
                "source_opportunity_id": row["source_opportunity_id"],
                "source_opportunity": json.loads(str(row["source_opportunity_json"])),
                "trust_report": json.loads(str(row["trust_report_json"])),
                "replay_report": json.loads(str(row["replay_report_json"])),
                "applied_pattern_id": row["applied_pattern_id"],
                "budget_decision": row["budget_decision"],
                "rollback_policy_id": row["rollback_policy_id"],
                "reason": row["reason"],
                "details": json.loads(str(row["details_json"])),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def upsert_policy_rollout_ledger_payload(self, ledger: dict[str, Any], *, commit: bool = True) -> dict[str, Any]:
        payload = dict(ledger or {})
        scope = normalize_scope(payload.get("scope"))
        created_at = str(payload.get("created_at") or now_utc())
        record_date = str(payload.get("record_date") or created_at[:10])
        ledger_id = str(payload.get("id") or next_rollout_id(kind="policy-rollout-ledger", scope=scope, payload=payload))
        self.conn.execute(
            """
            INSERT INTO policy_rollout_ledger (
                id, tenant_id, agent_id, workspace_id, user_id, record_date,
                action_type, promotion_id, is_auto, source_opportunity_id,
                source_opportunity_json, trust_report_json, replay_report_json,
                applied_pattern_id, budget_decision, rollback_policy_id, reason,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                record_date=excluded.record_date,
                action_type=excluded.action_type,
                promotion_id=excluded.promotion_id,
                is_auto=excluded.is_auto,
                source_opportunity_id=excluded.source_opportunity_id,
                source_opportunity_json=excluded.source_opportunity_json,
                trust_report_json=excluded.trust_report_json,
                replay_report_json=excluded.replay_report_json,
                applied_pattern_id=excluded.applied_pattern_id,
                budget_decision=excluded.budget_decision,
                rollback_policy_id=excluded.rollback_policy_id,
                reason=excluded.reason,
                details_json=excluded.details_json,
                created_at=excluded.created_at
            """,
            (
                ledger_id,
                scope.tenant_id,
                scope.agent_id,
                scope.workspace_id,
                scope.user_id,
                record_date,
                str(payload.get("action_type") or ""),
                str(payload.get("promotion_id") or ""),
                1 if payload.get("is_auto") else 0,
                str(payload.get("source_opportunity_id") or ""),
                json.dumps(dict(payload.get("source_opportunity") or {}), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(payload.get("trust_report") or {}), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(payload.get("replay_report") or {}), ensure_ascii=False, sort_keys=True),
                str(payload.get("applied_pattern_id") or ""),
                str(payload.get("budget_decision") or ""),
                str(payload.get("rollback_policy_id") or ""),
                str(payload.get("reason") or ""),
                json.dumps(dict(payload.get("details") or {}), ensure_ascii=False, sort_keys=True),
                created_at,
            ),
        )
        if commit:
            self.conn.commit()
        payload["id"] = ledger_id
        payload["scope"] = {
            "tenant_id": scope.tenant_id,
            "agent_id": scope.agent_id,
            "workspace_id": scope.workspace_id,
            "user_id": scope.user_id,
        }
        payload["created_at"] = created_at
        payload["record_date"] = record_date
        return payload

    def _record_policy_rollout_ledger(
        self,
        *,
        action_type: str,
        scope: ScopeRef,
        promotion_id: str,
        source_opportunity_id: str,
        source_opportunity: dict[str, Any],
        trust_report: dict[str, Any],
        replay_report: dict[str, Any],
        is_auto: bool,
        applied_pattern_id: str,
        budget_decision: str,
        rollback_policy_id: str = "",
        reason: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        created_at = now_utc()
        ledger = build_rollout_ledger_record(
            promotion_id=promotion_id,
            source_opportunity=source_opportunity,
            trust_gate_report=trust_report,
            replay_gate_report=replay_report,
            applied_pattern_id=applied_pattern_id,
            budget_decision=budget_decision,
            rollback_policy_id=rollback_policy_id,
            action=action_type,
            scope=scope,
            is_auto=bool(is_auto),
            reason=reason,
            details=details or {},
        )
        ledger_id = next_rollout_id(
            kind="policy-rollout-ledger",
            scope=scope,
            payload={
                "action_type": action_type,
                "promotion_id": promotion_id,
                "applied_pattern_id": applied_pattern_id,
                "rollback_policy_id": rollback_policy_id,
                "created_at": created_at,
            },
        )
        self.conn.execute(
            """
            INSERT INTO policy_rollout_ledger (
                id, tenant_id, agent_id, workspace_id, user_id, record_date,
                action_type, promotion_id, is_auto, source_opportunity_id,
                source_opportunity_json, trust_report_json, replay_report_json,
                applied_pattern_id, budget_decision, rollback_policy_id, reason,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ledger_id,
                scope.tenant_id,
                scope.agent_id,
                scope.workspace_id,
                scope.user_id,
                created_at[:10],
                str(action_type),
                str(promotion_id),
                1 if is_auto else 0,
                str(source_opportunity_id or ""),
                json.dumps(ledger["source_opportunity"], ensure_ascii=False, sort_keys=True),
                json.dumps(ledger["trust_report"], ensure_ascii=False, sort_keys=True),
                json.dumps(ledger["replay_report"], ensure_ascii=False, sort_keys=True),
                str(applied_pattern_id or ""),
                str(budget_decision or ""),
                str(rollback_policy_id or ""),
                str(reason or ""),
                json.dumps(ledger["details"], ensure_ascii=False, sort_keys=True),
                created_at,
            ),
        )
        ledger["id"] = ledger_id
        ledger["source_opportunity_id"] = str(source_opportunity_id or "")
        ledger["created_at"] = created_at
        ledger["record_date"] = created_at[:10]
        if not self.suppress_auxiliary_logging:
            self.enqueue_export(
                stream="policy_rollout_ledger",
                payload={
                    "log_type": "policy_rollout_ledger",
                    "scope": {
                        "tenant_id": scope.tenant_id,
                        "agent_id": scope.agent_id,
                        "workspace_id": scope.workspace_id,
                        "user_id": scope.user_id,
                    },
                    "payload": ledger,
                },
                commit=False,
            )
        return ledger

    def _pattern_row_for_scope(self, pattern_id: str, scope_ref: ScopeRef) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT *
            FROM intent_patterns
            WHERE id = ?
              AND tenant_id = ?
              AND (agent_id = ? OR agent_id = '')
              AND (workspace_id = ? OR workspace_id = '')
              AND (user_id = ? OR user_id = '')
            ORDER BY
              CASE WHEN agent_id = ? THEN 0 ELSE 1 END,
              CASE WHEN workspace_id = ? THEN 0 ELSE 1 END,
              CASE WHEN user_id = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (
                str(pattern_id),
                scope_ref.tenant_id,
                scope_ref.agent_id,
                scope_ref.workspace_id,
                scope_ref.user_id,
                scope_ref.agent_id,
                scope_ref.workspace_id,
                scope_ref.user_id,
            ),
        ).fetchone()

    def _bad_outcome_count_for_pattern(self, *, pattern_id: str, scope_ref: ScopeRef) -> int:
        rows = self.conn.execute(
            """
            SELECT o.event_id, o.payload_json AS outcome_payload, e.payload_json AS event_payload
            FROM event_outcomes o
            LEFT JOIN events e
              ON e.id = o.event_id
             AND e.tenant_id = o.tenant_id
             AND e.agent_id = o.agent_id
             AND e.workspace_id = o.workspace_id
             AND e.user_id = o.user_id
            WHERE o.outcome = 'bad'
              AND o.tenant_id = ?
              AND o.agent_id = ?
              AND o.workspace_id = ?
              AND o.user_id = ?
            ORDER BY o.recorded_at DESC
            LIMIT 200
            """,
            (scope_ref.tenant_id, scope_ref.agent_id, scope_ref.workspace_id, scope_ref.user_id),
        ).fetchall()
        outcome_groups: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(str(row["outcome_payload"]))
            except json.JSONDecodeError:
                continue
            if str(pattern_id) in extract_pattern_ids_from_outcome(payload):
                try:
                    event_payload = json.loads(str(row["event_payload"])) if row["event_payload"] else {}
                except json.JSONDecodeError:
                    event_payload = {}
                outcome_groups.add(
                    self._bad_outcome_group_key(
                        event_id=str(row["event_id"] or ""),
                        event_payload=event_payload,
                    )
                )
        return len(outcome_groups)

    @staticmethod
    def _bad_outcome_group_key(*, event_id: str, event_payload: dict[str, Any]) -> str:
        session_id = str(event_payload.get("session_id") or "").strip()
        if session_id:
            task_id = str(
                event_payload.get("task_id")
                or event_payload.get("taskId")
                or event_payload.get("turn_id")
                or ""
            ).strip()
            task_anchor = task_id or str(event_payload.get("user_phrase") or "").strip()
            event_type = str(event_payload.get("event_type") or "").strip()
            return f"session:{session_id}:{task_anchor}:{event_type}"
        return f"event:{event_id}"

    def _rollback_pattern(
        self,
        *,
        pattern_id: str,
        scope_ref: ScopeRef,
        reason: str,
        event_id: str = "",
        auto: bool,
    ) -> dict[str, Any]:
        row = self._pattern_row_for_scope(pattern_id, scope_ref)
        if row is None:
            return {"ok": False, "error": "pattern_not_found", "pattern_id": str(pattern_id)}

        payload = json.loads(str(row["payload_json"]))
        previous_status = str(row["status"] or payload.get("status") or "active")
        budget_decision = budget_decision_for_rollback(
            conn=self.conn,
            scope=scope_ref,
            auto=bool(auto),
            budget_limit=AUTO_ROLLBACK_BUDGET_PER_DAY,
        )
        if budget_decision not in {"ok", "manual_ok"}:
            ledger = self._record_policy_rollout_ledger(
                action_type="rollback",
                scope=scope_ref,
                promotion_id=next_rollout_id(
                    kind="policy-rollback",
                    scope=scope_ref,
                    payload={"pattern_id": str(pattern_id), "event_id": str(event_id), "blocked": True},
                ),
                source_opportunity_id=str(event_id or ""),
                source_opportunity={"event_id": str(event_id or ""), "pattern_id": str(pattern_id)},
                trust_report={},
                replay_report={},
                is_auto=bool(auto),
                applied_pattern_id="",
                budget_decision=budget_decision,
                rollback_policy_id=str(pattern_id),
                reason=str(reason or "rollback blocked by budget"),
                details={"previous_status": previous_status, "blocked": True},
            )
            return {
                "ok": False,
                "error": "rollback_blocked",
                "pattern_id": str(pattern_id),
                "budget_decision": budget_decision,
                "ledger_id": ledger["id"],
            }

        payload["status"] = "rolled_back"
        payload["last_rollback_reason"] = str(reason or "")
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            UPDATE intent_patterns
            SET status = ?, payload_json = ?, last_rollback_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                "rolled_back",
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                str(reason or ""),
                now,
                str(pattern_id),
            ),
        )
        follow_ups = follow_up_opportunities_from_rollback(
            pattern_id=str(pattern_id),
            event_id=str(event_id or ""),
            reason=str(reason or ""),
            source="auto" if auto else "manual",
            scope=scope_ref,
        )
        rollback_execution = {
            "ok": True,
            "skipped": False,
            "execution_type": "intent_pattern_status_transition",
            "pattern_id": str(pattern_id),
            "status_transition": {
                "from": previous_status,
                "to": "rolled_back",
                "pattern_id": str(pattern_id),
            },
        }
        ledger = self._record_policy_rollout_ledger(
            action_type="rollback",
            scope=scope_ref,
            promotion_id=next_rollout_id(
                kind="policy-rollback",
                scope=scope_ref,
                payload={"pattern_id": str(pattern_id), "event_id": str(event_id or "")},
            ),
            source_opportunity_id=str(event_id or ""),
            source_opportunity={"event_id": str(event_id or ""), "pattern_id": str(pattern_id)},
            trust_report={},
            replay_report={},
            is_auto=bool(auto),
            applied_pattern_id=str(pattern_id),
            budget_decision=budget_decision,
            rollback_policy_id=str(pattern_id),
            reason=str(reason or ""),
            details={
                "previous_status": previous_status,
                "follow_up_opportunities": follow_ups,
                "rollback": rollback_execution,
            },
        )
        return {
            "ok": True,
            "pattern_id": str(pattern_id),
            "previous_status": previous_status,
            "status": "rolled_back",
            "budget_decision": budget_decision,
            "ledger_id": ledger["id"],
            "follow_up_opportunities": follow_ups,
        }

    def _apply_pattern_rollback_if_needed(
        self,
        *,
        event_id: str,
        pattern_ids: list[str],
        outcome_payload: dict[str, Any],
        scope_ref: ScopeRef,
    ) -> dict[str, Any]:
        rolled_back: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        correction = str(outcome_payload.get("correction_from_user") or outcome_payload.get("reason") or "").strip()
        for pattern_id in pattern_ids:
            row = self._pattern_row_for_scope(pattern_id, scope_ref)
            if row is None:
                skipped.append({"pattern_id": str(pattern_id), "reason": "pattern_not_found"})
                continue
            status = str(row["status"] or "active")
            if status in {"rolled_back", "quarantined"}:
                skipped.append({"pattern_id": str(pattern_id), "reason": f"status:{status}"})
                continue
            bad_count = self._bad_outcome_count_for_pattern(pattern_id=pattern_id, scope_ref=scope_ref)
            immediate = outcome_triggers_immediate_rollback(outcome_payload)
            repeated = should_auto_rollback_from_repeated_bad_outcomes(bad_outcome_count=bad_count)
            if not (immediate or repeated):
                skipped.append(
                    {
                        "pattern_id": str(pattern_id),
                        "reason": "below_rollback_threshold",
                        "bad_outcome_count": bad_count,
                    }
                )
                continue
            reason = correction or str(outcome_payload.get("policy_update") or "bad outcome attributed to policy")
            if repeated and not immediate:
                reason = f"repeated bad outcomes ({bad_count}): {reason}"
            result = self._rollback_pattern(
                pattern_id=str(pattern_id),
                scope_ref=scope_ref,
                reason=reason,
                event_id=str(event_id),
                auto=True,
            )
            if result.get("ok"):
                rolled_back.append(result)
            else:
                blocked.append(result)
        return {
            "rollback": {
                "triggered": bool(rolled_back or blocked),
                "rolled_back_pattern_ids": [str(item.get("pattern_id")) for item in rolled_back],
                "blocked_pattern_ids": [str(item.get("pattern_id")) for item in blocked],
                "skipped": skipped,
                "details": rolled_back + blocked,
            }
        }

    def rollback_intent_pattern(
        self,
        pattern_id: str,
        *,
        scope: ScopeRef | dict | None = None,
        reason: str = "",
        auto: bool = False,
        commit: bool = True,
    ) -> dict[str, Any]:
        scope_ref = normalize_scope(scope)
        result = self._rollback_pattern(
            pattern_id=str(pattern_id),
            scope_ref=scope_ref,
            reason=str(reason or "manual rollback"),
            auto=bool(auto),
        )
        if commit:
            self.conn.commit()
        return result

    def upsert_intent_pattern(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopeRef | dict | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        scope_ref = normalize_scope(scope)
        data = ensure_pattern_payload(payload, scope_ref)
        now = datetime.now(timezone.utc).isoformat()

        source_opportunity_id = str(data.get("source_opportunity_id") or "").strip()
        is_auto = bool(data.get("is_auto", bool(source_opportunity_id)))
        budget_decision = "manual_ok"
        trust_report = dict(data.get("trust_report") or {})
        replay_report = dict(data.get("replay_report") or {})
        source_opportunity = dict(data.get("source_opportunity") or {})

        promotion_id = next_rollout_id(
            kind="policy-promotion",
            scope=scope_ref,
            payload={"pattern_id": data["id"], "source_opportunity_id": source_opportunity_id},
        )
        if source_opportunity and source_opportunity_id:
            budget_decision = budget_decision_for_promotion(
                conn=self.conn,
                scope=scope_ref,
                auto=bool(is_auto),
                budget_limit=AUTO_PROMOTION_BUDGET_PER_DAY,
            )
            budget_allowed = budget_decision in {"ok", "manual_ok"}
            if not budget_allowed:
                data["status"] = "candidate"

            self._record_policy_rollout_ledger(
                action_type="promotion",
                scope=scope_ref,
                promotion_id=promotion_id,
                source_opportunity_id=source_opportunity_id,
                source_opportunity=source_opportunity,
                trust_report=trust_report,
                replay_report=replay_report,
                is_auto=is_auto,
                applied_pattern_id=str(data["id"]) if budget_allowed else "",
                budget_decision=budget_decision,
                reason=str(data.get("promotion_blocked_reason") or ""),
                details=dict(data.get("promotion_details") or {}),
            )

        self.conn.execute(
            """
            INSERT INTO intent_patterns (
                id, pattern, default_event_type, interpreted_intent, confidence, status,
                tenant_id, agent_id, workspace_id, user_id, payload_json, last_rollback_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                pattern=excluded.pattern,
                default_event_type=excluded.default_event_type,
                interpreted_intent=excluded.interpreted_intent,
                confidence=excluded.confidence,
                status=excluded.status,
                payload_json=excluded.payload_json,
                last_rollback_reason=excluded.last_rollback_reason,
                updated_at=excluded.updated_at
            """,
            (
                data["id"],
                data["pattern"],
                data["default_event_type"],
                str(data.get("interpreted_intent") or ""),
                float(data["confidence"]),
                str(data.get("status") or "active"),
                scope_ref.tenant_id,
                scope_ref.agent_id,
                scope_ref.workspace_id,
                scope_ref.user_id,
                json.dumps(data, ensure_ascii=False, sort_keys=True),
                str(data.get("last_rollback_reason") or ""),
                now,
                now,
            ),
        )
        if commit:
            self.conn.commit()
        data["_promotion_id"] = promotion_id
        data["_promotion_budget_decision"] = budget_decision
        data["_promotion_source_opportunity_id"] = source_opportunity_id
        data["_promotion_is_auto"] = bool(is_auto)
        return data

    def search_policy(
        self,
        user_phrase: str,
        *,
        scope: ScopeRef | dict | None = None,
        context: dict[str, Any] | None = None,
        limit: int = 5,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        scope_ref = normalize_scope(scope)
        allowed_source_ids = normalize_source_ids(source_ids)
        if allowed_source_ids == () or (
            allowed_source_ids is not None and DEFAULT_SOURCE_ID not in allowed_source_ids
        ):
            return {
                "ok": True,
                "query": str(user_phrase or ""),
                "scope": {
                    "tenant_id": scope_ref.tenant_id,
                    "agent_id": scope_ref.agent_id,
                    "workspace_id": scope_ref.workspace_id,
                    "user_id": scope_ref.user_id,
                },
                "matched_event_type": "",
                "policy_suggestions": [],
            }
        max_limit = max(1, min(20, int(limit or 5)))
        context_payload = dict(context or {})
        status_values = ["active"]
        if bool(context_payload.get("include_shadow")):
            status_values.append("shadow")
        status_placeholders = ",".join("?" for _ in status_values)
        pattern_cursor = self.conn.execute(
            f"""
            SELECT payload_json, default_event_type, confidence, updated_at, status
            FROM intent_patterns
            WHERE tenant_id = ?
              AND status IN ({status_placeholders})
              AND (agent_id = ? OR agent_id = '')
              AND (workspace_id = ? OR workspace_id = '')
              AND (user_id = ? OR user_id = '')
            ORDER BY
              CASE WHEN agent_id = ? THEN 0 ELSE 1 END,
              CASE WHEN workspace_id = ? THEN 0 ELSE 1 END,
              CASE WHEN user_id = ? THEN 0 ELSE 1 END,
              confidence DESC,
              updated_at DESC
            """,
            (
                scope_ref.tenant_id,
                *status_values,
                scope_ref.agent_id,
                scope_ref.workspace_id,
                scope_ref.user_id,
                scope_ref.agent_id,
                scope_ref.workspace_id,
                scope_ref.user_id,
            ),
        )
        suggestions: list[dict[str, Any]] = []
        matched_event_type = str(context_payload.get("event_type") or "")
        while len(suggestions) < max_limit:
            pattern_rows = pattern_cursor.fetchmany(200)
            if not pattern_rows:
                break
            for row in pattern_rows:
                pattern = json.loads(str(row["payload_json"]))
                matched = pattern_matches(str(pattern.get("pattern") or ""), user_phrase)
                if not matched:
                    continue
                if not matched_event_type:
                    matched_event_type = str(pattern.get("default_event_type") or "")
                suggestions.append(
                    {
                        "source": "intent_pattern",
                        "id": pattern.get("id"),
                        "pattern": pattern.get("pattern"),
                        "event_type": pattern.get("default_event_type"),
                        "interpreted_intent": pattern.get("interpreted_intent"),
                        "first_questions": list(pattern.get("first_questions") or []),
                        "execution_policy": list(pattern.get("execution_policy") or []),
                        "ask_first_boundaries": list(pattern.get("ask_first_boundaries") or []),
                        "success_criteria": str(pattern.get("success_criteria") or ""),
                        "status": str(pattern.get("status") or "active"),
                        "score": round(0.55 + float(pattern.get("confidence") or 0.0) * 0.25, 3),
                    }
                )
                if len(suggestions) >= max_limit:
                    break
        event_rows = self.conn.execute(
            """
            SELECT e.payload_json AS event_payload, e.timestamp, e.confidence,
                   o.payload_json AS outcome_payload, o.outcome, o.recorded_at
            FROM events e
            LEFT JOIN event_outcomes o ON o.event_id = e.id
            WHERE e.tenant_id = ?
              AND (e.agent_id = ? OR e.agent_id = '')
              AND (e.workspace_id = ? OR e.workspace_id = '')
              AND (e.user_id = ? OR e.user_id = '')
            ORDER BY e.timestamp DESC
            LIMIT 200
            """,
            (scope_ref.tenant_id, scope_ref.agent_id, scope_ref.workspace_id, scope_ref.user_id),
        ).fetchall()
        for row in event_rows:
            event = json.loads(str(row["event_payload"]))
            similarity = event_similarity(event, user_phrase, matched_event_type)
            if similarity <= 0.0:
                continue
            outcome = json.loads(str(row["outcome_payload"])) if row["outcome_payload"] else {}
            outcome_name = str(outcome.get("outcome") or "")
            has_correction = bool(outcome.get("correction_from_user") or outcome.get("policy_update"))
            good = outcome_name == "good"
            bad_without_fix = outcome_name == "bad" and not has_correction
            score = (
                similarity
                + (0.25 if matched_event_type and str(event.get("event_type") or "") == matched_event_type else 0.0)
                + (0.30 if has_correction else 0.0)
                + (0.20 if good else 0.0)
                + 0.10
                - (0.30 if bad_without_fix else 0.0)
                + float(event.get("confidence") or 0.0) * 0.10
            )
            suggestions.append(
                {
                    "source": "event_outcome" if outcome else "event",
                    "id": event.get("id"),
                    "event_id": event.get("id"),
                    "event_type": event.get("event_type"),
                    "user_phrase": event.get("user_phrase"),
                    "interpreted_intent": event.get("interpreted_intent"),
                    "goal": event.get("goal"),
                    "constraints": list(event.get("constraints") or []),
                    "physical_conditions": dict(event.get("physical_conditions") or {}),
                    "environment": dict(event.get("environment") or {}),
                    "tools": list(event.get("tools") or []),
                    "action_path": list(event.get("action_path") or []),
                    "result": str(event.get("result") or ""),
                    "evidence": list(event.get("evidence") or []),
                    "verification": str(event.get("verification") or ""),
                    "lesson": str(event.get("lesson") or ""),
                    "next_policy": str(event.get("next_policy") or ""),
                    "notify_policy": str(event.get("notify_policy") or ""),
                    "outcome": outcome_name,
                    "reason": str(outcome.get("reason") or ""),
                    "correction_from_user": str(outcome.get("correction_from_user") or ""),
                    "policy_update": str(outcome.get("policy_update") or ""),
                    "score": round(max(0.0, score), 3),
                }
            )
        suggestions.sort(key=lambda item: (-float(item.get("score") or 0.0), str(item.get("source") or "")))
        return {
            "ok": True,
            "query": str(user_phrase or ""),
            "scope": {
                "tenant_id": scope_ref.tenant_id,
                "agent_id": scope_ref.agent_id,
                "workspace_id": scope_ref.workspace_id,
                "user_id": scope_ref.user_id,
            },
            "matched_event_type": matched_event_type,
            "policy_suggestions": suggestions[:max_limit],
        }

    def close(self) -> None:
        self.conn.close()


def _normalize_datetime_bound(value: str | None, *, end_of_day: bool) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return f"{raw}T23:59:59.999999+00:00" if end_of_day else f"{raw}T00:00:00+00:00"
    return raw


def _record_meta_keys_from_json(meta_json: str) -> tuple[str, str]:
    try:
        meta = json.loads(str(meta_json or "{}"))
    except json.JSONDecodeError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return str(meta.get("idempotency_key") or ""), str(meta.get("semantic_key") or "")


def _json_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _meta_json_text_expression(meta_key: str) -> str:
    key = str(meta_key or "").strip()
    if not key or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_." for ch in key):
        return ""
    return f"CAST(json_extract(meta_json, '$.{key}') AS TEXT)"
