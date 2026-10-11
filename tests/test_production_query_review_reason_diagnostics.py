"""Native evidence failures stay distinguishable after nightly receipt readback."""
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.learning.supervisor import persist_supervisor_summary, supervisor_summary
from eimemory.scheduler.result_contract import nightly_result_diagnostics
from test_production_query_auto_review import (
    BASE_SCOPE, _keys, _seed, _collect, auto_review_pending_production_queries,
    build_production_query_dataset,
)


@pytest.mark.parametrize("failure, reason", [
    ("missing_decision", "pending_capture_decision_missing"),
    ("missing_query", "original_query_input_boundary_mismatch"),
    ("low_signal", "query_features_low_signal"),
    ("not_delivered", "semantic_judgment_not_delivered"),
    ("boundary_invalid", "pending_capture_boundary_invalid"),
])
def test_native_review_reason_survives_supervisor_reopen_without_certification(tmp_path, failure, reason):
    with Runtime.create(root=tmp_path) as runtime:
        options = ({"query": "memory recall"} if failure == "low_signal" else
                   {"delivered": False, "semantic": None} if failure == "not_delivered" else {})
        _, decision_id = _seed(runtime, 9011, **options)
        pending_id = _collect(runtime)[0]
        if failure == "boundary_invalid":
            pending = runtime.store.get_by_id(pending_id)
            pending.content["channel"] = "codex"
            def corrupt_boundary(db):
                db.upsert(pending, commit=False)
                return None, [pending], []
            runtime.store.mutate_records_atomically(corrupt_boundary)
        if failure in ("missing_decision", "missing_query"):
            table = "proactive_decisions" if failure == "missing_decision" else "proactive_query_input_vault"
            with runtime.store.locked() as db:
                db.execute(f"DELETE FROM {table} WHERE decision_id = ?", (decision_id,))
                db.commit()
        reviewed = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
        assert reviewed["not_passed_count"] == 1 and reviewed["pending_count"] == 0
        assert reviewed["reason_counts"]["not_passed"][reason] == 1
        assert build_production_query_dataset(runtime, scope=BASE_SCOPE)["progress"]["accepted_case_count"] == 0
        # Plausible-looking unknown tokens must not pass a prefix/regex filter.
        reviewed["reason_counts"]["not_passed"].update({
            "private_provider_token": 1, "auto_accept_failed:private_provider_token": 1})
        diagnostics = nightly_result_diagnostics({"production_recall_auto_review": reviewed}, [])
        assert diagnostics["recall_quality_accepted"] is False
        summary = supervisor_summary(command="nightly", ok=True, duration_ms=1, memory_peak=0)
        summary["nightly_diagnostics"] = diagnostics
        receipt_id = persist_supervisor_summary(runtime, scope=BASE_SCOPE, summary=summary).record_id
    with Runtime.create(root=tmp_path) as runtime:
        saved = runtime.store.get_by_id(receipt_id)
        review = saved.content["nightly_diagnostics"]["recall_label_auto_review"]
        assert review["not_passed_reason_counts"][reason] == 1
        assert review["not_passed_reason_counts"]["reason_not_allowlisted"] == 2
        assert review["reason_catalog_version"] == "production_recall_review_reason_catalog.v1"
        assert review["not_passed_reasons_truncated"] is False
        assert "private_provider_token" not in json.dumps(saved.content)
