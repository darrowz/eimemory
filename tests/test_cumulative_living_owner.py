from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.living.operations import enrich_memory_records, _record_digest
from eimemory.living.posture import compile_living_posture_report


def test_enrichment_does_not_rewrite_shared_scope_and_hashes_content(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        shared = ScopeRef(user_id="")
        user = ScopeRef(user_id="fixture-user")
        record = RecordEnvelope.create(kind="memory", title="fixture", scope=shared, content={"text": "fixture preference"})
        runtime.store.append(record)
        result = enrich_memory_records(runtime, scope=user)
        assert result["enriched_count"] == 0
        before = _record_digest(record)
        record.content["text"] = "changed"
        assert _record_digest(record) != before
    finally:
        runtime.close()


def test_posture_fallback_cannot_reintroduce_internal_audit(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        scope = ScopeRef()
        record = RecordEnvelope.create(kind="memory", title="fixture audit", scope=scope,
                  content={"text": "fixture audit evidence"}, meta={"memory_type": "audit"})
        runtime.store.append(record)
        result = compile_living_posture_report(runtime, query="fixture audit", scope=scope, limit=5)
        assert result["record_count"] == 0
    finally:
        runtime.close()
