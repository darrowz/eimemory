"""S6: recover_incomplete_code_apply holds active-surface lease."""
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from eimemory.governance import promotion_manager as pm


def test_recover_incomplete_code_apply_blocks_when_lease_busy(tmp_path, monkeypatch) -> None:
    runtime = SimpleNamespace(store=SimpleNamespace(root=tmp_path), root=tmp_path)
    held = pm._acquire_active_surface_lease(runtime, timeout_sec=0.2)

    def boom(*_a, **_k):
        raise AssertionError("must not scan while lease busy")

    monkeypatch.setattr(pm, "_inflight_code_apply_transactions", boom)
    out = pm.recover_incomplete_code_apply(runtime, scope={"agent_id": "a"})
    assert out["ok"] is False
    assert out["blocked_reason"] == "active_surface_lease_unavailable"
    pm._release_active_surface_lease(held)
