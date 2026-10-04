from eimemory.models.records import RecordEnvelope, RecallBundle, ScopeRef, compact_record


def test_compact_source_identity_is_not_truncated_or_collapsed():
    records = [RecordEnvelope.create(kind="reflection", title="fixture", scope=ScopeRef(),
               source_id="a" * 96 + suffix) for suffix in ("b" * 32, "c" * 32)]
    payload = RecallBundle(records, [], [], 0.8, "fixture").to_compact_dict(limit=2)
    assert [item["source_id"] for item in payload["items"]] == [r.source_id for r in records]
    assert compact_record(records[0])["source_id"] == records[0].source_id
