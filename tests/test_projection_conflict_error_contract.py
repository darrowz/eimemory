"""Real repository CAS/error boundary; in-memory PG wire, no network/model."""
from types import SimpleNamespace

import pytest

from eimemory.retrieval import postgres_cli
from eimemory.retrieval.postgres_vector import (
    IndexState, PostgresCandidateRepository, PostgresVectorConfig, PROJECTION_DIGEST_SCHEMA,
)


class Wire:
    def __init__(self, revision="8", *, fault=""):
        self.revision, self.fault = revision, fault
        self.commits = self.rollbacks = 0
        self.statements = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, parameters=()):
        self.statements.append(sql)
        if sql.startswith("SELECT ready") and self.fault:
            raise RuntimeError(self.fault)

    def fetchone(self):
        return dict(ready=True, in_progress=False, committed_watermark="synthetic-generation",
                    authority_revision=self.revision, embedding_fingerprint="embedding-proof",
                    projection_fingerprint="projection-proof", projection_digest_schema=PROJECTION_DIGEST_SCHEMA)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def native(monkeypatch, *, revision="8", fault=""):
    config = PostgresVectorConfig(enabled=True, dsn="synthetic-unused", vector_dimension=3,
        projection_memory_only=True, embedding_provider=object())
    repository = PostgresCandidateRepository(config)
    wire = Wire(revision, fault=fault)
    monkeypatch.setattr(repository, "_connect", lambda **kwargs: wire)
    expected = IndexState(ready=True, watermark="synthetic-generation", authority_revision="8",
        embedding_fingerprint="embedding-proof", projection_fingerprint="projection-proof",
        projection_digest_schema=PROJECTION_DIGEST_SCHEMA)
    return repository, wire, config, expected


def apply(repository, expected):
    return repository.apply_memory_delta(expected_state=expected, projections=[], changed_keys=[],
        authority_revision="9", authoritative_head=("", ""))


def test_native_cas_conflict_preserves_only_fixed_code_and_rolls_back(monkeypatch):
    repository, wire, _, expected = native(monkeypatch, revision="9")
    with pytest.raises(RuntimeError, match="^postgres_delta_conflict$"):
        apply(repository, expected)
    assert wire.rollbacks == 1 and wire.commits == 0
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in wire.statements)


@pytest.mark.parametrize("fault", ["postgres_delta_conflict", "driver failure with synthetic-private-token"])
def test_driver_error_cannot_mint_recoverable_conflict_or_expose_details(monkeypatch, fault):
    repository, wire, _, expected = native(monkeypatch, fault=fault)
    with pytest.raises(RuntimeError, match="^postgres_delta_apply_failed$"):
        apply(repository, expected)
    assert wire.rollbacks == 1 and wire.commits == 0


def test_native_delta_success_keeps_atomic_commit(monkeypatch):
    repository, wire, _, expected = native(monkeypatch)
    apply(repository, expected)
    assert wire.commits == 1 and wire.rollbacks == 0
    assert any(sql.startswith("UPDATE") for sql in wire.statements)


def test_cli_retains_native_fixed_conflict_code(monkeypatch):
    repository, wire, config, expected = native(monkeypatch, revision="9")
    monkeypatch.setattr(postgres_cli, "config_from_env", lambda: config)
    monkeypatch.setattr(postgres_cli, "PostgresCandidateRepository", lambda supplied: repository)
    monkeypatch.setattr("eimemory.retrieval.incremental_sync.maintain_memory_projection",
                        lambda **kwargs: apply(kwargs["repository"], expected))
    result = postgres_cli.handle_vector_index_command(SimpleNamespace(vector_index_command="sync"),
                                                      SimpleNamespace(store=None))
    assert result == dict(ok=False, complete=False, error="memory_projection_maintenance_failed",
                         reason="postgres_delta_conflict")
    assert wire.rollbacks == 1 and wire.commits == 0


@pytest.mark.parametrize("fault", ["postgres_delta_apply_failed", "unknown synthetic-private-token"])
def test_cli_masks_other_errors_instead_of_classifying_them_as_conflict(monkeypatch, fault):
    _, _, config, _ = native(monkeypatch)
    monkeypatch.setattr(postgres_cli, "config_from_env", lambda: config)
    def fail(**kwargs):
        raise RuntimeError(fault)
    monkeypatch.setattr("eimemory.retrieval.incremental_sync.maintain_memory_projection", fail)
    result = postgres_cli.handle_vector_index_command(SimpleNamespace(vector_index_command="sync"),
                                                      SimpleNamespace(store=None))
    assert result == dict(ok=False, complete=False, error="memory_projection_maintenance_failed",
                         reason="RuntimeError")
