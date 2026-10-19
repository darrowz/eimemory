from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
import json

from eimemory.governance.l5_reader import _completion_evidence_refs, _select_code_evolution_rows
from eimemory.models.records import ScopeRef
from eimemory.storage.code_evolution_store import CodeEvolutionStore
from eimemory.storage.runtime_store import RuntimeStore


class _Ledger:
    def __init__(self, resolved: set[str] | None = None) -> None:
        self.resolved = set(resolved or ())

    def get_quarantine_resolution(self, transaction_id: str):
        if transaction_id in self.resolved:
            return {"transaction_id": transaction_id}
        return None


def test_current_transaction_suppresses_prior_terminal_outcome() -> None:
    active = {
        "transaction_id": "tx-current",
        "current_state": "OBSERVING",
        "terminal": False,
    }
    prior = {
        "transaction_id": "tx-prior",
        "current_state": "SUCCEEDED_SEDIMENTED",
        "terminal": True,
    }

    assert _select_code_evolution_rows(_Ledger(), [active, prior]) == (active, None)


def test_resolved_quarantine_is_auditable_but_not_current_l5_outcome() -> None:
    quarantine = {
        "transaction_id": "tx-quarantine",
        "current_state": "RECOVERY_QUARANTINED",
        "terminal": True,
    }
    prior_success = {
        "transaction_id": "tx-success",
        "current_state": "SUCCEEDED_SEDIMENTED",
        "terminal": True,
    }

    assert _select_code_evolution_rows(
        _Ledger({"tx-quarantine"}),
        [quarantine, prior_success],
    ) == (None, prior_success)
    assert _select_code_evolution_rows(
        _Ledger(),
        [quarantine, prior_success],
    ) == (None, quarantine)


def test_actual_terminal_receipt_keeps_its_native_digest_and_completion_reference(tmp_path) -> None:
    scope = ScopeRef(agent_id="terminal-test", workspace_id="isolated", user_id="owner")
    with closing(RuntimeStore(tmp_path)) as store:
        ledger = CodeEvolutionStore(store)
        transaction = ledger.create_transaction({
            "transaction_id": "native-terminal", "idempotency_key": "native-terminal",
            "scope": asdict(scope), "incident": {"incident_id": "local-incident"},
            "repository": {"root": str(tmp_path), "ref": "master"},
            "terminal": False, "current_state": "OBSERVING",
        })
        receipt = ledger.add_terminal_receipt(
            transaction["transaction_id"],
            {"outcome": "succeeded_sedimented", "evidence_digest": "d" * 64},
            terminal_state="SUCCEEDED_SEDIMENTED",
            expected_state=transaction["current_state"], expected_state_version=transaction["state_version"],
        )
        # This is a real isolated append-only receipt. Its qualification still
        # belongs to the independent provider/observation/lineage verifiers.
        saved = ledger.get_terminal_receipt(transaction["transaction_id"])
        assert saved["receipt_digest"] == receipt["receipt_digest"]
        assert saved["payload"] == json.loads(receipt["payload_json"])
        transaction = ledger.get_transaction(transaction["transaction_id"])
        assert transaction["terminal_receipt_digest"] == receipt["receipt_digest"]
        assert "quality_chain_digest" not in transaction
        refs = _completion_evidence_refs(provider={}, transaction=transaction, lineage={})
        assert refs["transaction"] == [transaction["transaction_id"], receipt["receipt_digest"]]
