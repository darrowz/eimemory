"""1.14.80: nightly rollback census, production-recall empty classification, CLI scope."""
from __future__ import annotations

import json

from eimemory.evaluation.production_query_dataset import _empty_decision_reason
from eimemory.scheduler import jobs
from eimemory.scheduler.collection_diagnostics import _empty_reason_counts


def test_rolled_back_count_ignores_historical_census_sections() -> None:
    report = {
        "outcome_evolution": {"rolled_back_count": 57},
        "autonomy": {"post_promotion_watch": {"rolled_back_count": 4}},
        "autonomous_evolution": {"rolled_back_count": 1},
        "patches": [{"rollback_count": 2}],
    }
    assert jobs._nightly_rolled_back_count(report) == 3


def _diag(**kwargs) -> str:
    return json.dumps(kwargs)


def test_fully_session_deduped_decision_is_not_an_empty_recall() -> None:
    raw = _diag(
        retrieval_status="evidence_found",
        selector={"status": "evidence_found", "selected_count": 2, "dropped_reasons": {"grounding": 3}},
        post_selection={"session_dedupe_input_count": 2, "session_deduped_count": 2, "render_input_count": 0},
        session_dedupe={"removed_count": 2},
    )
    assert _empty_decision_reason(raw) == "session_deduped_prior_delivery"


def test_partial_dedupe_or_rendered_items_stay_empty_with_selector_reason() -> None:
    raw = _diag(
        selector={"status": "evidence_found", "dropped_reasons": {}},
        post_selection={"session_dedupe_input_count": 3, "session_deduped_count": 1, "render_input_count": 2},
        session_dedupe={"removed_count": 1},
    )
    assert _empty_decision_reason(raw) == "selector_evidence_found"


def test_selector_no_evidence_reports_top_drop_reason() -> None:
    raw = _diag(selector={"status": "no_evidence",
                          "dropped_reasons": {"grounding": 4, "requested_attribute_missing": 7}})
    assert _empty_decision_reason(raw) == "selector_no_evidence:requested_attribute_missing"
    assert _empty_decision_reason(None) == "diagnostics_unavailable"
    assert _empty_decision_reason("{not json") == "diagnostics_unavailable"


def test_empty_reason_counts_are_bounded_codes() -> None:
    out = _empty_reason_counts({"selector_no_evidence:grounding": 3, "bad label with spaces": 2})
    assert out == {"selector_no_evidence:grounding": 3, "reason_not_allowlisted": 2}
    assert _empty_reason_counts(None) is None


def test_l5_readiness_accepts_scope_options() -> None:
    from eimemory.cli.main import _build_parser, _cli_scope

    parsed = _build_parser().parse_args([
        "learn", "l5-readiness", "--reader-mode", "v3",
        "--scope-agent", "hongtu", "--scope-workspace", "embodied", "--scope-user", "darrow",
    ])
    scope = _cli_scope(parsed, defaults={"agent_id": "main", "workspace_id": "default", "user_id": "x"})
    assert scope["agent_id"] == "hongtu" and scope["workspace_id"] == "embodied"


def test_deployment_repo_root_falls_back_to_settings(tmp_path, monkeypatch) -> None:
    from eimemory.governance.release.deployment_receipt import default_deployment_repo_root

    (tmp_path / "settings.json").write_text(json.dumps({"trusted_repository_root": str(tmp_path)}))
    monkeypatch.delenv("EIMEMORY_TRUSTED_REPOSITORY_ROOT", raising=False)
    monkeypatch.delenv("EIMEMORY_DEPLOYMENT_REPO_ROOT", raising=False)
    monkeypatch.delenv("EIMEMORY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("EIMEMORY_CONFIG_DIR", str(tmp_path))
    assert default_deployment_repo_root() == str(tmp_path.resolve())
