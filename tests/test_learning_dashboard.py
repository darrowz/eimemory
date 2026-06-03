from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.capability_seeding import SEEDED_CAPABILITIES, ensure_all_seeded
from eimemory.governance.learning_dashboard import build_weekly_dashboard


def test_capability_seed_is_idempotent_and_dashboard_lists_all_capabilities(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}

    first = ensure_all_seeded(runtime, scope=scope)
    second = ensure_all_seeded(runtime, scope=scope)
    report = build_weekly_dashboard(runtime, scope=scope, persist=True)

    assert first["created_count"] == len(SEEDED_CAPABILITIES)
    assert second["created_count"] == 0
    assert report["ok"] is True
    for capability in SEEDED_CAPABILITIES:
        assert capability in report["markdown"]
    assert "| Score | Average | Trend |" in report["markdown"]
    assert "- Learned:" in report["markdown"]
    assert "- Applied:" in report["markdown"]
    assert "- Blocked:" in report["markdown"]
    assert "- Next validation:" in report["markdown"]
    assert "## Failure Focus" in report["markdown"]
    assert report["persisted_record_id"]
