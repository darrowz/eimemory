"""1.14.80: nightly rollback census, production-recall empty classification, CLI scope."""
from __future__ import annotations

import json
import pytest

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


RECEIPT_COMMIT = "a" * 40
RECEIPT_ID = "rec_retry_fixture"


def _receipt_success(*, receipt_id=RECEIPT_ID):
    return "0|" + json.dumps({"ok": True, "report_type": "deployment_receipt",
        "commit": RECEIPT_COMMIT, "promotion_request_id": receipt_id})


def _receipt_harness(tmp_path, outputs, *, persisted="deployed"):
    import os
    import shlex
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import pytest
    from eimemory.storage.runtime_store import RuntimeStore
    from test_installer_receipt_before_maintenance import OWNER, _receipt

    if sys.platform != "linux" or shutil.which("bash") is None:
        pytest.skip("native Linux Bash receipt and immutable import-path contract")
    script_text = (Path(__file__).resolve().parents[1] / "deploy" / "install_immutable_release.sh").read_text()
    marker = "_record_deployment_receipt() {"
    func = marker + script_text.split(marker, 1)[1].split("\n}", 1)[0] + "\n}\n"
    checker_marker = "_verify_current_deployment_receipt() {"
    checker = checker_marker + script_text.split(checker_marker, 1)[1].split("\n}", 1)[0] + "\n}\n"
    release = tmp_path / "releases" / RECEIPT_COMMIT
    (release / ".venv" / "bin").mkdir(parents=True)
    # The separate native checker must derive its real immutable import-path
    # identity. Copy the package into this isolated commit-named fixture;
    # never monkeypatch current_release_identity or its runtime-commit reader.
    shutil.copytree(Path(__file__).resolve().parents[1] / "eimemory", release / "eimemory",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    current = tmp_path / "current"
    current.symlink_to(release, target_is_directory=True)
    root = tmp_path / "runtime"
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({"root": str(root)}), encoding="utf-8")
    record = _receipt(commit=RECEIPT_COMMIT)
    record.record_id = RECEIPT_ID
    effect = record.content["side_effect"]
    effect["release"]["release_path"] = str(release)
    effect["deployment"].update(release_path=str(release), current_link=str(current))
    effect["post_deploy_health"].update(release_path=str(release), current_link=str(current))
    if persisted != "absent":
        record.status = persisted
    store = RuntimeStore(root)
    try:
        if persisted != "absent":
            store.append(record)
    finally:
        store.close()
    counter = tmp_path / "n"
    counter.write_text("0")
    plan = tmp_path / "plan"
    plan.write_text("\n".join(outputs))
    readback = tmp_path / "native-readback"
    bootstrap = tmp_path / "checker.py"
    bootstrap.write_text("import sys\n"
        f"sys.path.insert(0, {str(release)!r})\n"
        "exec(compile(sys.stdin.read(), 'installer-native-receipt-checker', 'exec'), {})\n", encoding="utf-8")
    cache = tmp_path / "empty-cache"
    cache.mkdir()
    fake = release / ".venv" / "bin" / "python"
    fake.write_text(f"""#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -lt 5 ] || [ "$1" != -I ] || [ "$2" != -B ] || [ "$3" != -X ] || [ "$4" != pycache_prefix=/var/lib/eimemory-manager/empty-pycache ]; then
  echo unexpected_interpreter_argv >&2; exit 64
fi
shift 4
if [ "$1" = - ]; then
  [ "$#" = 7 ] || exit 64
  printf '%s\\n' "$3" > {shlex.quote(str(readback))}
  exec {shlex.quote(sys.executable)} -I -B -X {shlex.quote('pycache_prefix='+str(cache))} {shlex.quote(str(bootstrap))} "${{@:2}}"
fi
[ "$1" = {shlex.quote(str(tmp_path / 'deploy/record_deployment_receipt.py'))} ] || exit 64
n=$(cat {shlex.quote(str(counter))}); n=$((n+1)); echo $n > {shlex.quote(str(counter))}
line=$(sed -n "${{n}}p" {shlex.quote(str(plan))})
[ -n "$line" ] || {{ echo unplanned_recorder_call >&2; exit 97; }}
echo "${{line#*|}}"; exit "${{line%%|*}}"
""", encoding="utf-8")
    fake.chmod(0o755)
    values = {"RELEASE_DIR": release, "REPO_DIR": tmp_path, "COMMIT": RECEIPT_COMMIT,
              "CURRENT_LINK": current, "EIMEMORY_ROOT": root, "EIMEMORY_CONFIG_DIR": config,
              "EVIDENCE_RECEIPT_ENV_FILE": tmp_path / "receipt.env",
              "EIMEMORY_DEPLOY_SCOPE_TENANT": OWNER.tenant_id,
              "EIMEMORY_DEPLOY_SCOPE_AGENT": OWNER.agent_id,
              "EIMEMORY_DEPLOY_SCOPE_WORKSPACE": OWNER.workspace_id,
              "EIMEMORY_DEPLOY_SCOPE_USER": OWNER.user_id}
    setup = ("USER_SYSTEMD_ENABLE_SERVICE=1\n" + "".join(f"{key}={shlex.quote(str(value))}\n" for key, value in values.items()) +
             f"export EIMEMORY_DEPLOYMENT_RELEASES_ROOT={shlex.quote(str(release.parent))}\n"
             f"export EIMEMORY_DEPLOYMENT_CURRENT_LINK={shlex.quote(str(current))}\n"
             "export EIMEMORY_DEPLOYMENT_HEALTH_URL=http://127.0.0.1:8091/health\n"
             "EIMEMORY_RECEIPT_HEALTH_RETRY_SECONDS=0\n_prepare_release_receipt_source() { return 0; }\n")
    result = subprocess.run(["bash", "-c", setup + checker + func + "if _record_deployment_receipt; then exit 0; else exit $?; fi\n"],
                            capture_output=True, text=True, timeout=30, env=dict(os.environ))
    return result, int(counter.read_text())


