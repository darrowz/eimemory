from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class CollectionRecord:
    name: str
    path: str
    pattern: str


class QmdCompatRuntime:
    def __init__(
        self,
        *,
        xdg_config_home: str | Path | None = None,
        xdg_cache_home: str | Path | None = None,
    ) -> None:
        config_home = Path(
            xdg_config_home
            or os.environ.get("XDG_CONFIG_HOME", "").strip()
            or (Path.home() / ".config")
        )
        cache_home = Path(
            xdg_cache_home
            or os.environ.get("XDG_CACHE_HOME", "").strip()
            or (Path.home() / ".cache")
        )
        self.config_dir = config_home / "qmd"
        self.cache_dir = cache_home / "qmd"
        self.collections_path = self.config_dir / "collections.json"
        self.index_path = self.cache_dir / "index.sqlite"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def add_collection(self, path: str, name: str, pattern: str) -> None:
        collections = self._load_collections()
        resolved = str(Path(path).resolve())
        updated = [item for item in collections if item.name != name]
        updated.append(CollectionRecord(name=name, path=resolved, pattern=pattern))
        self._save_collections(updated)

    def remove_collection(self, name: str) -> None:
        collections = [item for item in self._load_collections() if item.name != name]
        self._save_collections(collections)
        conn = self._connect()
        try:
            conn.execute("DELETE FROM documents WHERE collection = ?", (name,))
            conn.commit()
        finally:
            conn.close()

    def list_collections(self) -> list[dict[str, str]]:
        return [
            {"name": item.name, "path": item.path, "pattern": item.pattern, "mask": item.pattern}
            for item in sorted(self._load_collections(), key=lambda entry: entry.name)
        ]

    def update_index(self) -> dict[str, int]:
        conn = self._connect()
        collections = self._load_collections()
        total = 0
        skipped = 0
        try:
            for collection in collections:
                files = self._list_files(collection)
                conn.execute("DELETE FROM documents WHERE collection = ?", (collection.name,))
                for file_path in files:
                    rel_path = file_path.relative_to(Path(collection.path)).as_posix()
                    try:
                        content = file_path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        skipped += 1
                        continue
                    doc_hash = self._hash_doc(collection.name, rel_path)
                    conn.execute(
                        """
                        INSERT INTO documents (hash, collection, path, content, active, updated_at)
                        VALUES (?, ?, ?, ?, 1, ?)
                        """,
                        (doc_hash, collection.name, rel_path, content, _now_iso()),
                    )
                    total += 1
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "collections": len(collections), "documents": total, "skipped": skipped}

    def embed(self) -> dict[str, object]:
        return {"ok": True, "embedded": True}

    def status(self) -> str:
        conn = self._connect()
        try:
            collections = self._load_collections()
            row = conn.execute("SELECT COUNT(*) AS count FROM documents WHERE active = 1").fetchone()
            documents = int(row["count"] or 0) if row is not None else 0
        finally:
            conn.close()
        return "\n".join(
            [
                "QMD Compatibility Status",
                f"Collections: {len(collections)}",
                f"Documents: {documents}",
                "Vectors: 0",
                f"DB Path: {self.index_path}",
            ]
        )

    def search(self, *, query: str, limit: int, collections: list[str] | None = None) -> list[dict[str, object]]:
        normalized_collections = [item for item in (collections or []) if item]
        conn = self._connect()
        try:
            sql = "SELECT hash, collection, path, content FROM documents WHERE active = 1"
            params: list[object] = []
            if normalized_collections:
                sql += f" AND collection IN ({','.join('?' for _ in normalized_collections)})"
                params.extend(normalized_collections)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        scored: list[tuple[int, dict[str, object]]] = []
        lowered_tokens = [token for token in query.lower().split() if token]
        for row in rows:
            content = str(row["content"] or "")
            haystack = content.lower()
            if lowered_tokens:
                score = sum(1 for token in lowered_tokens if token in haystack)
                if score == 0:
                    continue
            else:
                score = 1
            snippet = self._build_snippet(content, lowered_tokens)
            scored.append(
                (
                    score,
                    {
                        "docid": row["hash"],
                        "collection": row["collection"],
                        "file": row["path"],
                        "snippet": snippet,
                        "score": round(float(score), 6),
                    },
                )
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        return [payload for _, payload in scored[:limit]]

    def _load_collections(self) -> list[CollectionRecord]:
        if not self.collections_path.exists():
            return []
        payload = json.loads(self.collections_path.read_text(encoding="utf-8"))
        return [
            CollectionRecord(
                name=str(item["name"]),
                path=str(item["path"]),
                pattern=str(item.get("pattern") or item.get("mask") or "**/*.md"),
            )
            for item in payload
        ]

    def _save_collections(self, collections: list[CollectionRecord]) -> None:
        payload = [
            {"name": item.name, "path": item.path, "pattern": item.pattern, "mask": item.pattern}
            for item in sorted(collections, key=lambda entry: entry.name)
        ]
        self.collections_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.index_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                hash TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                path TEXT NOT NULL,
                content TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        return conn

    def _list_files(self, collection: CollectionRecord) -> list[Path]:
        base = Path(collection.path)
        if not base.exists():
            return []
        pattern = collection.pattern or "**/*.md"
        if "*" in pattern or "?" in pattern:
            files = [path for path in base.glob(pattern) if path.is_file()]
            if not files and pattern.startswith("**/"):
                files = [path for path in base.glob(pattern.removeprefix("**/")) if path.is_file()]
        else:
            candidate = base / pattern
            files = [candidate] if candidate.exists() and candidate.is_file() else []
        return sorted(files)

    def _hash_doc(self, collection: str, rel_path: str) -> str:
        return hashlib.sha1(f"{collection}:{rel_path}".encode("utf-8")).hexdigest()

    def _build_snippet(self, content: str, lowered_tokens: list[str]) -> str:
        lines = content.splitlines() or [content]
        start_line = 1
        if lowered_tokens:
            for idx, line in enumerate(lines, start=1):
                lowered = line.lower()
                if any(token in lowered for token in lowered_tokens):
                    start_line = idx
                    break
        snippet_lines = lines[max(0, start_line - 1): max(0, start_line - 1) + 6] or lines[:6]
        count = max(1, len(snippet_lines))
        body = "\n".join(snippet_lines)
        return f"@@ -{start_line},{count}\n{body}".strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eimemory qmd")
    sub = parser.add_subparsers(dest="command")

    collection = sub.add_parser("collection")
    collection_sub = collection.add_subparsers(dest="collection_command")

    collection_list = collection_sub.add_parser("list")
    collection_list.add_argument("--json", action="store_true")

    collection_add = collection_sub.add_parser("add")
    collection_add.add_argument("path")
    collection_add.add_argument("--name", required=True)
    collection_add.add_argument("--mask", dest="pattern", default=None)
    collection_add.add_argument("--glob", dest="pattern", default=None)

    collection_remove = collection_sub.add_parser("remove")
    collection_remove.add_argument("name")

    sub.add_parser("update")
    sub.add_parser("embed")
    sub.add_parser("status")

    for name in ("search", "query", "vsearch"):
        search = sub.add_parser(name)
        search.add_argument("query")
        search.add_argument("--json", action="store_true")
        search.add_argument("-n", type=int, default=6)
        search.add_argument("-c", dest="collections", action="append", default=[])

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parsed = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    runtime = QmdCompatRuntime()

    if parsed.command == "collection":
        if parsed.collection_command == "list":
            print(json.dumps(runtime.list_collections(), ensure_ascii=False, indent=2))
            return 0
        if parsed.collection_command == "add":
            runtime.add_collection(parsed.path, parsed.name, parsed.pattern or "**/*.md")
            print(json.dumps({"ok": True, "name": parsed.name}, ensure_ascii=False))
            return 0
        if parsed.collection_command == "remove":
            runtime.remove_collection(parsed.name)
            print(json.dumps({"ok": True, "name": parsed.name}, ensure_ascii=False))
            return 0
        print(json.dumps({"error": "unknown collection command"}))
        return 1

    if parsed.command == "update":
        print(json.dumps(runtime.update_index(), ensure_ascii=False, indent=2))
        return 0

    if parsed.command == "embed":
        print(json.dumps(runtime.embed(), ensure_ascii=False, indent=2))
        return 0

    if parsed.command == "status":
        print(runtime.status())
        return 0

    if parsed.command in {"search", "query", "vsearch"}:
        results = runtime.search(query=parsed.query, limit=parsed.n, collections=parsed.collections)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print(json.dumps({"usage": "eimemory qmd <collection|update|embed|status|search|query|vsearch>"}))
    return 0
