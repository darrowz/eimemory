import getpass
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.evaluation import production_query_dataset, real_query_gate
from eimemory.evaluation.production_recall import run_production_recall_eval
from eimemory.evaluation.recall_agent_scopes import agent_verdict
from eimemory.scheduler import jobs
from test_production_real_query_gate import BASE_SCOPE, RELEASE, _dataset, _refresh_dataset_evidence


@pytest.fixture(autouse=True)
def isolate_operator_context(monkeypatch):
    for name in ("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", "EIMEMORY_DEPLOY_SCOPE_USER",
                 "EIMEMORY_PRODUCTION_RECALL_DATASETS", "EIMEMORY_PRODUCTION_RECALL_DATASET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(getpass, "getuser", lambda: "host-user")


def bindings(tmp_path, monkeypatch, scopes, *, mode=0o600):
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps([{"scope": resolve_channel_scope("hermes", scope),
                                 "receipt_id": "receipt", "receipt_sha256": "a" * 64}
                                for scope in scopes]))
    path.chmod(mode)
    monkeypatch.setenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", str(path))
    return path


def bound_dataset(scope=BASE_SCOPE):
    dataset = deepcopy(_dataset({channel: "record-" + channel
                                 for channel in ("openclaw", "codex", "hermes")}))
    dataset["scope"] = dict(scope)
    for case in dataset["cases"]:
        case["scope"] = resolve_channel_scope(case["channel"], scope)
    _refresh_dataset_evidence(dataset)
    assert real_query_gate.freeze_production_recall_dataset(dataset)["eligibility"]["ok"] is True
    return dataset


def run_nightly_recall(tmp_path, monkeypatch, requested, dataset):
    calls = {"hydrate_count": 0}
    runtime = SimpleNamespace(store=SimpleNamespace(root=tmp_path / "runtime"))

    def build(runtime, *, scope):
        calls["build_scope"] = dict(scope)
        return {"ready": True, "dataset": dataset}

    def hydrate(*args, **kwargs):
        calls["hydrate_count"] += 1
        # Passing the scope boundary must still require real label authority.
        return False, "label_authority_required", {}

    def evaluate(dataset, *, seed, scope, persist_report):
        calls["eval_scope"] = dict(scope)
        return run_production_recall_eval(
            runtime, dataset, seed=seed, scope=scope, persist_report=persist_report)

    runtime.run_production_recall_eval = evaluate
    monkeypatch.setattr(production_query_dataset, "build_production_query_dataset", build)
    monkeypatch.setattr(real_query_gate, "current_release_identity", lambda *args: RELEASE)
    monkeypatch.setattr(real_query_gate, "_hydrate_real_query_labels", hydrate)
    monkeypatch.setattr(real_query_gate, "_persist_eligible_high_water",
                        lambda runtime, report, **kwargs: report)
    report = jobs._run_production_recall_eval(runtime, scope=requested)
    return report, calls


def test_host_account_uses_bound_logical_context_for_dataset_and_strict_gate(tmp_path, monkeypatch):
    bindings(tmp_path, monkeypatch, [BASE_SCOPE])
    dataset = bound_dataset()
    before = deepcopy(dataset)
    requested = {**BASE_SCOPE, "user_id": "host-user"}

    report, calls = run_nightly_recall(tmp_path, monkeypatch, requested, dataset)

    assert calls["build_scope"] == calls["eval_scope"] == BASE_SCOPE
    assert calls["hydrate_count"] == 1
    assert report["blocked_reason"] == "label_authority_required"
    assert "scope_mismatch_fields" not in report
    assert report["accepted"] is False
    assert dataset == before
    assert requested["user_id"] == "host-user"


@pytest.mark.parametrize("field", ["tenant_id", "agent_id", "workspace_id", "user_id"])
def test_dataset_metadata_cannot_redirect_bound_caller(tmp_path, monkeypatch, field):
    bindings(tmp_path, monkeypatch, [BASE_SCOPE])
    dataset = bound_dataset({**BASE_SCOPE, field: "another-identity"})

    report, calls = run_nightly_recall(
        tmp_path, monkeypatch, {**BASE_SCOPE, "user_id": "host-user"}, dataset)

    assert calls["build_scope"] == calls["eval_scope"] == BASE_SCOPE
    assert calls["hydrate_count"] == 0
    assert report["blocked_reason"] == "dataset_scope_override_mismatch"
    assert report["sample_count"] == 0
    assert report["scope_mismatch_fields"] == [field]
    assert agent_verdict(report)["scope_mismatch_fields"] == [field]


@pytest.mark.parametrize("field", ["tenant_id", "agent_id", "workspace_id"])
def test_host_account_does_not_borrow_binding_from_another_scope(tmp_path, monkeypatch, field):
    bindings(tmp_path, monkeypatch, [{**BASE_SCOPE, field: "another-identity"}])
    requested = {**BASE_SCOPE, "user_id": "host-user"}

    report, calls = run_nightly_recall(tmp_path, monkeypatch, requested, bound_dataset())

    assert calls["build_scope"] == calls["eval_scope"] == requested
    assert calls["hydrate_count"] == 0
    assert report["blocked_reason"] == "dataset_scope_override_mismatch"
    assert report["scope_mismatch_fields"] == ["user_id"]