def test_receipt_retries_only_transient_health_verdicts(tmp_path) -> None:
    result, calls = _receipt_harness(tmp_path, [
        '1|{"ok": false, "error": "health_identity_missing"}',
        '1|{"ok": false, "error": "health_fetch_failed"}',
        _receipt_success(),
    ])
    assert result.returncode == 0, result.stderr
    assert calls == 3 and "deployment_receipt_retry=transient_health attempt=2" in result.stderr
    proofs = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert {"current_deployment_receipt_verified": True, "commit": RECEIPT_COMMIT,
            "receipt_id": RECEIPT_ID} in proofs
    assert (tmp_path / "native-readback").read_text().strip() == RECEIPT_ID


def test_receipt_does_not_retry_identity_mismatch(tmp_path) -> None:
    result, calls = _receipt_harness(tmp_path, ['1|{"ok": false, "error": "health_commit_mismatch"}', _receipt_success()])
    assert result.returncode == 1 and calls == 1
    assert not (tmp_path / "native-readback").exists()


def test_exit_zero_incomplete_receipt_never_reaches_native_readback(tmp_path) -> None:
    result, calls = _receipt_harness(tmp_path, ['0|{"ok": true}'])
    assert result.returncode == 2 and calls == 1
    assert "recorder_contract_invalid" in result.stderr
    assert not (tmp_path / "native-readback").exists()


@pytest.mark.parametrize("invalid", ["absent", "revoked", "wrong_id"])
def test_complete_recorder_output_cannot_replace_committed_native_authority(tmp_path, invalid) -> None:
    result, calls = _receipt_harness(tmp_path,
        [_receipt_success(receipt_id="rec_other" if invalid == "wrong_id" else RECEIPT_ID)],
        persisted=invalid if invalid != "wrong_id" else "deployed")
    assert result.returncode == 1 and calls == 1
    assert "current_deployment_receipt_unavailable" in result.stderr
    assert (tmp_path / "native-readback").is_file()


def test_session_deduped_empty_reason_is_allowlisted_review_code() -> None:
    from eimemory.evaluation.production_query_review_reasons import PRODUCTION_REVIEW_REASON_CODES
    from eimemory.evaluation.production_query_auto_review import _STRUCTURAL_CLOSE

    assert "session_deduped_prior_delivery" in PRODUCTION_REVIEW_REASON_CODES
    assert _STRUCTURAL_CLOSE["session_deduped_prior_delivery"] == "session_deduped_prior_delivery"


def test_collection_diagnostics_forwards_new_session_deduped_count() -> None:
    from eimemory.scheduler.collection_diagnostics import production_collection_diagnostics

    out = production_collection_diagnostics({
        "schema": "ignored",
        "status": "completed",
        "decision_count": 2,
        "new_count": 2,
        "existing_count": 0,
        "empty_result_count": 0,
        "new_nonempty_count": 0,
        "new_empty_count": 0,
        "new_session_deduped_count": 2,
        "session_deduped_count": 2,
        "new_pending_record_ids": [],
        "policy": {"enabled": True},
    }, {})
    assert out["new_session_deduped_count"] == 2
    assert out["new_empty_count"] == 0
