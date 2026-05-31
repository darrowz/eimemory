from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any
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
from eimemory.models.records import RecordEnvelope, ScopeRef
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


MAX_QUERY_LIMIT = 1000
_MAX_LEXICAL_ADJUSTMENT = 0.18
_DEFAULT_CANDIDATE_LIMIT = 360
_MAX_CANDIDATE_LIMIT = 1200


class SqliteRecordStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self._init_db()

    def _init_db(self) -> None:
        existing = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='records'"
        ).fetchone()
        if not existing:
            self._create_records_table()
            self._create_indexes()
            self._create_recall_index_tables()
            self._create_event_memory_tables()
            self._create_policy_rollout_tables()
            self._seed_default_intent_patterns()
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
        self._create_indexes()
        self._create_recall_index_tables()
        self._create_event_memory_tables()
        self._migrate_intent_patterns_schema()
        self._create_policy_rollout_tables()
        self._seed_default_intent_patterns()
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
                agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                embedding_json TEXT NOT NULL DEFAULT '[]',
                meta_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._create_recall_index_tables()

    def _create_indexes(self) -> None:
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_records_record_id ON records(record_id)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_scope ON records(tenant_id, agent_id, workspace_id, user_id)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_records_kind_scope ON records(kind, tenant_id, agent_id, workspace_id, user_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_records_source_kind ON records(source, kind)")

    def _create_recall_index_tables(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recall_index (
                storage_key TEXT PRIMARY KEY,
                record_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
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
                body_text TEXT NOT NULL,
                anchor_terms TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recall_index_scope_lane ON recall_index(tenant_id, agent_id, workspace_id, user_id, lane, visibility)"
        )
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

    def _migrate_intent_patterns_schema(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(intent_patterns)").fetchall()}
        if "status" not in columns:
            self.conn.execute("ALTER TABLE intent_patterns ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "last_rollback_reason" not in columns:
            self.conn.execute("ALTER TABLE intent_patterns ADD COLUMN last_rollback_reason TEXT NOT NULL DEFAULT ''")
        if "payload_json" not in columns:
            return
        rows = self.conn.execute("SELECT id, payload_json FROM intent_patterns").fetchall()
        if not rows:
            return
        for row in rows:
            raw = str(row["payload_json"])
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            payload["status"] = str(payload.get("status") or "active")
            if payload["status"] not in {"candidate", "shadow", "active", "rolled_back", "quarantined"}:
                payload["status"] = "active"
            self.conn.execute(
                "UPDATE intent_patterns SET payload_json = ?, status = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), payload["status"], str(row["id"])),
            )

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
        rows = self.conn.execute(f"SELECT {', '.join(select_columns)} FROM records_legacy").fetchall()
        for row in rows:
            row_data = dict(row)
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
                    embedding_json, meta_json, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    row_data["meta_json"],
                    row_data["payload_json"],
                    row_data["created_at"],
                    row_data["updated_at"],
                ),
            )
        self.conn.execute("DROP TABLE records_legacy")
        self.conn.commit()

    def upsert(self, record: RecordEnvelope) -> None:
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
        self.conn.execute(
            """
            INSERT INTO records (
                storage_key, record_id, kind, status, title, summary, detail, content_text,
                source, agent_id, workspace_id, user_id, tenant_id,
                embedding_json, meta_json, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(storage_key) DO UPDATE SET
                status=excluded.status,
                title=excluded.title,
                summary=excluded.summary,
                detail=excluded.detail,
                content_text=excluded.content_text,
                source=excluded.source,
                agent_id=excluded.agent_id,
                workspace_id=excluded.workspace_id,
                user_id=excluded.user_id,
                tenant_id=excluded.tenant_id,
                embedding_json=excluded.embedding_json,
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
                record.scope.agent_id,
                record.scope.workspace_id,
                record.scope.user_id,
                record.scope.tenant_id,
                embedding,
                json.dumps(record.meta, ensure_ascii=False),
                json.dumps(payload, ensure_ascii=False),
                record.time.created_at,
                record.time.updated_at,
            ),
        )
        self._upsert_recall_index(record=record, storage_key=storage_key, content_text=content_text)
        self.conn.commit()

    def _upsert_recall_index(self, *, record: RecordEnvelope, storage_key: str, content_text: str) -> None:
        lane, visibility, source_class, memory_type, projection_type, quality_score = self._recall_index_traits(record)
        title_text = str(record.title or "")
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
                storage_key, record_id, kind, status, source,
                tenant_id, agent_id, workspace_id, user_id,
                lane, visibility, source_class, memory_type, projection_type,
                quality_score, title_text, body_text, anchor_terms, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(storage_key) DO UPDATE SET
                record_id=excluded.record_id,
                kind=excluded.kind,
                status=excluded.status,
                source=excluded.source,
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
                body_text,
                anchor_terms,
                record.time.updated_at,
            ),
        )
        if self._has_fts_table():
            self.conn.execute("DELETE FROM recall_index_fts WHERE storage_key = ?", (storage_key,))
            self.conn.execute(
                "INSERT INTO recall_index_fts(storage_key, title_text, body_text, anchor_terms) VALUES (?, ?, ?, ?)",
                (storage_key, title_text, body_text, anchor_terms),
            )

    def _delete_recall_index(self, storage_key: str) -> None:
        self.conn.execute("DELETE FROM recall_index WHERE storage_key = ?", (storage_key,))
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
        rows = self.conn.execute(
            "SELECT storage_key, payload_json, content_text FROM records ORDER BY updated_at DESC"
        ).fetchall()
        if index_count > record_count:
            self.conn.execute("DELETE FROM recall_index")
            if self._has_fts_table():
                self.conn.execute("DELETE FROM recall_index_fts")
        for row in rows:
            record = RecordEnvelope.from_dict(json.loads(row["payload_json"]))
            self._upsert_recall_index(
                record=record,
                storage_key=str(row["storage_key"]),
                content_text=str(row["content_text"] or ""),
            )

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
    ) -> list[RecordEnvelope]:
        records, _ = self.search_with_diagnostics(query=query, kinds=kinds, scope=scope, limit=limit)
        return records

    def search_with_diagnostics(
        self,
        *,
        query: str,
        kinds: list[str] | None,
        scope: ScopeRef,
        limit: int,
        recall_filters: dict | None = None,
    ) -> tuple[list[RecordEnvelope], dict]:
        limit = self._normalize_limit(limit)
        recall_filters = self._normalized_recall_filters(recall_filters)
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
        for row in rows:
            haystack = str(row["content_text"] or "").lower()
            payload = json.loads(row["payload_json"])
            lexical_signal = analyze_lexical_signal(
                query,
                haystack,
                record_kind=str(payload.get("kind", "")) if isinstance(payload, dict) else "",
                record_source=str(payload.get("source", "")) if isinstance(payload, dict) else "",
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
            record = RecordEnvelope.from_dict(payload) if isinstance(payload, dict) else RecordEnvelope.from_dict({})
            if not self._record_matches_recall_filters(record, recall_filters):
                continue
            quality = self._quality_from_record(record)
            if quality.get("capture_decision") == "reject":
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
            "recall_filters": dict(recall_filters or {}),
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
            "SELECT storage_key, payload_json, content_text, embedding_json FROM records WHERE storage_key IN ("
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
        self._apply_scope_filters(where, params, scope)
        where.append("status != 'rejected'")
        sql = (
            "SELECT storage_key, payload_json, content_text, embedding_json FROM records WHERE "
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
        self._apply_recall_index_scope_filters(where, params, scope, alias=alias)
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

    def get_active_policy(self, *, task_type: str, scope: ScopeRef) -> dict:
        where = [
            "kind = 'rule'",
            "status = 'active'",
        ]
        params: list[object] = []
        self._apply_scope_filters(where, params, scope)
        order_by = "updated_at DESC"
        if scope.user_id:
            order_by = "CASE WHEN user_id = ? THEN 1 ELSE 0 END DESC, updated_at DESC"
            params = [*params, scope.user_id]
        rows = self.conn.execute(
            "SELECT payload_json FROM records WHERE "
            + " AND ".join(where)
            + f" ORDER BY {order_by}",
            params,
        ).fetchall()
        for row in rows:
            record = RecordEnvelope.from_dict(json.loads(row["payload_json"]))
            if str(business_metadata(record.meta).get("task_type", "")) == task_type:
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
        return RecordEnvelope.from_dict(json.loads(row["payload_json"]))

    def list_records(
        self,
        *,
        kinds: list[str] | None = None,
        scope: ScopeRef | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
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
        rows = self.conn.execute(
            "SELECT payload_json FROM records WHERE "
            + " AND ".join(where)
            + " ORDER BY updated_at DESC, record_id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [RecordEnvelope.from_dict(json.loads(row["payload_json"])) for row in rows]

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

    def rewrite(self, record: RecordEnvelope, *, previous_scope: ScopeRef | None = None) -> None:
        previous_key = None
        if previous_scope is not None:
            previous_key = self._storage_key_from_values(
                record_id=record.record_id,
                tenant_id=previous_scope.tenant_id,
                agent_id=previous_scope.agent_id,
                workspace_id=previous_scope.workspace_id,
                user_id=previous_scope.user_id,
            )
        new_key = self._storage_key(record)
        if previous_key and previous_key != new_key:
            self.conn.execute("DELETE FROM records WHERE storage_key = ?", (previous_key,))
            self._delete_recall_index(previous_key)
            self.conn.commit()
        self.upsert(record)

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
        filters = dict(recall_filters or {})
        if not filters:
            return True
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
                return False
        blocked_kinds = set(self._as_tuple(filters.get("blocked_kinds") or ()))
        if blocked_kinds and record.kind in blocked_kinds:
            return False
        allowed_kinds = set(self._as_tuple(filters.get("allowed_kinds") or ()))
        if allowed_kinds and record.kind not in allowed_kinds:
            return False
        blocked_sources = set(filters.get("blocked_sources") or [])
        if blocked_sources and labels["sources"] & blocked_sources:
            return False
        allowed_sources = set(filters.get("allowed_sources") or [])
        if allowed_sources and not labels["sources"] & allowed_sources:
            return False
        allowed_memory_types = set(filters.get("allowed_memory_types") or [])
        if allowed_memory_types and record.kind == "memory" and labels["memory_types"] and not labels["memory_types"] & allowed_memory_types:
            return False
        organs = set(filters.get("organs") or [])
        if organs and labels["organs"] and not labels["organs"] & organs:
            return False
        return True

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
        }

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
        if str(data.get("outcome") or "").lower() == "bad" and pattern_ids:
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
            details={"previous_status": previous_status, "follow_up_opportunities": follow_ups},
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
    ) -> dict[str, Any]:
        scope_ref = normalize_scope(scope)
        max_limit = max(1, min(20, int(limit or 5)))
        context_payload = dict(context or {})
        status_values = ["active"]
        if bool(context_payload.get("include_shadow")):
            status_values.append("shadow")
        status_placeholders = ",".join("?" for _ in status_values)
        pattern_rows = self.conn.execute(
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
        ).fetchall()
        suggestions: list[dict[str, Any]] = []
        matched_event_type = str(context_payload.get("event_type") or "")
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
