from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from eimemory.recall import analyze_lexical_signal

from eimemory.embeddings.local import cosine_similarity, embed_text
from eimemory.identity import hongtu_query_scopes
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scoring import ScoreContext, evaluate_recall_score, extract_memory_score, score_from_legacy_quality
from eimemory.metadata import business_metadata


MAX_QUERY_LIMIT = 1000
_MAX_LEXICAL_ADJUSTMENT = 0.18


class SqliteRecordStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        existing = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='records'"
        ).fetchone()
        if not existing:
            self._create_records_table()
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

    def _create_indexes(self) -> None:
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_records_record_id ON records(record_id)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_scope ON records(tenant_id, agent_id, workspace_id, user_id)"
        )

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
        self.conn.commit()

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
        where = ["1=1"]
        params: list[object] = []
        if kinds:
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        self._apply_scope_filters(where, params, scope)
        where.append("status != 'rejected'")
        sql = (
            "SELECT payload_json, content_text, embedding_json FROM records WHERE "
            + " AND ".join(where)
            + " ORDER BY updated_at DESC"
        )
        rows = self.conn.execute(sql, params).fetchall()
        recall_filters = self._normalized_recall_filters(recall_filters)
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
            "retrieval_mode": "hybrid_vector",
            "scored_items": [score_report for _, _, _, score_report in selected_rows],
            "recall_filters": dict(recall_filters or {}),
        }

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

    def close(self) -> None:
        self.conn.close()
