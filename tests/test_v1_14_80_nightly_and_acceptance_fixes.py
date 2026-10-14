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


def test_full_dedupe_without_witness_block_is_still_session_deduped() -> None:
    raw = _diag(selector={"status": "evidence_found", "input_count": 13, "dropped_reasons": {"grounding": 10}},
                post_selection={"session_dedupe_input_count": 3, "session_deduped_count": 3,
                                "item_limit_input_count": 0, "render_input_count": 0})
    assert _empty_decision_reason(raw) == "session_deduped_prior_delivery"


def test_no_evidence_without_selector_input_names_the_emptying_stage() -> None:
    base = {"selector": {"status": "no_evidence", "input_count": 0, "dropped_reasons": {}}}
    pipe = lambda *stages: _diag(**base, pipeline=list(stages))
    assert _empty_decision_reason(pipe({"name": "retrieve", "retrieved_count": 6},
                                       {"name": "graph_expand", "candidate_count": 6},
                                       {"name": "score_filter", "blocked_counts": {"task_context": 6}})
                                  ) == "pre_selector:score_filter:task_context"
    assert _empty_decision_reason(pipe({"name": "retrieve", "retrieved_count": 48},
                                       {"name": "graph_expand", "candidate_count": 0})
                                  ) == "pre_selector:graph_expand_empty"
    assert _empty_decision_reason(pipe({"name": "retrieve", "retrieved_count": 0})) == "pre_selector:retrieve_empty"


def test_live_acceptance_case_response_carries_reuse_provenance() -> None:
    from types import SimpleNamespace
    from eimemory.governance.l5 import live_task_acceptance as lta

    record = SimpleNamespace(record_id="r1", created_at="2026-10-14T11:33:43Z",
                             content={"case_id": "c", "task_type": "t", "passed": True,
                                      "deployment_commit": "a" * 40, "release_session_id": "rec_x"})
    original = lta._record_payload
    try:
        lta._record_payload = lambda rec: rec.content
        out = lta._case_response(record, reused=True, trace_ok=True)
    finally:
        lta._record_payload = original
    assert out["reused"] is True and out["deployment_commit"] == "a" * 40
    assert out["release_session_id"] == "rec_x" and out["recorded_at"] == "2026-10-14T11:33:43Z"


def test_deploy_worker_reexecs_target_worker_after_fast_forward() -> None:
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "deploy" / "eimemory-deploy-worker").read_text()
    block = script.split('echo "checkout_fast_forward=$head..$COMMIT" >&2', 1)[1].split("\nfi\n", 1)[0]
    assert 'EIMEMORY_DEPLOY_WORKER_REEXEC=1 exec "$REPO/deploy/eimemory-deploy-worker" "$MODE" "$COMMIT"' in block
    assert '"${EIMEMORY_DEPLOY_WORKER_REEXEC:-0}" != "1"' in block
    assert "exec 9>&-" in block


def test_live_acceptance_recorded_at_reads_record_envelope_time() -> None:
    from eimemory.governance.l5 import live_task_acceptance as lta
    from eimemory.models.records import RecordEnvelope, ScopeRef

    record = RecordEnvelope.create(
        kind="learning_eval", title="t", summary="s",
        content={"case_id": "c", "task_type": "t", "passed": True,
                 "deployment_commit": "b" * 40, "release_session_id": "rec_y"},
        source="test", source_id="default",
        scope=ScopeRef(tenant_id="default", agent_id="a", workspace_id="w", user_id="u"),
    )
    out = lta._case_response(record, reused=True, trace_ok=True)
    assert out["recorded_at"] and out["recorded_at"] == record.time.created_at


def _receipt_harness(tmp_path, outputs):
    import os
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    if shutil.which("bash") is None:
        pytest.skip("bash is required")
    script_text = (Path(__file__).resolve().parents[1] / "deploy" / "install_immutable_release.sh").read_text()
    marker = "_record_deployment_receipt() {"
    func = marker + script_text.split(marker, 1)[1].split("\n}", 1)[0] + "\n}\n"
    release = tmp_path / "rel"
    (release / ".venv" / "bin").mkdir(parents=True)
    counter = tmp_path / "n"
    counter.write_text("0")
    plan = tmp_path / "plan"
    plan.write_text("\n".join(outputs))
    fake = release / ".venv" / "bin" / "python"
    fake.write_text(f"""#!/usr/bin/env bash
n=$(cat {counter}); n=$((n+1)); echo $n > {counter}
line=$(sed -n "${{n}}p" {plan})
echo "${{line#*|}}"; exit "${{line%%|*}}"
""")
    fake.chmod(0o755)
    setup = (f"USER_SYSTEMD_ENABLE_SERVICE=1\nRELEASE_DIR={release}\nREPO_DIR={tmp_path}\nCOMMIT=abc\n"
             "EIMEMORY_RECEIPT_HEALTH_RETRY_SECONDS=0\n_prepare_release_receipt_source() { return 0; }\n")
    result = subprocess.run(["bash", "-c", setup + func + "if _record_deployment_receipt; then exit 0; else exit $?; fi\n"],
                            capture_output=True, text=True, timeout=30, env=dict(os.environ))
    return result, int(counter.read_text())


def test_receipt_retries_only_transient_health_verdicts(tmp_path) -> None:
    result, calls = _receipt_harness(tmp_path, [
        '1|{"ok": false, "error": "health_identity_missing"}',
        '1|{"ok": false, "error": "health_fetch_failed"}',
        '0|{"ok": true}',
    ])
    assert result.returncode == 0, result.stderr
    assert calls == 3 and "deployment_receipt_retry=transient_health attempt=2" in result.stderr


def test_receipt_does_not_retry_identity_mismatch(tmp_path) -> None:
    result, calls = _receipt_harness(tmp_path, ['1|{"ok": false, "error": "health_commit_mismatch"}', '0|{"ok": true}'])
    assert result.returncode == 1 and calls == 1
