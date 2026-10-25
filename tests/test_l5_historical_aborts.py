"""Historical abort relevance never supplies autonomous completion credit."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json

import pytest

from eimemory.governance.l5.l5_reader import (
    _historical_abort_authority, _qualifying_ledger_evidence_error,
    _select_code_evolution_rows,
)
from eimemory.governance.l5.native_assessment import _negative_evidence
from eimemory.models.records import ScopeRef
from eimemory.storage.code_evolution_store import CodeEvolutionStore
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef("default", "logical-agent", "workspace", "owner")
OLD = "a" * 40
CURRENT = "b" * 40


def authority():
    return {"commit": CURRENT, "receipt_id": "native-current", "created_at": "2026-10-25T08:10:59Z",
            "checked_at": "2026-10-25T10:00:00Z",
            "receipt_snapshot_digest": "d" * 64, "scope": asdict(SCOPE)}


@pytest.fixture
def native_abort(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        ledger = CodeEvolutionStore(store)
        row = ledger.create_transaction({
            "transaction_id": "old-user-repair", "idempotency_key": "old-user-repair",
            "scope": asdict(SCOPE),
            "incident": {"incident_id": "user-reported-incident"}, "origin": "user_reported",
            "known_before_detection": True, "prior_user_reported": True, "manual_bootstrap": False,
            "repository": {"root": str(tmp_path), "ref": "master", "base_commit": OLD},
            "base_commit": OLD, "current_state": "REGRESSION_VERIFIED",
            "payload": {"error": "historical_verification_failure"},
        })
        ledger.add_terminal_receipt(row["transaction_id"], {
            "outcome": "aborted_candidate_restored", "base_commit": OLD,
            "evidence_digest": "e" * 64, "reason": "full_suite_verification_failed",
            "created_at": "2026-10-21T04:26:46+00:00",
        }, terminal_state="ABORTED_CANDIDATE_RESTORED",
            expected_state=row["current_state"], expected_state_version=row["state_version"])
        yield store, ledger, ledger.get_transaction(row["transaction_id"])


def select(ledger, rows, current=None):
    history = []
    selected = _select_code_evolution_rows(ledger, rows, current_deployment=current,
                                         historical_transactions=history)
    return selected, history


def test_native_old_abort_preserves_adverse_facts_and_original_receipt(native_abort):
    store, ledger, row = native_abort
    original = ledger.get_transaction(row["transaction_id"])
    receipt = ledger.get_terminal_receipt(row["transaction_id"])
    selected, history = select(ledger, [row], authority())
    assert selected == (None, None) and len(history) == 1
    item = history[0]
    assert item["known_before_detection"] is True and item["prior_user_reported"] is True
    assert item["manual_bootstrap"] is False and item["origin"] == "user_reported"
    assert item["qualifies_for_product_completion"] is False
    assert json.loads(item["ledger_transaction_json"]) == original
    assert sha256(item["ledger_transaction_json"].encode()).hexdigest() == item["ledger_transaction_digest"]
    assert json.loads(item["terminal_receipt_json"]) == receipt["payload"]
    assert sha256(item["terminal_receipt_json"].encode()).hexdigest() == receipt["receipt_digest"]
    assert ledger.get_transaction(row["transaction_id"]) == original
    assert ledger.get_terminal_receipt(row["transaction_id"]) == receipt
    assert not _negative_evidence({"assessment": {}, "transaction_evidence": {"historical_transactions": history}})


@pytest.mark.parametrize("fault", ["no_authority", "same_release", "wrong_scope", "missing_time", "naive_time", "earlier_receipt",
                                    "future_deployment", "future_terminal", "missing_checked_at", "equal_terminal_deploy",
                                    "candidate", "deployed", "missing_base", "missing_receipt", "identity", "outcome", "digest"])
def test_uncertain_or_current_abort_is_never_skipped(native_abort, monkeypatch, fault):
    _, ledger, row = native_abort
    row = deepcopy(row)
    current = authority()
    receipt = ledger.get_terminal_receipt(row["transaction_id"])
    if fault == "no_authority": current = None
    elif fault == "same_release": current["commit"] = OLD
    elif fault == "wrong_scope": current["scope"]["user_id"] = "other-owner"
    elif fault == "missing_time": current.pop("created_at")
    elif fault == "naive_time": current["created_at"] = "2026-10-25T08:10:59"
    elif fault == "earlier_receipt": current["created_at"] = "2026-10-20T08:10:59Z"
    elif fault == "future_deployment": current["created_at"] = "2027-01-01T00:00:00Z"
    elif fault == "missing_checked_at": current.pop("checked_at")
    elif fault == "equal_terminal_deploy": current["created_at"] = receipt["created_at"]
    elif fault == "future_terminal":
        receipt["created_at"] = receipt["payload"]["created_at"] = "2027-01-02T00:00:00Z"
        receipt["receipt_digest"] = row["terminal_receipt_digest"] = sha256(json.dumps(
            receipt["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    elif fault == "candidate": row["candidate_commit"] = "c" * 40
    elif fault == "deployed": row["deployed_commit"] = "c" * 40
    elif fault == "missing_base": row["base_commit"] = ""
    elif fault == "missing_receipt": receipt = None
    elif fault == "identity": receipt["transaction_id"] = "different-transaction"
    elif fault == "outcome": receipt["outcome"] = "aborted_no_external_effect"
    elif fault == "digest": receipt["payload"]["reason"] = "tampered"
    monkeypatch.setattr(ledger, "get_terminal_receipt", lambda _: receipt)
    selected, history = select(ledger, [row], current)
    assert selected == (None, row) and history == []


def test_native_bad_receipt_read_cannot_be_swallowed(native_abort):
    store, ledger, row = native_abort
    with store._lock:
        # Simulate raw file corruption in this isolated test database; the
        # production append-only guard correctly forbids normal SQL mutation.
        store.sqlite.conn.execute("DROP TRIGGER trg_code_evolution_terminal_receipts_no_update")
        store.sqlite.conn.execute("UPDATE code_evolution_terminal_receipts SET payload_json='{}' WHERE transaction_id=?",
                                  (row["transaction_id"],))
        store.sqlite.conn.commit()
    with pytest.raises(RuntimeError, match="terminal receipt digest mismatch"):
        select(ledger, [row], authority())


@pytest.mark.parametrize("barrier", ["active", "quarantine", "qualifying", "current_abort"])
def test_history_does_not_allow_selecting_around_current_or_failed_evidence(native_abort, barrier):
    _, ledger, row = native_abort
    other = {**row, "transaction_id": "blocking-transaction"}
    if barrier == "active": other.update(terminal=False, current_state="OBSERVING")
    elif barrier == "quarantine": other.update(current_state="RECOVERY_QUARANTINED")
    elif barrier == "qualifying": other.update(current_state="SUCCEEDED_SEDIMENTED")
    else: other.update(base_commit=CURRENT)
    selected, history = select(ledger, [row, other], authority())
    assert selected == ((other, None) if barrier == "active" else (None, other))
    assert len(history) == (0 if barrier in {"active", "quarantine"} else 1)


@pytest.mark.parametrize("receipt,state,expected", [
    ({}, "ABORTED_CANDIDATE_RESTORED", "terminal_receipt_identity_mismatch"),
    ({"transaction_id": "tx", "receipt_digest": "d" * 64, "outcome": "aborted_candidate_restored"},
     "ABORTED_NO_EXTERNAL_EFFECT", "terminal_nonqualifying_state_outcome_mismatch"),
])
def test_missing_or_contradictory_nonqualifying_receipt_is_real_failure(receipt, state, expected):
    error = _qualifying_ledger_evidence_error(None,
        transaction_row={"transaction_id": "tx", "current_state": state, "terminal_receipt_digest": "d" * 64},
        terminal_receipt=receipt, current_provider={}, capabilities=None,
        runtime_scope=SCOPE, capability_scope="global")
    assert error == expected
    assert _negative_evidence({"assessment": {}, "transaction_evidence": {
        "qualifying_terminal_outcome": "aborted_candidate_restored", "evidence_error": error}})
