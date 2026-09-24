"""Regression guards for the business-closure audit fixes.

Each test pins a defect that was verified in the source tree, so a future change
cannot silently reopen it.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from eimemory.governance.code_automation_policy import v2_allowed_but_unreachable
from eimemory.governance.code_evolution_effects import CodeEvolutionEffectOwner
from eimemory.governance.code_evolution_path_policy import (
    DEFAULT_ALLOWED_PATH_GLOBS,
    DEFAULT_DENIED_PATH_GLOBS,
    path_allowed_for_evolution,
)
from eimemory.governance.code_evolution_test_plans import (
    RUNTIME_IDENTITY_DRIFT_TEST_PLAN,
)
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = ScopeRef(
    tenant_id="tenant-a",
    agent_id="agent-a",
    workspace_id="workspace-a",
    user_id="user-a",
)


@pytest.mark.parametrize(
    "path",
    [
        "eimemory/governance/promotion_manager.py",
        "eimemory/governance/promotion_code_apply.py",
        "eimemory/governance/promotion_watch.py",
        "eimemory/governance/isolated_evaluator.py",
        "eimemory/governance/autonomous_learning.py",
        "eimemory/governance/autonomous_evolution.py",
        "eimemory/governance/capability_acceptance.py",
        "eimemory/governance/capability_replay_executor.py",
        "eimemory/governance/capability_replay_packs.py",
        "eimemory/governance/l5_readiness.py",
        "eimemory/storage/code_evolution_store.py",
    ],
)
def test_promotion_and_evaluation_authority_plane_is_deny_self(path: str) -> None:
    ok, reason = path_allowed_for_evolution(
        path,
        allowed_path_globs=DEFAULT_ALLOWED_PATH_GLOBS,
        denied_path_globs=DEFAULT_DENIED_PATH_GLOBS,
    )
    assert ok is False
    assert reason == "deny_self"


def test_runtime_identity_plan_does_not_authorize_its_own_verifier() -> None:
    plan = RUNTIME_IDENTITY_DRIFT_TEST_PLAN
    assert plan.allows_path("deploy/runtime_identity_policy.py") is True
    assert plan.allows_path("tests/test_runtime_identity_policy.py") is False
    assert "tests/test_runtime_identity_policy.py" in v2_allowed_but_unreachable()


def test_trigram_index_keeps_cjk_candidate_pool(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    try:
        for index in range(8):
            store.append(_cjk_record(f"配置生产环境参数 {index}", salience=0.05))
        strong = store.append(_cjk_record("部署完成后必须配置生产环境", salience=1.0))
        records, report = store.search_with_diagnostics(
            query="配置生产环境",
            kinds=["memory"],
            scope=SCOPE,
            limit=5,
            recall_filters={"_exact_scope": True, "candidate_limit": 64},
            source_ids=["alpha"],
        )
        assert report["candidate_sources"]["fts"] >= 8
        assert report["candidate_sources"]["anchor"] >= 1
        assert strong.record_id in {record.record_id for record in records}
    finally:
        store.close()


def _cjk_record(text: str, *, salience: float):
    from eimemory.models.records import RecordEnvelope

    return RecordEnvelope.create(
        kind="memory",
        title=text,
        summary=text,
        content={"text": text, "memory_type": "fact"},
        scope=SCOPE,
        source="test",
        source_id="alpha",
        meta={"memory_type": "fact", "quality": {"capture_decision": "accept", "salience_score": salience}},
    )


def test_quality_repair_transaction_binds_deployed_commit(tmp_path) -> None:
    from eimemory.api.runtime import Runtime
    from eimemory.governance.l5_reader import _quality_repair_transaction

    runtime = Runtime.create(root=tmp_path)
    try:
        transaction = _quality_repair_transaction(runtime, runtime_scope=asdict(SCOPE))
    finally:
        runtime.close()
    assert transaction is None


def test_quality_repair_reader_rejects_unbound_commit() -> None:
    import inspect

    from eimemory.governance import l5_reader

    source = inspect.getsource(l5_reader)
    assert "expected_commit = lineage_commit" not in source
    assert "quality_repair_release_unbound" in source


def test_snapshot_emergency_brake_honours_kill_switch(tmp_path) -> None:
    owner = CodeEvolutionEffectOwner.__new__(CodeEvolutionEffectOwner)
    absent = tmp_path / "absent.disabled"
    present = tmp_path / "present.disabled"
    present.write_text("stop", encoding="utf-8")

    assert owner._snapshot_emergency_brake({}) == ""
    assert owner._snapshot_emergency_brake({"kill_switch_path": str(absent)}) == ""
    assert owner._snapshot_emergency_brake({"kill_switch_path": str(present)}) == "kill_switch_present"
    assert owner.EMERGENCY_BRAKE_REASONS == frozenset({"kill_switch_present"})
