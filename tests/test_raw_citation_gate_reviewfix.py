"""Generic role/authority/physical-row counterparts for the sole review repair.

Parent-run only. No original contract or first-candidate test is modified.
"""
from dataclasses import asdict
from time import perf_counter

import pytest

from eimemory.contracts.recall_boundary import exact_ref, scope_tuple
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.raw import boundary, citation_gate as gate, retrieval
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.sqlite_store import SqliteRecordStore


OWNER = ScopeRef(tenant_id="review-synthetic", agent_id="reader", workspace_id="lab", user_id="owner")
QUERY = "Where are Meridian deployment manifests stored?"
GENERIC = "The deployment workbook describes release procedures."
EXPLICIT = "Meridian deployment manifests are stored in the infrastructure repository."


@pytest.fixture
def store(tmp_path):
    instance = RuntimeStore(tmp_path / "store")
    try:
        yield instance
    finally:
        instance.close()


def add(store, text=GENERIC, *, source_id="public", scope=OWNER, status="active", record_id=None):
    record = RecordEnvelope.create(kind="raw_chunk", title="notebook", summary=text, detail=text,
        content={"raw_text": text, "text": text}, scope=scope, source_id=source_id, status=status)
    if record_id is not None:
        record.record_id = record_id
    return store.append(record)


def ask(store, *, query=QUERY, context=None, sources=None, intent="fact_citation"):
    task = {"rerank_with_llm": False, "enable_llm_rerank": False, **(context or {})}
    if intent is not None:
        task["evidence_intent"] = intent
    return retrieval.search_raw_chunks(store, query=query, scope=OWNER, source_ids=sources,
                                       task_context=task, limit=5)


def collect(store, *, owners=None, sources=None, check_budget=lambda: None):
    return store.read_raw_citation_records(owner_scopes=owners if owners is not None else (scope_tuple(OWNER),),
        source_ids=sources, kinds=("raw_chunk", "memory"), deadline=perf_counter() + 3.0,
        check_budget=check_budget)


@pytest.mark.parametrize("query,text", [
    ("Can our team write commit messages in Italian?", "Code comments and commit messages must be written in English."),
    ("Can Aster commit messages be written in German?", "Aster code comments and commit messages must be written in English."),
    ("Can the deployment run on Neptune?", "Every deployment runs only on the approved local server."),
    ("May the archive use Postgres?", "The archive uses only the embedded database."),
    ("Are CSV attachments permitted?", "Attachments must be PDF documents; all other formats are excluded."),
    ('Can exports use "Ruby" format?', "Exports must use the documented format only."),
    ('Is "Operator" a permitted role?', "The only permitted role is reader."),
    ('“紫色”可以作为界面颜色吗？', "界面颜色只允许黑色和白色。"),
])
def test_proposed_values_do_not_override_generic_or_closed_set_rules(store, query, text):
    add(store, text)
    baseline = ask(store, query=query, intent=None)
    actual = ask(store, query=query)
    assert baseline and list(actual) == baseline
    assert actual.decision.status == "related"


@pytest.mark.parametrize("query", [QUERY, 'Where is "Meridian" deployed?', "When is Meridian deployment scheduled?"])
def test_missing_literal_in_open_subject_slot_still_rejects(store, query):
    add(store)
    actual = ask(store, query=query)
    assert actual == [] and actual.decision.status == "reject"
    assert actual.decision.reason in {"missing_named_literal", "missing_quoted_literal"}


@pytest.mark.parametrize("context_sources,argument_sources", [
    (["public"], ["public", "restricted"]),
    (["public", "restricted"], ["public"]),
    (["ＰＵＢＬＩＣ"], None),
])
def test_each_source_ceiling_intersects_before_both_snapshots_and_collection(store, monkeypatch, context_sources, argument_sources):
    add(store)
    add(store, EXPLICIT, source_id="restricted")
    seen = []
    original = store.read_raw_citation_records
    def observed(**kwargs):
        seen.append(kwargs["source_ids"])
        return original(**kwargs)
    monkeypatch.setattr(store, "read_raw_citation_records", observed)
    context = {"source_ids": context_sources}
    actual = ask(store, context=context, sources=argument_sources)
    assert actual == [] and actual.decision.reason == "missing_named_literal"
    assert actual.decision.source_ids == ("public",) and "meridian" in actual.decision.missing_terms
    assert seen == [("public",), ("public",)]
    assert context == {"source_ids": context_sources}


