from eimemory.models.records import RecordEnvelope, ScopeRef, VALID_KINDS


def test_raw_chunk_is_a_valid_record_kind() -> None:
    assert "raw_chunk" in VALID_KINDS
    record = RecordEnvelope.create(
        kind="raw_chunk",
        title="Raw chunk sess-1#0",
        summary="User said they prefer PostgreSQL.",
        detail="User said they prefer PostgreSQL because backups are easier.",
        content={
            "text": "User said they prefer PostgreSQL because backups are easier.",
            "session_id": "sess-1",
            "chunk_index": 0,
            "raw_text_hash": "hash",
        },
        scope=ScopeRef(agent_id="hongtu", workspace_id="embodied"),
        source="eimemory.raw.ingest",
    )

    assert record.kind == "raw_chunk"
    assert record.content["session_id"] == "sess-1"


def test_chunk_text_is_deterministic_and_overlapping() -> None:
    from eimemory.raw.chunks import chunk_text

    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    chunks = chunk_text(
        text,
        session_id="sess-1",
        source_event_id="event-1",
        role="user",
        speaker="alice",
        max_chars=24,
        overlap_chars=6,
    )

    assert [chunk["chunk_index"] for chunk in chunks] == list(range(len(chunks)))
    assert chunks[0]["session_id"] == "sess-1"
    assert chunks[0]["source_event_id"] == "event-1"
    assert chunks[0]["role"] == "user"
    assert chunks[0]["speaker"] == "alice"
    assert chunks[0]["text"]
    assert chunks[1]["text"].startswith(chunks[0]["text"][-6:].strip()[:1])
    assert chunk_text(text, session_id="sess-1", source_event_id="event-1") == chunk_text(
        text,
        session_id="sess-1",
        source_event_id="event-1",
    )


def test_raw_text_hash_is_stable() -> None:
    from eimemory.raw.chunks import raw_text_hash

    assert raw_text_hash("  hello\nworld  ") == raw_text_hash("hello world")


def test_raw_evidence_api_persists_chunks_and_context_window(tmp_path) -> None:
    from eimemory.raw.store import RawEvidenceAPI
    from eimemory.storage.runtime_store import RuntimeStore

    store = RuntimeStore(root=tmp_path)
    raw = RawEvidenceAPI(store)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied"}

    records = raw.ingest_text(
        text="first message. second message. third message.",
        scope=scope,
        source_event_id="event-1",
        session_id="sess-1",
        role="user",
        speaker="alice",
        max_chars=18,
        overlap_chars=4,
    )

    assert len(records) >= 2
    assert all(record.kind == "raw_chunk" for record in records)
    assert records[0].title == "Raw chunk sess-1#0"
    assert records[0].summary == records[0].detail[:240]
    assert records[0].content["source_event_id"] == "event-1"
    assert records[0].content["source_type"] == "conversation"
    assert records[0].content["session_id"] == "sess-1"
    assert records[0].content["role"] == "user"
    assert records[0].content["speaker"] == "alice"
    assert records[0].tags == ["raw-evidence", "conversation"]
    assert records[0].source == "eimemory.raw.ingest"
    assert records[0].meta["evidence_layer"] == "raw"
    assert records[0].meta["granularity"] == "chunk"
    assert records[0].meta["token_estimate"] >= 1
    assert records[0].content["next_chunk_id"] == records[1].record_id
    assert records[1].content["prev_chunk_id"] == records[0].record_id
    assert store.get_by_id(records[0].record_id, scope=scope) is not None

    window = raw.context_window(records[1].record_id, scope=scope, radius=1)

    assert [item.record_id for item in window] == [
        records[0].record_id,
        records[1].record_id,
        records[2].record_id,
    ]
    assert [item.content["chunk_index"] for item in window] == [0, 1, 2]


def test_raw_chunk_search_matches_raw_identifiers_and_text(tmp_path) -> None:
    from eimemory.raw.store import RawEvidenceAPI
    from eimemory.storage.runtime_store import RuntimeStore

    store = RuntimeStore(root=tmp_path)
    raw = RawEvidenceAPI(store)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied"}
    chunk = raw.ingest_text(
        text="The user prefers PostgreSQL for durable backups.",
        scope=scope,
        source_event_id="event-2",
        session_id="sess-2",
        turn_id="turn-7",
    )[0]

    by_record_id = store.search(query=chunk.record_id, kinds=["raw_chunk"], scope=scope, limit=5)
    by_session_id = store.search(query="sess-2", kinds=["raw_chunk"], scope=scope, limit=5)
    by_turn_id = store.search(query="turn-7", kinds=["raw_chunk"], scope=scope, limit=5)
    by_source_event = store.search(query="event-2", kinds=["raw_chunk"], scope=scope, limit=5)
    by_hash = store.search(query=chunk.content["raw_text_hash"], kinds=["raw_chunk"], scope=scope, limit=5)
    by_text = store.search(query="durable backups", kinds=["raw_chunk"], scope=scope, limit=5)

    assert by_record_id and by_record_id[0].record_id == chunk.record_id
    assert by_session_id and by_session_id[0].record_id == chunk.record_id
    assert by_turn_id and by_turn_id[0].record_id == chunk.record_id
    assert by_source_event and by_source_event[0].record_id == chunk.record_id
    assert by_hash and by_hash[0].record_id == chunk.record_id
    assert by_text and by_text[0].record_id == chunk.record_id


def test_runtime_exposes_raw_api_and_raw_api_searches_chunks(tmp_path) -> None:
    from eimemory.api.runtime import Runtime
    from eimemory.raw.store import RawEvidenceAPI

    runtime = Runtime.create(root=tmp_path)
    assert isinstance(runtime.raw, RawEvidenceAPI)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied"}
    chunk = runtime.raw.ingest_text(
        text="Hong prefers extremely concise communication with no filler.",
        scope=scope,
        source_event_id="event-raw-runtime",
        session_id="sess-runtime",
    )[0]

    results = runtime.raw.search_raw_chunks(query="concise communication", scope=scope, limit=5)

    assert results
    assert results[0]["record"].record_id == chunk.record_id
    assert results[0]["base_score"] > 0
