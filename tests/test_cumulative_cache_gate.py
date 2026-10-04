from copy import deepcopy
import pytest
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.recall.indexing import build_recall_index_document, clear_recall_index_document_cache
from eimemory.retrieval.diagnostics import _safe_proofs


def test_cache_tracks_partition_scope_and_same_second_payload_changes():
    clear_recall_index_document_cache()
    r = RecordEnvelope.create(kind="reflection", title="fixture", scope=ScopeRef(), content={"text": "before"})
    original = build_recall_index_document(r)
    other = deepcopy(r)
    other.source_id = "other"
    other.scope.user_id = "other-user"
    other.content["text"] = "after"
    actual = build_recall_index_document(other)
    assert actual == build_recall_index_document(other, use_cache=False)
    original.scope["user_id"] = "poisoned"
    assert build_recall_index_document(r).scope["user_id"] == r.scope.user_id
    r.content["text"] = "new same second text"
    assert build_recall_index_document(r) == build_recall_index_document(r, use_cache=False)


def test_diagnostic_proof_record_id_cannot_be_arbitrary_text():
    proof = {"record_id": "private text with spaces", "quote_digest": "a" * 64, "span_start": 0, "span_end": 1}
    assert _safe_proofs([proof]) == []