def test_enclosing_source_boundary_is_an_additional_ceiling(store):
    add(store)
    add(store, EXPLICIT, source_id="restricted")
    outer = boundary._Boundary(None, frozenset({"public"}), perf_counter() + 3.0)
    token = boundary._CURRENT.set(outer)
    try:
        result = ask(store, context={"source_ids": ["public", "restricted"]}, sources=["public", "restricted"])
        assert result == [] and result.decision.source_ids == ("public",)
    finally:
        boundary._CURRENT.reset(token)


def test_disjoint_source_ceilings_do_not_read_or_fall_back_to_all_sources(store, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("disjoint ceilings reached storage/collection")
    monkeypatch.setattr(store, "read_raw_citation_records", forbidden)
    monkeypatch.setattr(retrieval, "_search_raw_candidates", forbidden)
    result = ask(store, context={"source_ids": ["public"]}, sources=["restricted"])
    assert result == [] and result.decision.reason == "no_authorized_sources"
    assert not result.decision.complete


@pytest.mark.parametrize("context,sources", [
    ({"source_ids": "public"}, ["public"]),
    ({"source_ids": {"public": True}}, None),
    ({"source_ids": ["public", "ＰＵＢＬＩＣ"]}, None),
    ({"source_ids": [None]}, None),
    ({}, "public"),
    ({}, {"public"}),
    ({"kinds": "memory"}, None),
    ({"kinds": []}, None),
    ({"kinds": None}, None),
    ({"kinds": ["unknown-kind"]}, None),
    ({"kinds": [None]}, None),
])
def test_invalid_fact_authority_is_unavailable_before_io(store, monkeypatch, context, sources):
    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid authority reached storage/collection")
    monkeypatch.setattr(store, "read_raw_citation_records", forbidden)
    monkeypatch.setattr(retrieval, "_search_raw_candidates", forbidden)
    with pytest.raises(gate.CitationUnavailable) as caught:
        ask(store, context=context, sources=sources)
    assert caught.value.decision.reason == "invalid_authority_filter"
    assert not caught.value.decision.complete


def test_whitespace_kind_is_normalized_in_snapshot_and_unchanged_raw_body(store, monkeypatch):
    add(store, EXPLICIT)
    seen = []
    original = retrieval._search_raw_candidates
    def observed(*args, **kwargs):
        seen.append(kwargs["task_context"]["kinds"])
        return original(*args, **kwargs)
    monkeypatch.setattr(retrieval, "_search_raw_candidates", observed)
    result = ask(store, context={"kinds": [" raw_chunk "]})
    assert result and result.decision.kinds == ("raw_chunk",)
    assert seen == [("raw_chunk",)]


@pytest.mark.parametrize("context", [{"kinds": "memory"}, {"source_ids": "invalid-legacy-shape"}])
def test_default_raw_does_not_enter_new_filter_validation(store, monkeypatch, context):
    add(store, EXPLICIT)
    def forbidden(*_args, **_kwargs):
        pytest.fail("default RAW entered fact input validation")
    monkeypatch.setattr(gate, "_fact_authority_inputs", forbidden)
    task = {"rerank_with_llm": False, "enable_llm_rerank": False, **context}
    expected = retrieval.search_raw_chunks.__wrapped__(store, query=QUERY, scope=OWNER, task_context=task, limit=5)
    assert ask(store, context=context, intent=None) == expected


@pytest.mark.parametrize("field,value", [("record_id", " target"), ("record_id", "target\t"),
                                          ("source_id", "PUBLIC"), ("source_id", "ｐｕｂｌｉｃ")])
def test_noncanonical_physical_reference_never_reaches_shared_getter(store, monkeypatch, field, value):
    record = add(store)
    add(store, "excluded target", status="withdrawn", record_id="target")
    with store.locked() as sqlite:
        sqlite.execute(f"UPDATE records SET {field}=? WHERE record_id=?", (value, record.record_id))
        sqlite.commit()
    calls = []
    original = SqliteRecordStore.get_by_exact_ref
    def observed(self, record_id, *args, **kwargs):
        calls.append(record_id)
        return original(self, record_id, *args, **kwargs)
    monkeypatch.setattr(SqliteRecordStore, "get_by_exact_ref", observed)
    with pytest.raises(gate.CitationUnavailable) as caught:
        collect(store)
    assert caught.value.decision.reason == "invalid_physical_reference" and calls == []


@pytest.mark.parametrize("tenant", ["", "tenant\x1falias"])
def test_scope_normalization_collision_is_rejected_before_hydration(store, monkeypatch, tenant):
    add(store)
    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid owner reached hydration")
    monkeypatch.setattr(SqliteRecordStore, "get_by_exact_ref", forbidden)
    owner = (tenant, OWNER.agent_id, OWNER.workspace_id, OWNER.user_id)
    with pytest.raises(gate.CitationUnavailable) as caught:
        collect(store, owners=(owner,))
    assert caught.value.decision.reason == "invalid_authority_filter"


def test_duplicate_physical_ref_cannot_select_excluded_unpreflighted_payload(store, monkeypatch):
    record = add(store)
    with store.locked() as sqlite:
        columns = [row[1] for row in sqlite.execute("PRAGMA table_info(records)").fetchall()]
        expressions = ["?" if name in {"storage_key", "status", "payload_json"} else name for name in columns]
        # A malformed second key has the same canonical ref but is not active.
        sqlite.execute("INSERT INTO records (" + ",".join(columns) + ") SELECT " + ",".join(expressions)
            + " FROM records WHERE record_id=?", ("malformed-duplicate", "withdrawn", "x" * 8192, record.record_id))
        sqlite.commit()
    def forbidden(*_args, **_kwargs):
        pytest.fail("ambiguous reference reached unpreflighted hydration")
    monkeypatch.setattr(SqliteRecordStore, "get_by_exact_ref", forbidden)
    with pytest.raises(gate.CitationUnavailable) as caught:
        collect(store)
    assert caught.value.decision.reason == "invalid_physical_reference"


@pytest.mark.parametrize("column", ["storage_key", "record_id", "source_id", "payload_digest", "payload_pointer_json"])
def test_oversized_metadata_is_not_returned_to_python_before_validation(store, column):
    record = add(store)
    observed = []
    with store.locked() as sqlite:
        sqlite.execute(f"UPDATE records SET {column}=? WHERE record_id=?", ("x" * 8192, record.record_id))
        sqlite.commit()
        original = sqlite.conn.row_factory
        def observer(cursor, row):
            names = [item[0] for item in cursor.description]
            if "inline_bytes" in names:
                observed.extend((name, len(value.encode("utf-8"))) for name, value in zip(names, row)
                                if isinstance(value, str) and len(value.encode("utf-8")) >= 8192)
            return original(cursor, row) if original else row
        sqlite.conn.row_factory = observer
        try:
            with pytest.raises(gate.CitationUnavailable):
                collect(store)
        finally:
            sqlite.conn.row_factory = original
        assert not sqlite.in_transaction
    assert observed == []


def test_legitimate_maximum_unicode_scope_and_ascii_ids_are_not_truncated(store):
    owner = ScopeRef(tenant_id="🧭" * 256, agent_id="🧪" * 256, workspace_id="界" * 256, user_id=" " + "a" * 254 + " ")
    record = add(store, EXPLICIT, scope=owner, source_id="s" * 128, record_id="r" * 256)
    rows = collect(store, owners=(scope_tuple(owner),), sources=("s" * 128,))
    assert len(rows) == 1 and exact_ref(rows[0]) == exact_ref(record)


@pytest.mark.parametrize("failure", ["metadata", "hydrate", "budget"])
def test_failed_pooled_statistics_release_slot_transaction_and_deadline_state(store, monkeypatch, failure):
    record = add(store, EXPLICIT)
    monkeypatch.setattr(store, "_reader_count", lambda: 1)
    with store.borrow_reader() as slot:
        raw_connection = slot.store.conn
        busy_timeout = raw_connection.execute("PRAGMA busy_timeout").fetchone()[0]
    if failure == "metadata":
        with store.locked() as sqlite:
            sqlite.execute("UPDATE records SET payload_digest=? WHERE record_id=?", ("x" * 8192, record.record_id))
            sqlite.commit()
    with monkeypatch.context() as temporary:
        if failure == "hydrate":
            def failed(*_args, **_kwargs):
                raise RuntimeError("synthetic hydration failure")
            temporary.setattr(SqliteRecordStore, "get_by_exact_ref", failed)
        checks = []
        def check_budget():
            checks.append(True)
            if failure == "budget" and len(checks) == 4:
                raise gate.CitationUnavailable("cpu_limit")
        with pytest.raises((gate.CitationUnavailable, RuntimeError)):
            collect(store, check_budget=check_budget)
    assert store._readers and all(not reader.in_use for reader in store._readers)
    with store.borrow_reader() as slot:
        assert slot.store.conn is raw_connection
        assert not slot.store.in_transaction
        assert slot.store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == busy_timeout
        assert getattr(slot.store, "_recall_read_progress", None) is None
    if failure == "metadata":
        store.append(record)
    assert collect(store)
