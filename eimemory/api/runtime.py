from __future__ import annotations

from pathlib import Path

from eimemory.api.evolution import EvolutionAPI
from eimemory.api.memory import MemoryAPI
from eimemory.intake.registry import SourceRegistry
from eimemory.intake.papers.sources import ingest_paper_source
from eimemory.knowledge.compiler import KnowledgeCompilation, compile_paper_knowledge
from eimemory.knowledge.extract import PaperMemoryExtraction, extract_paper_memory
from eimemory.config.defaults import default_root
from eimemory.models.records import RecordEnvelope
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
    ) -> dict:
        from dataclasses import asdict

        from eimemory.intake.connectors import collect_from_source_entry

        sources = self.sources.list_sources(enabled=True, source_kind=source_kind or None)
        if limit is not None:
            sources = sources[: max(0, int(limit))]
        results = []
        item_count = 0
        for source in sources:
            result = collect_from_source_entry(source, fetch_text=fetch_text)
            payload = asdict(result)
            payload["source_id"] = source.source_id
            payload["source_kind"] = source.source_kind
            item_count += len(result.items)
            results.append(payload)
        return {
            "ok": True,
            "source_count": len(sources),
            "item_count": item_count,
            "results": results,
        }

    def promote_paper_candidate(self, record_or_payload, *, scope: dict | None = None) -> dict:
        from eimemory.intake.pipeline import promote_paper_candidate

        return promote_paper_candidate(self, record_or_payload, scope)

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
