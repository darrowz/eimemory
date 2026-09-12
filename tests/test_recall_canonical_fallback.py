from __future__ import annotations

from contextlib import closing
from dataclasses import asdict

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


@pytest.mark.parametrize("legacy_match", [False, True])
def test_canonical_fallback_preserves_grounded_canonical_fact(tmp_path, legacy_match) -> None:
    canonical_scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    with closing(RuntimeStore(tmp_path)) as store:
        canonical = store.append(RecordEnvelope.create(
            kind="memory",
            title="Application database configuration",
            summary="The current application database is PostgreSQL.",
            content={"text": "The current application database is PostgreSQL."},
            scope=canonical_scope,
            source_id="alpha",
            meta={"memory_type": "fact", "force_capture": True},
        ))
        expected_ids = {canonical.record_id}
        if legacy_match:
            legacy = store.append(RecordEnvelope.create(
                kind="memory",
                title="Archived database configuration",
                summary="The previous application database was PostgreSQL.",
                content={"text": "The previous application database was PostgreSQL."},
                scope=ScopeRef(agent_id="main", workspace_id="repo-x", user_id="darrow"),
                source_id="alpha",
                meta={"memory_type": "fact", "force_capture": True},
            ))
            expected_ids.add(legacy.record_id)

        bundle = MemoryAPI(store).recall(
            query="PostgreSQL",
            scope=asdict(canonical_scope),
            task_context={"source_ids": ["alpha"], "scope_strategy": "canonical_first"},
            limit=3,
        )

        assert bundle.explanation["scope_fallback"] == "legacy_union"
        assert {item.record_id for item in bundle.items} == expected_ids
        assert bundle.items[0].record_id == canonical.record_id
        assert bundle.explanation["retrieval_status"] == "evidence_found"
