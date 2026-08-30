"""Protected code-evolution test plans and typed argv construction.

Test plans are trusted release code.  Incident/provider payloads can select a
registered plan by identifier but cannot add paths, options, interpreters, or
commands to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Sequence
import json


TEST_PLAN_SCHEMA = "code_evolution_test_plan.v1"
L5_PRODUCT_COMPLETION_TEST_PLAN_ID = "l5.product-completion-reporting.v1"
RUNTIME_IDENTITY_DRIFT_TEST_PLAN_ID = "deployment.runtime-identity-drift.v1"
RELEASE_CLOSURE_FAILURE_TEST_PLAN_ID = "release.closure-self-repair.v1"
CODE_IMPLEMENTATION_CATALOG_TEST_PLAN_ID = "hongtu.code-implementation-provider.v1"


@dataclass(frozen=True, slots=True)
class ProtectedTestPlan:
    plan_id: str
    allowed_files: tuple[str, ...]
    phases: tuple[tuple[str, tuple[str, ...]], ...]
    full_suite_required: bool = True

    @property
    def digest(self) -> str:
        payload = {
            "schema": TEST_PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "allowed_files": list(self.allowed_files),
            "phases": [[phase, list(paths)] for phase, paths in self.phases],
            "full_suite_required": self.full_suite_required,
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def argv(self, phase: str, *, candidate_python: str | Path) -> list[str]:
        normalized = str(phase or "").strip().lower()
        paths = dict(self.phases).get(normalized)
        if paths is None:
            raise ValueError("test_plan_phase_unknown")
        interpreter = Path(candidate_python)
        if not interpreter.is_absolute() or not interpreter.name.startswith("python"):
            raise ValueError("candidate_python_untrusted")
        argv = [str(interpreter), "-B", "-m", "pytest", "-q", *paths]
        if normalized == "full_suite":
            argv.insert(5, "--strict-markers")
        return argv


L5_PRODUCT_COMPLETION_TEST_PLAN = ProtectedTestPlan(
    plan_id=L5_PRODUCT_COMPLETION_TEST_PLAN_ID,
    allowed_files=("eimemory/governance/l5_reader.py",),
    phases=(
        (
            "focused",
            (
                "tests/test_l5_product_completion.py",
                "tests/test_l5_readiness.py",
                "tests/test_cli_governance.py",
            ),
        ),
        (
            "regression",
            (
                "tests/test_l5_v3_release_independence.py",
                "tests/test_capability_profiles.py",
                "tests/test_governance_console.py",
                "tests/test_no_fixed_l5_taxonomy.py",
            ),
        ),
        ("full_suite", ("tests",)),
    ),
)

CODE_IMPLEMENTATION_CATALOG_TEST_PLAN = ProtectedTestPlan(
    plan_id=CODE_IMPLEMENTATION_CATALOG_TEST_PLAN_ID,
    allowed_files=("fixture.py",),
    phases=(),
    full_suite_required=False,
)

RUNTIME_IDENTITY_DRIFT_TEST_PLAN = ProtectedTestPlan(
    plan_id=RUNTIME_IDENTITY_DRIFT_TEST_PLAN_ID,
    allowed_files=(
        "deploy/runtime_identity_policy.py",
        "tests/test_runtime_identity_policy.py",
    ),
    phases=(
        ("focused", ("tests/test_runtime_identity_policy.py",)),
        (
            "regression",
            (
                "tests/test_code_evolution_security.py",
                "tests/test_code_automation_policy_v2.py",
            ),
        ),
        ("full_suite", ("tests",)),
    ),
)

RELEASE_CLOSURE_FAILURE_TEST_PLAN = ProtectedTestPlan(
    plan_id=RELEASE_CLOSURE_FAILURE_TEST_PLAN_ID,
    allowed_files=(
        "eimemory/governance/release_closure_lineage.py",
    ),
    phases=(
        (
            "focused",
            (
                "tests/test_release_closure_failure.py",
                "tests/test_release_closure.py",
                "tests/test_release_lineage.py",
            ),
        ),
        (
            "regression",
            (
                "tests/test_code_evolution_security.py",
                "tests/test_code_automation_policy_v2.py",
                "tests/test_deployment_tools.py",
            ),
        ),
        ("full_suite", ("tests",)),
    ),
    full_suite_required=True,
)

_PLANS = {
    L5_PRODUCT_COMPLETION_TEST_PLAN_ID: L5_PRODUCT_COMPLETION_TEST_PLAN,
    CODE_IMPLEMENTATION_CATALOG_TEST_PLAN_ID: CODE_IMPLEMENTATION_CATALOG_TEST_PLAN,
    RUNTIME_IDENTITY_DRIFT_TEST_PLAN_ID: RUNTIME_IDENTITY_DRIFT_TEST_PLAN,
    RELEASE_CLOSURE_FAILURE_TEST_PLAN_ID: RELEASE_CLOSURE_FAILURE_TEST_PLAN,
}


def protected_test_plan(plan_id: str) -> ProtectedTestPlan | None:
    return _PLANS.get(str(plan_id or "").strip())


def protected_test_plan_digest(plan_id: str) -> str:
    plan = protected_test_plan(plan_id)
    return plan.digest if plan is not None else ""


def allowed_files_for_incident(incident_class: str, *, test_plan_id: str = "") -> tuple[str, ...]:
    incident = str(incident_class or "").strip()
    plan_by_incident = {
        "l5.product_completion_semantic_misreport": L5_PRODUCT_COMPLETION_TEST_PLAN_ID,
        "deployment.runtime_commit_drift": RUNTIME_IDENTITY_DRIFT_TEST_PLAN_ID,
        "release.closure_internal_failure": RELEASE_CLOSURE_FAILURE_TEST_PLAN_ID,
    }
    expected_plan = plan_by_incident.get(incident)
    if expected_plan is None:
        return ()
    selected_plan = str(test_plan_id or expected_plan)
    if selected_plan != expected_plan:
        return ()
    plan = protected_test_plan(selected_plan)
    return plan.allowed_files if plan is not None else ()


def build_test_plan_argv(plan_id: str, phase: str, *, candidate_python: str | Path) -> list[str]:
    plan = protected_test_plan(plan_id)
    if plan is None:
        raise ValueError("test_plan_not_registered")
    return plan.argv(phase, candidate_python=candidate_python)


def protected_test_plan_command_error(
    commands: Any,
    *,
    plan_id: str,
    candidate_python: str | Path,
) -> str:
    """Validate commands against the immutable plan-owned argv matrix.

    This helper is intentionally stricter than the legacy command grammar:
    every argv must be byte-for-byte equal to one of the trusted phase
    commands.  A proposal/provider cannot add an option, target, interpreter,
    or shell wrapper.  Production v2 callers construct the argv directly from
    :class:`ProtectedTestPlan`; this validator protects compatibility callers
    that still carry a serialized command list.
    """

    plan = protected_test_plan(plan_id)
    if plan is None:
        return "test_plan_not_registered"
    if not isinstance(commands, list) or not commands:
        return "protected_test_plan_commands_required"
    expected = {
        tuple(plan.argv(phase, candidate_python=candidate_python))
        for phase, _paths in plan.phases
    }
    for command in commands:
        if not isinstance(command, (list, tuple)) or not command:
            return "protected_test_plan_commands_must_be_argv"
        if tuple(str(part) for part in command) not in expected:
            return "protected_test_plan_command_not_allowed"
    return ""


def test_plan_manifest() -> dict[str, Any]:
    return {
        "schema": TEST_PLAN_SCHEMA,
        "plans": [
            {
                "plan_id": plan.plan_id,
                "digest": plan.digest,
                "allowed_files": list(plan.allowed_files),
                "full_suite_required": plan.full_suite_required,
                "phases": {phase: list(paths) for phase, paths in plan.phases},
            }
            for plan in _PLANS.values()
        ],
    }


__all__ = [
    "CODE_IMPLEMENTATION_CATALOG_TEST_PLAN",
    "CODE_IMPLEMENTATION_CATALOG_TEST_PLAN_ID",
    "L5_PRODUCT_COMPLETION_TEST_PLAN",
    "L5_PRODUCT_COMPLETION_TEST_PLAN_ID",
    "RUNTIME_IDENTITY_DRIFT_TEST_PLAN",
    "RUNTIME_IDENTITY_DRIFT_TEST_PLAN_ID",
    "RELEASE_CLOSURE_FAILURE_TEST_PLAN",
    "RELEASE_CLOSURE_FAILURE_TEST_PLAN_ID",
    "ProtectedTestPlan",
    "allowed_files_for_incident",
    "build_test_plan_argv",
    "protected_test_plan_command_error",
    "protected_test_plan",
    "protected_test_plan_digest",
    "test_plan_manifest",
]
