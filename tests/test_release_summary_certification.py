"""Contract fixtures are synthetic; these tests never certify a deployed release."""
from copy import deepcopy
import json

import pytest

from deploy.summarize_release_closure import main, summarize_release_closure
from release_report_fixtures import complete_report, accumulating_report, wait_report


@pytest.mark.parametrize("builder,certified,waiting", [
    (complete_report, True, False), (accumulating_report, False, True), (wait_report, False, True),
])
def test_admitted_states_have_consistent_json_and_exit(builder, certified, waiting, tmp_path, capsys):
    report = builder()
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert main(["--path", str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["exit_code"] == 0 and out["contract_ok"] is True
    assert out["closure_certified"] is certified
    assert out["closure_complete"] is certified
    assert out["data_accumulating"] is waiting
    assert out["reported_ok"] is report["ok"]


@pytest.mark.parametrize("path,value", [
    (("report_type",), "unknown"), (("storage_migrations", "ok"), False),
    (("deployment", "release_path"), "/other-release"),
    (("deployment_receipt", "release_session_id"), "wrong"),
    (("replay_bootstrap", "capability_acceptance", "results"), []),
    (("replay_bootstrap", "capability_replay", "persisted_replay_ids"), []),
    (("live_acceptance", "cases"), []),
    (("live_acceptance", "deployment", "release_session_id"), "wrong"),
    (("readiness", "readiness_score"), 0.99), (("channel_acceptance", "ok"), False),
])
def test_failed_contract_never_prints_certified_success(path, value, tmp_path, capsys):
    report = complete_report()
    assert summarize_release_closure(report)["closure_certified"] is True
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    snapshot = deepcopy(report)
    report_file = tmp_path / "report.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    assert main(["--path", str(report_file)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["exit_code"] == 1 and out["contract_ok"] is False
    assert out["ok"] is False and out["closure_complete"] is False
    assert out["closure_certified"] is False
    assert out["business_closure_outcome"] == "failed"
    assert out["reported_ok"] is True and out["reported_closure_complete"] is True
    assert report == snapshot


@pytest.mark.parametrize("value", [True, "10", 10.0, 10.5, -1])
def test_count_coercion_cannot_certify(value):
    report = complete_report()
    assert summarize_release_closure(report)["contract_ok"]
    report["live_acceptance"]["case_count"] = value
    assert summarize_release_closure(report)["contract_ok"] is False


def test_missing_all_evidence_does_not_become_success():
    result = summarize_release_closure({"ok": True, "closure_complete": True, "data_accumulating": False})
    assert result["reported_ok"] and not result["ok"] and not result["closure_certified"]


def test_pre_observation_summary_does_not_use_legacy_replay_checker(monkeypatch):
    # Only summary dispatch is isolated. This does not test the runtime's
    # dynamic admission implementation or synthesize a real admission.
    from deploy import summarize_release_closure as summary_module
    from eimemory.governance.release import closure_verdict
    # The summary implementation lives in closure_verdict since 1.14.4; the
    # deploy CLI re-exports it, so isolate the contract check at its owner.
    monkeypatch.setattr(closure_verdict, '_release_closure_summary_contract_ok', lambda *_args: True)
    report = dict(ok=True, report_type='code_evolution_pre_observation', status='ready_for_observation',
                  closure_complete=False, data_accumulating=False, replay_bootstrap={'ok': True})
    result = summary_module.summarize_release_closure(report)
    assert result['replay_ok'] is True
    assert result['closure_certified'] is False
    assert result['business_closure_outcome'] == 'ready_for_observation'
