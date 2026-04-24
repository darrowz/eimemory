from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from eimemory.embeddings.local import cosine_similarity, embed_text
from eimemory.models.records import RecordEnvelope, ScopeRef


MAX_QUERY_LIMIT = 1000


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
        content_text = "\n".join(
            part for part in [
                record.title,
                record.summary,
                record.detail,
                str(record.content.get("text", "")),
                str(record.content.get("excerpt", "")),
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
        lowered_tokens = [token for token in query.lower().split() if token]
        query_ngrams = self._char_ngrams(query.lower())
        query_embedding = embed_text(query)
        scored: list[tuple[float, float, RecordEnvelope, dict]] = []
        vector_hits = 0
        for row in rows:
            haystack = str(row["content_text"] or "").lower()
            lexical_score = sum(1 for token in lowered_tokens if token in haystack) if lowered_tokens else 1
            semantic_score = self._jaccard_score(query_ngrams, self._char_ngrams(haystack))
            stored_embedding = self._parse_embedding(row["embedding_json"])
            vector_score = max(0.0, cosine_similarity(query_embedding, stored_embedding))
            if vector_score >= 0.12:
                vector_hits += 1
            record = RecordEnvelope.from_dict(json.loads(row["payload_json"]))
            quality = self._quality_from_record(record)
            if quality.get("capture_decision") == "reject":
                continue
            quality_score = float(quality.get("salience_score") or 0.0)
            quality_boost = quality_score * 1.25 if record.kind == "memory" else quality_score * 0.35
            relevance_score = float(lexical_score) + semantic_score + vector_score
            if lowered_tokens and lexical_score <= 0 and semantic_score < 0.08 and vector_score < 0.28:
                continue
            score = relevance_score + quality_boost
            scored.append(
                (
                    score,
                    vector_score,
                    record,
                    {
                        "record_id": record.record_id,
                        "kind": record.kind,
                        "title": record.title,
                        "lexical_score": lexical_score,
                        "semantic_score": round(semantic_score, 4),
                        "vector_score": round(vector_score, 4),
                        "quality_score": round(quality_score, 4),
                        "quality": quality,
                        "final_score": round(score, 4),
                    },
                )
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        selected_rows = scored[:limit]
        selected = [record for _, _, record, _ in selected_rows]
        return selected, {
            "vector_hits": sum(1 for _, vector_score, _, _ in selected_rows if vector_score >= 0.12),
            "retrieval_mode": "hybrid_vector",
            "scored_items": [score_report for _, _, _, score_report in selected_rows],
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
            if str(record.meta.get("task_type", "")) == task_type:
                return dict(record.meta)
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
        where.append("tenant_id = ?")
        params.append(scope.tenant_id or "default")
        where.append("agent_id = ?")
        params.append(scope.agent_id)
        where.append("workspace_id = ?")
        params.append(scope.workspace_id)
        if scope.user_id:
            where.append("(user_id = ? OR user_id = '')")
            params.append(scope.user_id)
        else:
            where.append("user_id = ''")

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

    def _quality_from_record(self, record: RecordEnvelope) -> dict:
        quality = record.meta.get("quality") if isinstance(record.meta, dict) else {}
        if not isinstance(quality, dict):
            return {}
        return dict(quality)

    def close(self) -> None:
        self.conn.close()
