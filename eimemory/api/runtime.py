from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from eimemory.api.evolution import EvolutionAPI
from eimemory.api.memory import MemoryAPI
from eimemory.intake.registry import SourceRegistry
from eimemory.intake.papers.sources import ingest_paper_source
from eimemory.knowledge.compiler import KnowledgeCompilation, compile_paper_knowledge
from eimemory.knowledge.extract import PaperMemoryExtraction, extract_paper_memory
from eimemory.knowledge.projectors import project_operational_knowledge
from eimemory.config.defaults import default_root
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage.runtime_store import RuntimeStore


class Runtime:
    def __init__(self, store: RuntimeStore) -> None:
        self.store = store
        self.memory = MemoryAPI(store)
        self.evolution = EvolutionAPI(store)
        self.sources = SourceRegistry(self.store.root / "state" / "source_registry.json")

    @classmethod
    def create(cls, *, root: str | Path | None = None) -> "Runtime":
        final_root = default_root(root)
        return cls(RuntimeStore(final_root))

    def close(self) -> None:
        self.store.close()

    def knowledge_intake_loop(self):
        from eimemory.intake.loop import KnowledgeIntakeLoop

        return KnowledgeIntakeLoop(self.sources, self.store)

    def run_knowledge_intake(
        self,
        *,
        scope: dict | None = None,
        persist: bool = False,
        source_kind: str | None = None,
        limit: int | None = None,
    ) -> dict:
        return self.knowledge_intake_loop().run(
            scope,
            persist=persist,
            source_kind=source_kind,
            limit=limit,
        )

    def collect_external_sources(
        self,
        *,
        source_kind: str | None = None,
        limit: int | None = None,
        fetch_text=None,
        fetch: bool = False,
        persist: bool = False,
        scope: dict | None = None,
    ) -> dict:
        from dataclasses import asdict

        from eimemory.intake.connectors import collect_from_source_entry

        sources = self.sources.list_sources(enabled=True, source_kind=source_kind or None)
        if limit is not None:
            sources = sources[: max(0, int(limit))]
        if fetch and fetch_text is None:
            fetch_text = _default_fetch_text
        results = []
        item_count = 0
        written_count = 0
        skipped_existing_count = 0
        quarantined_count = 0
        rejected_count = 0
        persisted_record_ids: list[str] = []
        scope_ref = ScopeRef.from_dict(scope)
        for source in sources:
            result = collect_from_source_entry(source, fetch_text=fetch_text)
            payload = asdict(result)
            payload["source_id"] = source.source_id
            payload["source_kind"] = source.source_kind
            item_count += len(result.items)
            if persist:
                for item in result.items:
                    record = _collected_item_record(
                        item,
                        source_id=source.source_id,
                        source_kind=source.source_kind,
                        fetch_metadata=dict(result.metadata or {}),
                        scope=scope_ref,
                    )
                    if record.status == "quarantined":
                        quarantined_count += 1
                    elif record.status == "rejected":
                        rejected_count += 1
                    if self.store.get_by_id(record.record_id) is not None:
                        skipped_existing_count += 1
                        continue
                    self.store.append(record)
                    written_count += 1
                    persisted_record_ids.append(record.record_id)
            results.append(payload)
        return {
            "ok": True,
            "persist": bool(persist),
            "source_count": len(sources),
            "item_count": item_count,
            "written_count": written_count,
            "skipped_existing_count": skipped_existing_count,
            "quarantined_count": quarantined_count,
            "rejected_count": rejected_count,
            "persisted_record_ids": persisted_record_ids,
            "results": results,
        }

    def promote_paper_candidate(self, record_or_payload, *, scope: dict | None = None) -> dict:
        from eimemory.intake.pipeline import promote_paper_candidate

        return promote_paper_candidate(self, record_or_payload, scope)

    def promote_collected_paper_candidates(self, *, scope: dict | None = None, limit: int = 100, auto: bool = False) -> dict:
        from eimemory.intake.pipeline import promote_collected_paper_candidates

        return promote_collected_paper_candidates(self, scope, limit=limit, auto=auto)

    def list_intake_review_queue(self, *, scope: dict | None = None, status=None, limit: int = 100) -> list[dict]:
        from eimemory.intake.review import list_review_queue

        return list_review_queue(self, scope, status=status, limit=limit)

    def review_intake_candidate(
        self,
        *,
        record_id: str,
        decision: str,
        reviewer: str,
        note: str = "",
        scope: dict | None = None,
    ) -> RecordEnvelope:
        from eimemory.intake.review import review_candidate

        return review_candidate(self, record_id, decision, reviewer, note=note, scope=scope)

    def promote_intake_candidate(
        self,
        *,
        record_id: str,
        promoter: str,
        note: str = "",
        scope: dict | None = None,
    ) -> RecordEnvelope:
        from eimemory.intake.review import promote_candidate

        return promote_candidate(self, record_id, promoter, note=note, scope=scope)

    def merge_intake_candidates(
        self,
        *,
        source_record_id: str,
        target_record_id: str,
        reviewer: str,
        note: str = "",
        scope: dict | None = None,
    ) -> RecordEnvelope:
        from eimemory.intake.review import merge_candidates

        return merge_candidates(self, source_record_id, target_record_id, reviewer, note=note, scope=scope)

    def source_quality_report(self, *, scope: dict | None = None) -> dict:
        from eimemory.intake.policy import build_source_quality_report

        return build_source_quality_report(self, scope or {})

    def collection_policy(self, *, scope: dict | None = None, topic_gaps: list[str] | None = None) -> dict:
        from eimemory.intake.policy import recommend_collection_policy

        return recommend_collection_policy(self, scope or {}, topic_gaps=topic_gaps or [])

    def export_knowledge_pack(self, path: str | Path, *, scope: dict | None = None, include_candidates: bool = False) -> dict:
        from eimemory.intake.packs import export_knowledge_pack

        return export_knowledge_pack(self, path, scope or {}, include_candidates=include_candidates)

    def import_knowledge_pack(self, path: str | Path, *, scope: dict | None = None, dry_run: bool = False) -> dict:
        from eimemory.intake.packs import import_knowledge_pack

        return import_knowledge_pack(self, path, scope or {}, dry_run=dry_run)

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def ingest_paper_source(self, paper_input: dict, *, scope: dict | None = None) -> RecordEnvelope:
        return ingest_paper_source(self.store, paper_input, scope=scope)

    def extract_paper_memory(self, paper_input: dict, *, scope: dict | None = None) -> PaperMemoryExtraction:
        result = extract_paper_memory(
            paper_source_id=str(paper_input["paper_source_id"]),
            title=str(paper_input.get("title", "")),
            abstract=str(paper_input.get("abstract", "")),
            body=str(paper_input.get("body", "")),
            metadata=dict(paper_input.get("metadata") or {}),
            provenance=dict(paper_input.get("provenance") or {}),
        )
        for record in result.to_records(scope=scope):
            self.store.append(record)
        return result

    def compile_paper_knowledge(
        self,
        *,
        extraction: PaperMemoryExtraction,
        scope: dict | None = None,
    ) -> KnowledgeCompilation:
        result = compile_paper_knowledge(extraction=extraction)
        for record in result.to_records(scope=scope):
            self.store.append(record)
        return result

    def project_operational_knowledge(self, *, scope: dict | None = None, limit: int = 100) -> dict:
        return project_operational_knowledge(self.store, scope=scope, limit=limit)


