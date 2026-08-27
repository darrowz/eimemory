from __future__ import annotations

import inspect

import pytest

from eimemory.governance import autonomous_evolution, autonomous_learning
from eimemory.adapters.hermes.code_implementation import CodeImplementationError, validate_request, validate_response
from eimemory.governance.code_evolution_test_plans import (
    L5_PRODUCT_COMPLETION_TEST_PLAN_ID,
    RUNTIME_IDENTITY_DRIFT_TEST_PLAN_ID,
    allowed_files_for_incident,
    protected_test_plan,
    build_test_plan_argv,
    protected_test_plan_command_error,
)
from eimemory.governance.code_evolution_transaction import qualification_report
from eimemory.governance.code_patch_command_policy import protected_test_plan_command_error as policy_test_plan_command_error


def test_provider_response_without_request_cannot_widen_file_allowlist() -> None:
    response = {
        "schema": "code_implementation_response.v2",
        "request_id": "request",
        "request_digest": "a" * 64,
        "file_updates": [{"path": "deploy/install.sh", "prior_sha256": "b" * 64, "content": "x"}],
        "rationale": "bounded",
        "assumptions": [],
    }
    with pytest.raises(CodeImplementationError):
        validate_response(response)


def test_product_qualification_rejects_manual_known_and_user_reported_evidence() -> None:
    report = qualification_report(
        {"origin": "user_reported", "known_before_detection": True, "prior_user_reported": True, "manual_bootstrap": True, "observation_valid": True, "qualifying_terminal_outcome": "succeeded_sedimented"},
        current_lineage={"ok": True, "compatible": True},
    )
    assert report["qualifies_for_product_completion"] is False
    assert {"system_origin", "unknown_before_detection", "not_prior_user_reported", "not_manual_bootstrap"} <= set(report["reasons"])


def test_production_autonomy_modules_cannot_issue_legacy_code_mutation_authority() -> None:
    assert "_issue_legacy_promotion_authority" not in inspect.getsource(autonomous_evolution)
    assert "_issue_legacy_promotion_authority" not in inspect.getsource(autonomous_learning)


def test_protected_test_plan_accepts_only_trusted_argv() -> None:
    command = build_test_plan_argv(
        L5_PRODUCT_COMPLETION_TEST_PLAN_ID,
        "focused",
        candidate_python="/usr/bin/python3",
    )
    assert protected_test_plan_command_error(
        [command],
        plan_id=L5_PRODUCT_COMPLETION_TEST_PLAN_ID,
        candidate_python="/usr/bin/python3",
    ) == ""
    assert policy_test_plan_command_error(
        [command],
        plan_id=L5_PRODUCT_COMPLETION_TEST_PLAN_ID,
        candidate_python="/usr/bin/python3",
    ) == ""


@pytest.mark.parametrize(
    "command",
    [
        ["/usr/bin/python3", "-m", "pytest", "-q", "tests/test_l5_product_completion.py", "--capture=no"],
        ["/usr/bin/python3", "-m", "pytest", "-q", "tests/test_l5_product_completion.py", "../../etc/passwd"],
        ["/bin/sh", "-c", "pytest tests"],
    ],
)
def test_protected_test_plan_rejects_provider_owned_command_shape(command: list[str]) -> None:
    assert protected_test_plan_command_error(
        [command],
        plan_id=L5_PRODUCT_COMPLETION_TEST_PLAN_ID,
        candidate_python="/usr/bin/python3",
    ) != ""


def test_runtime_identity_drift_has_a_bounded_production_test_plan() -> None:
    plan = protected_test_plan(RUNTIME_IDENTITY_DRIFT_TEST_PLAN_ID)

    assert plan is not None
    assert plan.allowed_files == (
        "deploy/install_immutable_release.sh",
        "tests/test_deployment_tools.py",
    )
    assert allowed_files_for_incident(
        "deployment.runtime_commit_drift",
        test_plan_id=RUNTIME_IDENTITY_DRIFT_TEST_PLAN_ID,
    ) == plan.allowed_files