def test_explicit_user_is_preserved_despite_operator_binding(tmp_path, monkeypatch):
    bindings(tmp_path, monkeypatch, [BASE_SCOPE])
    requested = {**BASE_SCOPE, "user_id": "explicit-user"}

    report, calls = run_nightly_recall(tmp_path, monkeypatch, requested, bound_dataset())

    assert calls["build_scope"] == calls["eval_scope"] == requested
    assert calls["hydrate_count"] == 0
    assert report["blocked_reason"] == "dataset_scope_override_mismatch"
    assert report["scope_mismatch_fields"] == ["user_id"]


@pytest.mark.parametrize("failure", ["missing", "public", "ambiguous", "symlink", "malformed"])
def test_host_translation_requires_unambiguous_private_binding(tmp_path, monkeypatch, failure):
    if failure != "missing":
        scopes = [BASE_SCOPE]
        if failure == "ambiguous":
            scopes.append({**BASE_SCOPE, "user_id": "another-member"})
        path = bindings(tmp_path, monkeypatch, scopes, mode=0o644 if failure == "public" else 0o600)
        if failure == "symlink":
            link = tmp_path / "link.json"
            link.symlink_to(path)
            monkeypatch.setenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", str(link))
        elif failure == "malformed":
            path.write_text("invalid private bindings")
    requested = {**BASE_SCOPE, "user_id": "host-user"}

    report, calls = run_nightly_recall(tmp_path, monkeypatch, requested, bound_dataset())

    assert calls["build_scope"] == calls["eval_scope"] == requested
    assert calls["hydrate_count"] == 0
    assert report["blocked_reason"] == "dataset_scope_override_mismatch"
    assert report["scope_mismatch_fields"] == ["user_id"]


def test_channel_workspace_difference_is_reported_without_changing_caller(tmp_path, monkeypatch):
    bindings(tmp_path, monkeypatch, [BASE_SCOPE])
    requested = resolve_channel_scope("hermes", {**BASE_SCOPE, "user_id": "host-user"})

    report, calls = run_nightly_recall(tmp_path, monkeypatch, requested, bound_dataset())

    assert calls["eval_scope"] == resolve_channel_scope("hermes", BASE_SCOPE)
    assert calls["hydrate_count"] == 0
    assert report["blocked_reason"] == "dataset_scope_override_mismatch"
    assert report["scope_mismatch_fields"] == ["workspace_id"]
    verdict = agent_verdict(report)
    assert verdict["scope_mismatch_fields"] == ["workspace_id"]
    assert "production" not in json.dumps(verdict["scope_mismatch_fields"])


def test_partial_scope_diagnostics_only_include_fields_actually_overridden(tmp_path, monkeypatch):
    requested = {"agent_id": "another-agent"}

    report, calls = run_nightly_recall(tmp_path, monkeypatch, requested, bound_dataset())

    assert calls["eval_scope"] == requested
    assert report["scope_mismatch_fields"] == ["agent_id"]


def test_authoritative_threshold_gate_is_not_reread_as_missing_quality_gate():
    # Nightly 1.14.149 shape: the authoritative report passed in threshold_gate,
    # while quality_gate was absent. Reading only quality_gate marked a passed
    # agent as production_recall_gate_failed / label_trust_false.
    report = {
        "ok": True,
        "sample_count": 3,
        "evaluation_contract": "production_real_query_gate.v1",
        "recall_at_5": 1.0,
        "mrr": 0.833,
        "threshold_gate": {
            "ok": True,
            "blocked_reason": "",
            "blocking_metrics": {},
            "recall_quality_evidence": {
                "evaluation_contract": "production_real_query_gate.v1",
                "label_trust_accepted": True,
            },
        },
    }

    verdict = agent_verdict(report)

    assert verdict["ok"] is True
    assert verdict["blocked_reason"] == ""
    assert verdict["label_trust_accepted"] is True
    assert verdict["sample_count"] == 3


def test_missing_quality_gate_still_fails_closed():
    verdict = agent_verdict({"ok": True, "sample_count": 3, "threshold_gate": {}})

    assert verdict["ok"] is False
    assert verdict["blocked_reason"] == "recall_quality_unavailable"
    assert verdict["label_trust_accepted"] is False


def test_per_agent_diagnostics_only_forward_fixed_scope_field_names():
    verdict = agent_verdict({
        "quality_gate": {"ok": False, "blocked_reason": "dataset_scope_override_mismatch"},
        "scope_mismatch_fields": ["private-scope-value", "workspace_id", "user_id", "workspace_id"],
    })

    assert verdict["scope_mismatch_fields"] == ["workspace_id", "user_id"]
