"""Tests for capability_ledger enforcement of the ProposalCard under
HARNESS_PATCH_V2 (Task 3 of the 1.6.0 harness-patch plan).

When HARNESS_PATCH_V2=1 is set in the environment, ``record_capability_score``
rejects any candidate_promotion write that does not carry a valid
``proposal_card`` dict inside its content payload.
"""
from __future__ import annotations

import pytest

from eimemory.governance.capability_ledger import record_capability_score
from eimemory.governance.harness_patch import HARNESS_PATCH_V2


@pytest.mark.skipif(not HARNESS_PATCH_V2, reason="opt-in via HARNESS_PATCH_V2=1")
def test_capability_ledger_rejects_promotion_without_proposal_card(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HARNESS_PATCH_V2", "1")
    # Build a minimal runtime stub
    class _Stub:
        class store:
            list_records = lambda *a, **k: []
            rewrite = lambda *a, **k: None
            get_by_id = lambda *a, **k: None

    runtime = _Stub()
    with pytest.raises(ValueError, match="proposal_card"):
        record_capability_score(
            runtime,
            scope=None,
            loop_id="test",
            capability="memory.recall",
            score=0.9,
            meta={"authority_tier": "L2", "kind": "candidate_promotion"},
        )