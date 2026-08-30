from __future__ import annotations

from eimemory.governance.l5_reader import _select_code_evolution_rows


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
