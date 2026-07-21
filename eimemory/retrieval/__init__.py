"""Governed recall contracts and physical candidate sources."""

from .contracts import (
    CandidateBatch,
    CandidateHit,
    CandidateRef,
    CandidateRequest,
    CandidateSource,
    ExactScope,
    RecallEngine,
    RecallPipelineSnapshot,
)
from .engine import GovernedRecallEngine, RecallCallbacks
from .sqlite_source import SQLiteCandidateSource
from .postgres_sync import PostgresVectorIndexSynchronizer, ProjectionCursor, SQLiteProjectionReader
from .postgres_vector import (
    EmbeddingProvider,
    IndexState,
    OpenAICompatibleEmbeddingProvider,
    PostgresCandidateRepository,
    PostgresVectorCandidateSource,
    PostgresVectorConfig,
    build_postgres_vector_candidate_source,
)

__all__ = [
    "CandidateBatch",
    "CandidateHit",
    "CandidateRef",
    "CandidateRequest",
    "CandidateSource",
    "ExactScope",
    "EmbeddingProvider",
    "GovernedRecallEngine",
    "RecallCallbacks",
    "RecallEngine",
    "RecallPipelineSnapshot",
    "IndexState",
    "OpenAICompatibleEmbeddingProvider",
    "PostgresCandidateRepository",
    "PostgresVectorCandidateSource",
    "PostgresVectorConfig",
    "PostgresVectorIndexSynchronizer",
    "ProjectionCursor",
    "SQLiteProjectionReader",
    "SQLiteCandidateSource",
    "build_postgres_vector_candidate_source",
]