def _default_fetch_text(url: str) -> str:
    request = Request(
        url,
        headers={
            "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
            "User-Agent": "eimemory/1.0 (+https://github.com/darrowz/eimemory)",
        },
    )
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _collected_item_record(
    item: Any,
    *,
    source_id: str,
    source_kind: str,
    fetch_metadata: dict[str, Any],
    scope: ScopeRef,
) -> RecordEnvelope:
    fingerprint = str(getattr(item, "fingerprint", "") or "")
    item_source_kind = str(getattr(item, "source_kind", "") or "")
    title = str(getattr(item, "title", "") or "Fetched knowledge candidate")
    content = str(getattr(item, "content", "") or "")
    item_url = str(getattr(item, "url", "") or "")
    metadata = dict(getattr(item, "metadata", {}) or {})
    status = _collected_item_status(metadata)
    summary = _summary_from_content(content)
    content_excerpt = _content_excerpt(content)
    provenance = {
        "source_id": str(source_id or ""),
        "source_kind": str(source_kind or ""),
        "item_url": item_url,
        "fingerprint": fingerprint,
        "fetch_source": item_source_kind,
        "fetch_metadata": dict(fetch_metadata or {}),
        "published_at": str(getattr(item, "published_at", "") or ""),
    }
    content_payload = {
        "source_id": str(source_id or ""),
        "source_kind": str(source_kind or ""),
        "fetch_source": item_source_kind,
        "item_url": item_url,
        "fingerprint": fingerprint,
        "title": title,
        "summary": summary,
        "content_excerpt": content_excerpt,
        "metadata": metadata,
        "published_at": str(getattr(item, "published_at", "") or ""),
    }
    return RecordEnvelope(
        record_id=_collected_item_record_id(fingerprint, source_id=str(source_id or ""), scope=scope),
        kind="knowledge_candidate",
        status=status,
        title=f"Knowledge candidate: {title}",
        summary=summary,
        detail=content_excerpt,
        content=content_payload,
        tags=[],
        links=[],
        evidence=[],
        source="eimemory.intake.collect",
        scope=scope,
        time=TimeRef.now(),
        provenance=provenance,
        meta={
            "intake_decision": status,
            "source_id": str(source_id or ""),
            "source_kind": str(source_kind or ""),
            "item_url": item_url,
            "fingerprint": fingerprint,
            "fetch_source": item_source_kind,
            "safety": dict(metadata.get("safety") or {}) if isinstance(metadata.get("safety"), dict) else {},
        },
    )


def _collected_item_record_id(fingerprint: str, *, source_id: str, scope: ScopeRef) -> str:
    stable = fingerprint or sha256(source_id.encode("utf-8", errors="ignore")).hexdigest()
    return f"kc_fetch_{stable[:12]}_{_scope_hash(scope)}"


def _scope_hash(scope: ScopeRef) -> str:
    payload = {
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "workspace_id": scope.workspace_id,
        "user_id": scope.user_id,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:8]


def _collected_item_status(metadata: dict[str, Any]) -> str:
    safety = metadata.get("safety") if isinstance(metadata, dict) else None
    if not isinstance(safety, dict) or not safety:
        return "candidate"
    if safety.get("prompt_injection"):
        return "quarantined"
    return "rejected"


def _summary_from_content(content: str) -> str:
    return " ".join(str(content or "").split())[:240]


def _content_excerpt(content: str) -> str:
    return str(content or "").strip()[:1200]
