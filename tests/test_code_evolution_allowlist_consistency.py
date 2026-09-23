from __future__ import annotations

from eimemory.governance.code_automation_policy import (
    v2_allowed_but_unreachable,
    v2_allowed_files,
)
from eimemory.governance import code_evolution_test_plans as plans


def test_v2_allowed_files_match_test_plans_or_explicit_unreachable() -> None:
    reachable: set[str] = set()
    for plan in plans.test_plan_manifest()["plans"]:
        # Catalog fixture is non-production; skip fixture.py from this contract.
        for path in plan["allowed_files"]:
            if path == "fixture.py":
                continue
            reachable.add(path)
    allowed = v2_allowed_files()
    unreachable = v2_allowed_but_unreachable()
    assert unreachable <= allowed
    assert allowed - unreachable == (allowed & reachable)
    assert not (unreachable & reachable)
