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
