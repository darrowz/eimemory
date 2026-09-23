from __future__ import annotations

from eimemory.governance.code_automation_policy import (
    v2_allowed_but_unreachable,
    v2_allowed_files,
)
from eimemory.governance.code_evolution_path_policy import (
    DEFAULT_ALLOWED_PATH_GLOBS,
    DEFAULT_DENIED_PATH_GLOBS,
    DEFAULT_EXACT_ALLOW_FILES,
    path_allowed_for_evolution,
    path_denied_by_self,
)
from eimemory.governance import code_evolution_test_plans as plans


def test_v2_allowed_files_match_test_plans_or_explicit_unreachable() -> None:
    reachable: set[str] = set()
    for plan in plans.test_plan_manifest()["plans"]:
        for path in plan["allowed_files"]:
            if path == "fixture.py":
                continue
            reachable.add(path)
        for path in plan.get("allowed_path_globs") or ():
            # Directory mode unlocks governance/ops files previously marked unreachable.
            if path.startswith("eimemory/governance/") or path == "eimemory/governance/**":
                reachable.update(
                    {
                        "eimemory/governance/release_closure.py",
                        "eimemory/governance/release_closure_lineage.py",
                        "eimemory/governance/release_lineage.py",
                    }
                )
            if path.startswith("eimemory/ops/") or path == "eimemory/ops/**":
                reachable.add("eimemory/ops/release_closure_failure.py")
    allowed = v2_allowed_files()
    unreachable = v2_allowed_but_unreachable()
    assert unreachable <= allowed
    assert allowed - unreachable == (allowed & reachable)
    assert not (unreachable & reachable)


def test_deny_self_never_allowed_under_default_globs() -> None:
    for path in (
        "eimemory/governance/code_evolution_effects.py",
        "eimemory/governance/code_automation_policy.py",
        "eimemory/governance/code_automation_policy_issue.py",
        "eimemory/governance/code_evolution_path_policy.py",
        "eimemory/adapters/hermes/code_implementation.py",
        "eimemory/governance/code_evolution_test_plans.py",
    ):
        assert path_denied_by_self(path)
        ok, reason = path_allowed_for_evolution(
            path,
            allowed_path_globs=DEFAULT_ALLOWED_PATH_GLOBS,
            denied_path_globs=DEFAULT_DENIED_PATH_GLOBS,
        )
        assert ok is False
        assert reason == "deny_self"


def test_default_globs_keep_deploy_and_tests_closed() -> None:
    for path in (
        "deploy/install_immutable_release.sh",
        "deploy/systemd/eimemory.service",
        "tests/test_code_evolution_effects.py",
        "tests/conftest.py",
    ):
        ok, reason = path_allowed_for_evolution(
            path,
            allowed_path_globs=DEFAULT_ALLOWED_PATH_GLOBS,
            denied_path_globs=DEFAULT_DENIED_PATH_GLOBS,
        )
        assert ok is False
        assert reason in {"path_denied", "path_not_allowed"}


def test_governance_and_exact_runtime_identity_paths_allowed() -> None:
    ok, reason = path_allowed_for_evolution(
        "eimemory/governance/l5_reader.py",
        allowed_path_globs=DEFAULT_ALLOWED_PATH_GLOBS,
        denied_path_globs=DEFAULT_DENIED_PATH_GLOBS,
    )
    assert ok is True and reason == ""

    ok, reason = path_allowed_for_evolution(
        "eimemory/governance/release_closure.py",
        allowed_path_globs=DEFAULT_ALLOWED_PATH_GLOBS,
        denied_path_globs=DEFAULT_DENIED_PATH_GLOBS,
    )
    assert ok is True

    for path in DEFAULT_EXACT_ALLOW_FILES:
        ok, reason = path_allowed_for_evolution(
            path,
            allowed_path_globs=DEFAULT_ALLOWED_PATH_GLOBS,
            denied_path_globs=DEFAULT_DENIED_PATH_GLOBS,
            exact_allow_files=DEFAULT_EXACT_ALLOW_FILES,
        )
        assert ok is True, (path, reason)


def test_release_plan_reaches_release_closure_but_not_effects() -> None:
    plan = plans.RELEASE_CLOSURE_FAILURE_TEST_PLAN
    assert plan.allows_path("eimemory/governance/release_closure.py")
    assert plan.allows_path("eimemory/governance/release_closure_gate_evidence.py")
    assert plan.allows_path("eimemory/ops/release_closure_failure.py")
    assert not plan.allows_path("eimemory/governance/code_evolution_effects.py")
    assert not plan.allows_path("deploy/install_immutable_release.sh")
    assert not plan.allows_path("tests/test_release_closure.py")
