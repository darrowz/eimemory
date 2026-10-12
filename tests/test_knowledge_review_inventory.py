"""Native bounded inventory, mutation reconciliation, and receipt isolation."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage import knowledge_review_inventory as inventory


SCOPE = ScopeRef(tenant_id="tenant", agent_id="agent", workspace_id="workspace", user_id="user")
OTHER = replace(SCOPE, user_id="other")
TARGET = "capability:v1"


@pytest.fixture
def store(tmp_path):
    value = RuntimeStore(tmp_path / "runtime")
    yield value
    value.close()


def _record(store, identifier, *, kind="claim_card", scope=SCOPE, source_id="paper", content=None,
            meta=None, source="test.inventory"):
    record = RecordEnvelope.create(kind=kind, title=f"Synthetic inventory {identifier}", summary="Controlled fixture",
        content=content or {}, scope=scope, source=source, source_id=source_id,
        status="archived" if kind == "reflection" and source == inventory._REVIEW_SOURCE else "active")
    record.record_id = identifier
    record.meta.update(meta or {})
    store.append(record)
    return record


def _row(store, identifier, *, scope=SCOPE):
    return store.read_consistent(lambda sql: inventory._compact(sql.execute(
        "SELECT " + inventory._ROW_COLUMNS + " FROM records WHERE record_id=? AND " + inventory._WHERE,
        (identifier, *inventory._scope(scope))).fetchone()))


def _entry(row, *, applicability="not_applicable", attempt=0, dirty=False, dependencies=None):
    digest = sha256(row["record_id"].encode()).hexdigest()
    return {**row, "input_digest": digest, "review_receipt_id": "review_" + digest[:32],
            "review_status": "completed", "dirty": dirty, "applicability": applicability,
            "attempt_count": attempt, "reason": "", "next_transition": "",
            "dependencies": dependencies or []}


def _upsert(store, scope, target, entry):
    # Completed manifest fixtures now include a genuine durable review receipt;
    # absence and conflicting receipts must fail the native terminal-proof gate.
    if not entry.get("dirty") and entry.get("review_status") == "completed":
        review_id = entry["review_receipt_id"]
        if store.get_by_id(review_id, scope=scope, exact_scope=True) is None:
            _record(store, review_id, kind="reflection", scope=scope, source=inventory._REVIEW_SOURCE,
                meta={"report_type": inventory._REVIEW_TYPE, "input_digest": entry["input_digest"], "review_status": "completed"},
                content={"schema": inventory._REVIEW_TYPE, "status": "completed", "input_digest": entry["input_digest"]})
    return inventory.upsert_manifest_entry(store, scope, target, entry)


def _write(store, statement, parameters=()):
    def run(sql):
        sql.execute(statement, parameters)
        sql.commit()
    store.run_locked(run)


def test_lazy_install_sees_existing_generation_zero_and_reopens(store):
    _record(store, "claim_existing")
    assert inventory.current_watermark(store, SCOPE) == 0
    page = inventory.inventory_page(store, SCOPE)
    assert [row["record_id"] for row in page["rows"]] == ["claim_existing"]
    assert page["watermark"] == 0 and page["complete"]
    _record(store, "claim_new")
    high = inventory.current_watermark(store, SCOPE)
    assert high > 0
    root = store.root
    second = RuntimeStore(root)
    try:
        assert inventory.current_watermark(second, SCOPE) == high
        assert inventory.count_inputs(second, SCOPE) == 2
    finally:
        second.close()


def test_keyset_pages_over_200_never_select_bodies_or_metadata(store):
    # Native rows share fixed timestamp; neither updated_at nor an offset can
    # supply a stable tie breaker or accidentally truncate the inventory.
    first = _record(store, "claim_000")
    def seed(sql):
        template = sql.execute("SELECT * FROM records WHERE record_id=?", (first.record_id,)).fetchone()
        columns = list(template.keys())
        insert = "INSERT INTO records(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")"
        for number in range(1, 451):
            values = dict(template)
            values["record_id"] = f"claim_{number:03}"
            values["storage_key"] = "\x1f".join((*inventory._scope(SCOPE), values["record_id"]))
            sql.execute(insert, tuple(values[key] for key in columns))
        sql.commit()
    store.run_locked(seed)
    inventory.ensure(store)
    statements = []
    original = store.read_consistent
    def capture(reader):
        def wrapped(sql):
            sql.conn.set_trace_callback(statements.append)
            try:
                return reader(sql)
            finally:
                sql.conn.set_trace_callback(None)
        return original(wrapped)
    store.read_consistent = capture
    cursor, seen, sizes = "", [], []
    while True:
        page = inventory.inventory_page(store, SCOPE, cursor=cursor)
        seen.extend(row["record_id"] for row in page["rows"])
        sizes.append(page["page_count"])
        cursor = page["cursor"]
        if page["complete"]:
            break
    assert sizes == [200, 200, 51]
    assert len(seen) == len(set(seen)) == 451
    record_reads = [sql for sql in statements if "FROM records" in sql]
    assert record_reads and all("payload_json" not in sql and "meta_json" not in sql and " OFFSET " not in sql for sql in record_reads)
    with pytest.raises(inventory.KnowledgeReviewInventoryError, match="page_limit_invalid"):
        inventory.inventory_page(store, SCOPE, limit=201)


def test_concurrent_low_key_insertion_and_mutation_reconcile_without_offset(store):
    for name in ("claim_b", "claim_c", "claim_d"):
        _record(store, name)
    start = inventory.current_watermark(store, SCOPE)
    first = inventory.inventory_page(store, SCOPE, limit=2)
    go, done = Event(), Event()
    def writer():
        go.wait()
        _record(store, "claim_a")
        _write(store, "UPDATE records SET status='archived' WHERE record_id='claim_b'")
        done.set()
    thread = Thread(target=writer)
    thread.start()
    go.set()
    assert done.wait(5)
    thread.join()
    second = inventory.inventory_page(store, SCOPE, cursor=first["cursor"], limit=2)
    assert [row["record_id"] for row in second["rows"]] == ["claim_d"]
    inventory.add_inventory_entries(store, SCOPE, TARGET, first["rows"] + second["rows"])
    high = inventory.current_watermark(store, SCOPE)
    changes = inventory.changes_page(store, SCOPE, after_version=start, through_version=high)
    inventory.apply_changes(store, SCOPE, TARGET, changes["changes"])
    entries = inventory.manifest_page(store, SCOPE, TARGET)["rows"]
    assert [row["record_id"] for row in entries] == ["claim_a", "claim_b", "claim_c", "claim_d"]
    assert entries[1]["status"] == "archived"


def test_update_scope_kind_and_partition_log_old_and_new_identities(store):
    inventory.ensure(store)
    _record(store, "claim_move")
    initial = inventory.current_watermark(store, SCOPE)
    _write(store, "UPDATE records SET user_id=?,source_id=?,kind=?,status=? WHERE record_id=?",
           (OTHER.user_id, "new_partition", "paper_source", "archived", "claim_move"))
    old = inventory.changes_page(store, SCOPE, after_version=initial,
        through_version=inventory.current_watermark(store, SCOPE))["changes"]
    new = inventory.changes_page(store, OTHER, after_version=0,
        through_version=inventory.current_watermark(store, OTHER))["changes"]
    assert [(row["operation"], row["kind"], row["source_id"]) for row in old] == [("before_update", "claim_card", "paper")]
    assert [(row["operation"], row["kind"], row["source_id"]) for row in new] == [("after_update", "paper_source", "new_partition")]
    high = inventory.current_watermark(store, SCOPE)
    _record(store, "claim_other", scope=OTHER)
    assert inventory.current_watermark(store, SCOPE) == high
    _write(store, "DELETE FROM records WHERE record_id='claim_move'")
    assert inventory.changes_page(store, OTHER, after_version=new[-1]["version"],
        through_version=inventory.current_watermark(store, OTHER))["changes"][-1]["operation"] == "delete"


def test_bounded_changes_page_freezes_through_watermark(store):
    inventory.ensure(store)
    _record(store, "claim_log")
    def mutate(sql):
        for number in range(125):
            sql.execute("UPDATE records SET status=? WHERE record_id='claim_log'", ("active" if number % 2 else "archived",))
        sql.commit()
    store.run_locked(mutate)
    high = inventory.current_watermark(store, SCOPE)
    first = inventory.changes_page(store, SCOPE, after_version=0, through_version=high)
    assert len(first["changes"]) == 200 and not first["complete"]
    _record(store, "claim_after")
    second = inventory.changes_page(store, SCOPE, after_version=first["cursor"], through_version=high)
    assert len(second["changes"]) == 51 and second["complete"]
    assert second["cursor"] == high and all(row["record_id"] != "claim_after" for row in second["changes"])


def test_review_insert_is_excluded_but_mutation_delete_and_revocation_tracked(store):
    inventory.ensure(store)
    review = _record(store, "review_1", kind="reflection", source=inventory._REVIEW_SOURCE,
        meta={"report_type": inventory._REVIEW_TYPE, "input_digest": "a" * 64},
        content={"schema": inventory._REVIEW_TYPE, "status": "completed"})
    _record(store, "checkpoint_1", kind="reflection", source=inventory._REVIEW_SOURCE,
        meta={"report_type": "knowledge.review_checkpoint.v1"})
    assert inventory.current_watermark(store, SCOPE) == 0
    _write(store, "UPDATE records SET status='archived' WHERE record_id=?", (review.record_id,))
    changes = inventory.changes_page(store, SCOPE, after_version=0,
        through_version=inventory.current_watermark(store, SCOPE))["changes"]
    assert [row["category"] for row in changes] == ["review", "review"]
    _write(store, "DELETE FROM records WHERE record_id=?", (review.record_id,))
    _record(store, "revoke_1", kind="reflection", source=inventory._REVIEW_SOURCE,
        meta={"report_type": inventory._REVOCATION_TYPE},
        content={"review_receipt_id": review.record_id})
    changes = inventory.changes_page(store, SCOPE, after_version=changes[-1]["version"],
        through_version=inventory.current_watermark(store, SCOPE))["changes"]
    assert [(row["operation"], row["category"]) for row in changes] == [("delete", "review"), ("insert", "revocation")]


def test_manifest_dependency_update_invalidates_and_delete_removes_exact_entry(store):
    claim = _record(store, "claim_1")
    paper = _record(store, "paper_1", kind="paper_source")
    inventory.ensure(store)
    claim_row, paper_row = _row(store, claim.record_id), _row(store, paper.record_id)
    entry = _entry(claim_row, applicability="applicable", dependencies=[{
        key: paper_row[key] for key in ("storage_key", "record_id", "source_id", "payload_digest")}])
    assert _upsert(store, SCOPE, TARGET, entry)
    assert inventory.get_unique_applicable(store, SCOPE, TARGET)["record_id"] == claim.record_id
    high = inventory.current_watermark(store, SCOPE)
    _write(store, "UPDATE records SET payload_digest=? WHERE record_id=?", ("b" * 64, paper.record_id))
    changes = inventory.changes_page(store, SCOPE, after_version=high,
        through_version=inventory.current_watermark(store, SCOPE))["changes"]
    inventory.apply_changes(store, SCOPE, TARGET, changes)
    assert inventory.manifest_counts(store, SCOPE, TARGET)["dirty_count"] == 1
    assert inventory.get_unique_applicable(store, SCOPE, TARGET) is None
    high = inventory.current_watermark(store, SCOPE)
    _write(store, "DELETE FROM records WHERE record_id=?", (claim.record_id,))
    inventory.apply_changes(store, SCOPE, TARGET, inventory.changes_page(store, SCOPE,
        after_version=high, through_version=inventory.current_watermark(store, SCOPE))["changes"])
    assert inventory.manifest_counts(store, SCOPE, TARGET)["entry_count"] == 0


def test_stale_terminal_proof_does_not_clear_dirty_manifest(store):
    _record(store, "claim_race")
    row = _row(store, "claim_race")
    inventory.add_inventory_entries(store, SCOPE, TARGET, [row])
    _write(store, "UPDATE records SET payload_digest=? WHERE record_id='claim_race'", ("b" * 64,))
    assert not _upsert(store, SCOPE, TARGET, _entry(row, applicability="applicable"))
    entry = inventory.get_manifest_entry(store, SCOPE, TARGET, row["storage_key"])
    assert entry["dirty"] and entry["payload_digest"] == "b" * 64
    assert inventory.manifest_counts(store, SCOPE, TARGET)["applicable_count"] == 0


def test_repeated_enumeration_preserves_clean_review_and_fair_pending_order(store):
    for identifier in ("claim_a", "claim_b", "claim_c"):
        _record(store, identifier)
    rows = inventory.inventory_page(store, SCOPE)["rows"]
    inventory.add_inventory_entries(store, SCOPE, TARGET, rows)
    _upsert(store, SCOPE, TARGET, _entry(rows[0], attempt=3, dirty=True, applicability=""))
    _upsert(store, SCOPE, TARGET, _entry(rows[1], attempt=0, dirty=True, applicability=""))
    _upsert(store, SCOPE, TARGET, _entry(rows[2], applicability="applicable"))
    inventory.add_inventory_entries(store, SCOPE, TARGET, rows)
    first = inventory.manifest_page(store, SCOPE, TARGET, dirty_only=True, limit=1)
    assert first["rows"][0]["record_id"] == "claim_b" and not first["complete"]
    second = inventory.manifest_page(store, SCOPE, TARGET, dirty_only=True, cursor=first["cursor"], limit=1)
    assert second["rows"][0]["record_id"] == "claim_a" and second["complete"]
    assert not inventory.get_manifest_entry(store, SCOPE, TARGET, rows[2]["storage_key"])["dirty"]
    assert inventory.get_unique_applicable(store, SCOPE, TARGET) is None


def test_due_validity_transition_invalidates_clean_entry(store):
    _record(store, "claim_time")
    row = _row(store, "claim_time")
    entry = _entry(row, applicability="applicable")
    entry["next_transition"] = "2026-10-13T00:00:00Z"
    assert _upsert(store, SCOPE, TARGET, entry)
    assert inventory.mark_due_entries(store, SCOPE, TARGET, now="2026-10-12T00:00:00+00:00") == 0
    assert inventory.mark_due_entries(store, SCOPE, TARGET, now="2026-10-13T00:00:00+00:00") == 1
    assert inventory.get_unique_applicable(store, SCOPE, TARGET) is None


def test_scan_state_cas_persistence_and_caller_transaction_is_not_committed(store):
    assert inventory.save_scan_state(store, SCOPE, TARGET, {"cursor": "claim_a"}, expected_revision=0) == 1
    assert inventory.load_scan_state(store, SCOPE, TARGET) == {"cursor": "claim_a", "_revision": 1}
    with pytest.raises(inventory.KnowledgeReviewInventoryError, match="state_conflict"):
        inventory.save_scan_state(store, SCOPE, TARGET, {"cursor": "bad"}, expected_revision=0)
    assert inventory.save_scan_state(store, SCOPE, TARGET, {"cursor": "claim_b"}, expected_revision=1) == 2
    def outer(sql):
        sql.execute("BEGIN")
        sql.execute("INSERT INTO knowledge_review_inventory_scan_state VALUES(?,?,?,?,?,?,?)",
            (*inventory._scope(SCOPE), "other_target", "{}", 1))
        with pytest.raises(inventory.KnowledgeReviewInventoryError, match="requires_own_transaction"):
            inventory.save_scan_state(store, SCOPE, TARGET, {"cursor": "never"})
        assert sql.in_transaction
        sql.rollback()
    store.run_locked(outer)
    assert inventory.load_scan_state(store, SCOPE, "other_target") is None
    assert inventory.load_scan_state(store, SCOPE, TARGET)["cursor"] == "claim_b"


@pytest.mark.parametrize("mutation", [
    "DROP TRIGGER knowledge_review_inventory_insert",
    "ALTER TABLE knowledge_review_inventory_changes ADD COLUMN unsafe TEXT",
    "UPDATE knowledge_review_inventory_schema SET schema='corrupt'",
])
def test_corrupted_owned_schema_fails_closed_without_repair(store, mutation):
    inventory.ensure(store)
    _write(store, mutation)
    with pytest.raises(inventory.KnowledgeReviewInventoryError, match="schema_invalid"):
        inventory.current_watermark(store, SCOPE)


def test_exact_indexed_review_lookup_and_legacy_revocation(store):
    digest = "a" * 64
    # Scope-wide newer or foreign source receipts must not consume our limit.
    for number in range(5):
        _record(store, f"review_other_{number}", kind="reflection", scope=OTHER,
            source=inventory._REVIEW_SOURCE, meta={"report_type": inventory._REVIEW_TYPE, "input_digest": digest},
            content={"schema": inventory._REVIEW_TYPE, "status": "completed"})
    _record(store, "review_exact", kind="reflection", source=inventory._REVIEW_SOURCE,
        meta={"report_type": inventory._REVIEW_TYPE, "input_digest": digest},
        content={"schema": inventory._REVIEW_TYPE, "status": "completed"})
    found = inventory.reviews_for_input(store, SCOPE, digest)
    assert [record.record_id for record in found] == ["review_exact"]
    assert not inventory.review_is_revoked(store, SCOPE, "review_exact")
    _record(store, "legacy_revocation", kind="reflection", source=inventory._REVIEW_SOURCE,
        meta={"report_type": inventory._REVOCATION_TYPE}, content={"review_receipt_id": "review_exact"})
    assert inventory.review_is_revoked(store, SCOPE, "review_exact")
    assert not inventory.review_is_revoked(store, OTHER, "review_exact")


def test_absent_dependency_can_close_ineligible_then_new_source_invalidates(store):
    _record(store, "claim_missing")
    row = _row(store, "claim_missing")
    missing = {"storage_key": "", "record_id": "paper_future", "source_id": "*", "payload_digest": ""}
    entry = _entry(row, applicability="", dependencies=[missing])
    entry.update(review_status="ineligible", reason="knowledge_source_missing")
    assert _upsert(store, SCOPE, TARGET, entry)
    counts = inventory.manifest_counts(store, SCOPE, TARGET)
    assert counts["unknown_count"] == counts["eligible_count"] == counts["dirty_count"] == 0
    high = inventory.current_watermark(store, SCOPE)
    _record(store, "paper_future", kind="paper_source", source_id="different_partition")
    inventory.apply_changes(store, SCOPE, TARGET, inventory.changes_page(store, SCOPE,
        after_version=high, through_version=inventory.current_watermark(store, SCOPE))["changes"])
    assert inventory.manifest_counts(store, SCOPE, TARGET)["dirty_count"] == 1
    assert not _upsert(store, SCOPE, TARGET, entry)


def test_clean_revoked_and_ineligible_are_terminal_without_model_verdict(store):
    for name in ("claim_applicable", "claim_ineligible", "claim_revoked"):
        _record(store, name)
    rows = inventory.inventory_page(store, SCOPE)["rows"]
    _upsert(store, SCOPE, TARGET, _entry(rows[0], applicability="applicable"))
    for row, status in zip(rows[1:], ("ineligible", "revoked")):
        entry = _entry(row, applicability="")
        entry["review_status"] = status
        assert _upsert(store, SCOPE, TARGET, entry)
    counts = inventory.manifest_counts(store, SCOPE, TARGET)
    assert counts["unknown_count"] == 0 and counts["completed_count"] == 1 and counts["applicable_count"] == 1
    assert inventory.get_unique_applicable(store, SCOPE, TARGET)["record_id"] == "claim_applicable"


def test_review_receipt_mutation_and_revocation_invalidate_manifest_without_insert_loop(store):
    _record(store, "claim_reviewed")
    row = _row(store, "claim_reviewed")
    inventory.ensure(store)
    _record(store, "review_reference", kind="reflection", source=inventory._REVIEW_SOURCE,
        meta={"report_type": inventory._REVIEW_TYPE, "input_digest": _entry(row)["input_digest"]},
        content={"schema": inventory._REVIEW_TYPE, "status": "completed", "input_digest": _entry(row)["input_digest"]})
    entry = _entry(row, applicability="applicable")
    entry["review_receipt_id"] = "review_reference"
    assert _upsert(store, SCOPE, TARGET, entry)
    high = inventory.current_watermark(store, SCOPE)
    _record(store, "new_review_other", kind="reflection", source=inventory._REVIEW_SOURCE,
        meta={"report_type": inventory._REVIEW_TYPE}, content={"schema": inventory._REVIEW_TYPE, "status": "completed"})
    assert inventory.current_watermark(store, SCOPE) == high
    receipt = store.get_by_id("review_reference", scope=SCOPE, exact_scope=True)
    receipt.status = "archived"
    store.run_locked(lambda sql: sql.upsert(receipt))
    changes = inventory.changes_page(store, SCOPE, after_version=high,
        through_version=inventory.current_watermark(store, SCOPE))["changes"]
    inventory.apply_changes(store, SCOPE, TARGET, changes)
    assert inventory.get_manifest_entry(store, SCOPE, TARGET, row["storage_key"])["dirty"]
    assert _upsert(store, SCOPE, TARGET, entry)
    high = inventory.current_watermark(store, SCOPE)
    _record(store, "revocation_ref", kind="reflection", source=inventory._REVIEW_SOURCE,
        meta={"report_type": inventory._REVOCATION_TYPE, "review_receipt_id": "review_reference"},
        content={"review_receipt_id": "review_reference"})
    inventory.apply_changes(store, SCOPE, TARGET, inventory.changes_page(store, SCOPE,
        after_version=high, through_version=inventory.current_watermark(store, SCOPE))["changes"])
    assert inventory.get_unique_applicable(store, SCOPE, TARGET) is None


def test_native_covering_inventory_index_and_reason_aggregates(store):
    _record(store, "claim_index")
    row = _row(store, "claim_index")
    entry = _entry(row, applicability="")
    entry.update(review_status="ineligible", reason="knowledge_source_missing")
    _upsert(store, SCOPE, TARGET, entry)
    assert inventory.manifest_reason_counts(store, SCOPE, TARGET) == {"knowledge_source_missing": 1}
    plan = store.read_consistent(lambda sql: sql.execute("EXPLAIN QUERY PLAN SELECT " + inventory._ROW_COLUMNS
        + " FROM records INDEXED BY knowledge_review_inventory_inputs WHERE " + inventory._WHERE
        + " AND kind IN ('claim_card','knowledge_page') AND storage_key>? ORDER BY storage_key LIMIT 201",
        (*inventory._scope(SCOPE), "")).fetchall())
    assert any("COVERING INDEX knowledge_review_inventory_inputs" in str(item["detail"]) for item in plan)
    assert all("TEMP B-TREE" not in str(item["detail"]) for item in plan)


def test_change_history_floor_never_treated_as_completed_reconciliation(store):
    inventory.ensure(store)
    _record(store, "claim_floor")
    high = inventory.current_watermark(store, SCOPE)
    _write(store, "UPDATE knowledge_review_inventory_watermarks SET floor=? WHERE " + inventory._WHERE,
        (high, *inventory._scope(SCOPE)))
    with pytest.raises(inventory.KnowledgeReviewInventoryError, match="history_gap"):
        inventory.changes_page(store, SCOPE, after_version=0, through_version=high)


def test_missing_change_entry_fails_closed_without_unbounded_log_scan(store):
    inventory.ensure(store)
    _record(store, "claim_chain_a")
    _record(store, "claim_chain_b")
    _record(store, "claim_chain_c")
    high = inventory.current_watermark(store, SCOPE)
    _write(store, "DELETE FROM knowledge_review_inventory_changes WHERE record_id='claim_chain_b'")
    with pytest.raises(inventory.KnowledgeReviewInventoryError, match="history_gap"):
        inventory.changes_page(store, SCOPE, after_version=0, through_version=high)
    # A fresh corpus enumeration can safely start at the current high-water;
    # its certificate does not claim to have reconstructed the deleted log.
    assert inventory.changes_page(store, SCOPE, after_version=high, through_version=high)["complete"]


def test_second_completed_review_from_other_process_invalidates_all_target_manifests(store):
    _record(store, "claim_concurrent")
    row = _row(store, "claim_concurrent")
    entry = _entry(row, applicability="applicable")
    assert _upsert(store, SCOPE, TARGET, entry)
    assert _upsert(store, SCOPE, "another_target:v1", entry)
    high = inventory.current_watermark(store, SCOPE)
    assert high == 0  # First completion inserts do not invalidate their own scan.
    # A second worker commits the conflicting receipt then exits immediately;
    # it never performs a manifest write or finishes the upper-level scan.
    program = (
        "import json,os,sys\n"
        "request=json.load(sys.stdin)\n"
        "sys.path.insert(0,request['repo'])\n"
        "from eimemory.storage.runtime_store import RuntimeStore\n"
        "from eimemory.models.records import RecordEnvelope,ScopeRef\n"
        "store=RuntimeStore(request['root'])\n"
        "record=RecordEnvelope.create(kind='reflection',status='archived',title='Controlled conflicting review',"
        "scope=ScopeRef(**request['scope']),source=request['source'],"
        "content={'schema':request['schema'],'status':'completed','input_digest':request['digest']},"
        "meta={'report_type':request['schema'],'input_digest':request['digest'],'review_status':'completed'})\n"
        "record.record_id='review_concurrent_worker'\n"
        "store.append(record)\n"
        "os._exit(0)\n"
    )
    request = {"repo": str(Path(inventory.__file__).resolve().parents[2]), "root": str(store.root),
        "scope": row["scope"], "source": inventory._REVIEW_SOURCE,
        "schema": inventory._REVIEW_TYPE, "digest": entry["input_digest"]}
    result = subprocess.run([sys.executable, "-I", "-B", "-c", program], input=json.dumps(request),
        text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert inventory.current_watermark(store, SCOPE) > high
    for target in (TARGET, "another_target:v1"):
        assert inventory.get_manifest_entry(store, SCOPE, target, row["storage_key"])["dirty"]
        assert inventory.get_unique_applicable(store, SCOPE, target) is None
    changes = inventory.changes_page(store, SCOPE, after_version=high,
        through_version=inventory.current_watermark(store, SCOPE))["changes"]
    assert [(change["category"], change["record_id"]) for change in changes] == [("review", "review_concurrent_worker")]


def test_conflict_before_first_manifest_upsert_cannot_seal_old_lookup(store):
    _record(store, "claim_before_manifest")
    row = _row(store, "claim_before_manifest")
    entry = _entry(row, applicability="applicable")
    inventory.ensure(store)
    _record(store, entry["review_receipt_id"], kind="reflection", source=inventory._REVIEW_SOURCE,
        meta={"report_type": inventory._REVIEW_TYPE, "input_digest": entry["input_digest"], "review_status": "completed"},
        content={"schema": inventory._REVIEW_TYPE, "status": "completed"})
    assert [review.record_id for review in inventory.reviews_for_input(store, SCOPE, entry["input_digest"])] == [entry["review_receipt_id"]]
    second = RuntimeStore(store.root)
    try:
        _record(second, "review_raced", kind="reflection", source=inventory._REVIEW_SOURCE,
            meta={"report_type": inventory._REVIEW_TYPE, "input_digest": entry["input_digest"], "review_status": "completed"},
            content={"schema": inventory._REVIEW_TYPE, "status": "completed"})
    finally:
        second.close()
    assert not inventory.upsert_manifest_entry(store, SCOPE, TARGET, entry)
    assert inventory.get_manifest_entry(store, SCOPE, TARGET, row["storage_key"])["dirty"]
    assert inventory.get_unique_applicable(store, SCOPE, TARGET) is None


def test_completed_terminal_proof_must_bind_the_only_exact_receipt(store):
    _record(store, "claim_receipt_identity")
    row = _row(store, "claim_receipt_identity")
    entry = _entry(row, applicability="applicable")
    assert _upsert(store, SCOPE, TARGET, entry)
    wrong = {**entry, "review_receipt_id": "review_unrelated"}
    assert not inventory.upsert_manifest_entry(store, SCOPE, TARGET, wrong)
    assert inventory.get_manifest_entry(store, SCOPE, TARGET, row["storage_key"])["dirty"]


def test_review_lookup_overflow_fails_before_any_body_hydration(store, monkeypatch):
    for number in range(4):
        _record(store, f"review_unavailable_{number}", kind="reflection", source=inventory._REVIEW_SOURCE,
            meta={"report_type": inventory._REVIEW_TYPE, "input_digest": "a" * 64},
            content={"schema": inventory._REVIEW_TYPE, "status": "unavailable"})
    inventory.ensure(store)
    from eimemory.storage.sqlite_store import SqliteRecordStore
    monkeypatch.setattr(SqliteRecordStore, "get_by_id", lambda *_args, **_kwargs: pytest.fail("overflow must not hydrate bodies"))
    with pytest.raises(inventory.KnowledgeReviewInventoryError, match="inventory_review_lookup_truncated"):
        inventory.reviews_for_input(store, SCOPE, "a" * 64)


def test_unavailable_review_updated_to_completed_atomically_invalidates_existing_proof(store):
    _record(store, "claim_review_update")
    row = _row(store, "claim_review_update")
    entry = _entry(row, applicability="applicable")
    assert _upsert(store, SCOPE, TARGET, entry)
    _record(store, "review_was_unavailable", kind="reflection", source=inventory._REVIEW_SOURCE,
        meta={"report_type": inventory._REVIEW_TYPE, "input_digest": entry["input_digest"], "review_status": "unavailable"},
        content={"schema": inventory._REVIEW_TYPE, "status": "unavailable"})
    high = inventory.current_watermark(store, SCOPE)
    assert not inventory.get_manifest_entry(store, SCOPE, TARGET, row["storage_key"])["dirty"]
    updated = store.get_by_id("review_was_unavailable", scope=SCOPE, exact_scope=True)
    updated.content["status"] = "completed"
    updated.meta["review_status"] = "completed"
    store.run_locked(lambda sql: sql.upsert(updated))
    assert inventory.current_watermark(store, SCOPE) > high
    assert inventory.get_manifest_entry(store, SCOPE, TARGET, row["storage_key"])["dirty"]
    assert not inventory.upsert_manifest_entry(store, SCOPE, TARGET, entry)


def test_foreign_scope_completed_insert_cannot_dirty_exact_manifest(store):
    _record(store, "claim_scope_isolation")
    row = _row(store, "claim_scope_isolation")
    entry = _entry(row, applicability="applicable")
    assert _upsert(store, SCOPE, TARGET, entry)
    high = inventory.current_watermark(store, SCOPE)
    _record(store, "review_foreign_scope", kind="reflection", scope=OTHER, source=inventory._REVIEW_SOURCE,
        meta={"report_type": inventory._REVIEW_TYPE, "input_digest": entry["input_digest"], "review_status": "completed"},
        content={"schema": inventory._REVIEW_TYPE, "status": "completed"})
    assert inventory.current_watermark(store, SCOPE) == high
    assert inventory.get_unique_applicable(store, SCOPE, TARGET)["review_receipt_id"] == entry["review_receipt_id"]


def test_legacy_segmented_review_without_inline_status_cannot_hide_conflicting_completion(store):
    from eimemory.storage.jsonl import canonical_payload_json
    _record(store, "claim_legacy_archive")
    row = _row(store, "claim_legacy_archive")
    entry = _entry(row, applicability="applicable")
    _record(store, entry["review_receipt_id"], kind="reflection", source=inventory._REVIEW_SOURCE,
        meta={"report_type": inventory._REVIEW_TYPE, "input_digest": entry["input_digest"]},
        content={"schema": inventory._REVIEW_TYPE, "status": "completed", "input_digest": entry["input_digest"]})
    # Exercise native verified segment hydration with a legacy compact row
    # whose classification exists only in the durable full receipt body.
    def archive(sql):
        stored = sql.execute("SELECT payload_json FROM records WHERE record_id=?", (entry["review_receipt_id"],)).fetchone()
        full = json.loads(stored["payload_json"])
        pointer = sql.payload_segments.append(canonical_payload_json(full).encode())
        compact = {**full, "content": {}}
        sql.execute("UPDATE records SET payload_json=?,payload_pointer_json=? WHERE record_id=?",
            (canonical_payload_json(compact), canonical_payload_json(pointer), entry["review_receipt_id"]))
        sql.commit()
    store.run_locked(archive)
    projection = store.read_consistent(lambda sql: sql.execute(
        "SELECT payload_pointer_json,json_extract(payload_json,'$.content.status') AS inline_status,"
        "json_extract(meta_json,'$.review_status') AS compact_status FROM records WHERE record_id=?",
        (entry["review_receipt_id"],)).fetchone())
    assert projection["payload_pointer_json"] and projection["inline_status"] is None and projection["compact_status"] is None
    assert inventory.upsert_manifest_entry(store, SCOPE, TARGET, entry)
    high = inventory.current_watermark(store, SCOPE)
    assert high == 0
    _record(store, "review_competes_with_archived", kind="reflection", source=inventory._REVIEW_SOURCE,
        meta={"report_type": inventory._REVIEW_TYPE, "input_digest": entry["input_digest"], "review_status": "completed"},
        content={"schema": inventory._REVIEW_TYPE, "status": "completed", "input_digest": entry["input_digest"]})
    assert inventory.current_watermark(store, SCOPE) > high
    assert inventory.get_manifest_entry(store, SCOPE, TARGET, row["storage_key"])["dirty"]
    assert not inventory.upsert_manifest_entry(store, SCOPE, TARGET, entry)
    assert inventory.get_unique_applicable(store, SCOPE, TARGET) is None
    assert len(inventory.reviews_for_input(store, SCOPE, entry["input_digest"])) == 2


def test_completed_after_unavailable_requires_one_settle_then_unique_proof_is_stable(store):
    _record(store, "claim_unavailable_first")
    row = _row(store, "claim_unavailable_first")
    entry = _entry(row, applicability="applicable")
    inventory.ensure(store)
    _record(store, "review_initial_unavailable", kind="reflection", source=inventory._REVIEW_SOURCE,
        meta={"report_type": inventory._REVIEW_TYPE, "input_digest": entry["input_digest"], "review_status": "unavailable"},
        content={"schema": inventory._REVIEW_TYPE, "status": "unavailable", "input_digest": entry["input_digest"]})
    assert inventory.current_watermark(store, SCOPE) == 0
    assert _upsert(store, SCOPE, TARGET, entry)
    high = inventory.current_watermark(store, SCOPE)
    assert high > 0
    assert inventory.upsert_manifest_entry(store, SCOPE, TARGET, entry)
    assert inventory.current_watermark(store, SCOPE) == high
    assert inventory.get_unique_applicable(store, SCOPE, TARGET)["review_receipt_id"] == entry["review_receipt_id"]
