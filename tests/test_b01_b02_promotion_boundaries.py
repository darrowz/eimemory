"""B01/B02: active-surface lease and applied-artifact rollback."""
from __future__ import annotations

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance import promotion_manager as pm
from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.promotion_manager import (
    _acquire_active_surface_lease,
    _release_active_surface_lease,
    promote_candidate,
    rollback_capability_candidate,
)


PASSING_EVAL = {
    "verdict": "pass",
    "scores": {"capability": 0.9, "safety": 1.0, "regression": 1.0, "cost": 0.8},
}


def test_active_surface_lease_is_exclusive(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    first = _acquire_active_surface_lease(runtime, timeout_sec=0.2)
    with pytest.raises(ValueError, match="active_surface_lease_unavailable"):
        _acquire_active_surface_lease(runtime, timeout_sec=0.2)
    _release_active_surface_lease(first)
    second = _acquire_active_surface_lease(runtime, timeout_sec=0.2)
    _release_active_surface_lease(second)


def test_promote_fails_closed_when_surface_lease_busy(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="b01",
        experiment_id="exp_b01",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="lease busy",
        target_capability="tool.routing",
    )

    def busy(*_a, **_k):
        raise ValueError("active_surface_lease_unavailable")

    monkeypatch.setattr(pm, "_acquire_active_surface_lease", busy)
    result = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        eval_result=PASSING_EVAL,
        health={"ok": True},
        apply=False,
    )
    assert result["ok"] is False
    assert result["blocked_reason"] == "active_surface_lease_unavailable"


def test_artifact_rollback_undoes_intent_pattern_when_possible(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="b02",
        experiment_id="exp_b02",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="pattern rollback",
        target_capability="tool.routing",
    )
    # Apply for real so an intent pattern artifact exists.
    promoted = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        eval_result=PASSING_EVAL,
        health={"ok": True},
        apply=True,
    )
    assert promoted.get("ok") is True, promoted
    assert promoted.get("applied_artifact_ids"), promoted
    result = rollback_capability_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        reason="b02 test",
    )
    assert result["ok"] is True, result
    stored = runtime.store.get_by_id(candidate_id, scope=scope)
    assert stored.status == "rolled_back"
    assert stored.meta.get("applied_artifact_ids") == []


def test_unsupported_code_artifact_rollback_stays_required(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="b02b",
        experiment_id="exp_b02b",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="unsupported artifact",
        target_capability="tool.routing",
    )
    candidate = runtime.store.get_by_id(candidate_id, scope=scope)
    candidate.meta["applied_artifact_ids"] = ["src/eimemory/foo.py"]
    candidate.meta["promotion_target"] = "code_patch"
    candidate.content["promotion_target"] = "code_patch"
    runtime.store.rewrite(candidate)
    result = rollback_capability_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        reason="cannot undo code path",
    )
    assert result["ok"] is False
    assert result["blocked_reason"] == "artifact_rollback_required"
    assert "src/eimemory/foo.py" in result.get("unsupported_artifact_kinds", [])


def test_code_artifact_rollback_restores_from_transaction_backups(tmp_path, monkeypatch) -> None:
    """B02: when code_apply backups exist and deploy was not applied, restore files."""
    from base64 import b64encode
    from hashlib import sha256

    from eimemory.governance.promotion_manager import CODE_APPLY_TRANSACTION_SOURCE
    from eimemory.models.records import RecordEnvelope, ScopeRef

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo.resolve()))
    target = repo / "src" / "pkg" / "mod.py"
    target.parent.mkdir(parents=True)
    original = b"original content\n"
    applied = b"applied content\n"
    target.write_bytes(applied)

    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu")
    playbook = runtime.store.append(
        RecordEnvelope.create(
            kind="learning_playbook",
            title="pb",
            summary="pb",
            scope=scope,
            source="test",
            status="active",
            content={"production_applied": False},
            meta={"promotion_target": "code_patch"},
        )
    )
    relative = "src/pkg/mod.py"
    backup = {
        "path": relative,
        "existed": True,
        "content_b64": b64encode(original).decode("ascii"),
        "content_sha256": sha256(original).hexdigest(),
    }
    planned = {"path": relative, "new_content_sha256": sha256(applied).hexdigest()}
    candidate = runtime.store.append(
        RecordEnvelope.create(
            kind="capability_candidate",
            title="c",
            summary="c",
            scope=scope,
            source="test",
            status="promoted",
            content={"promotion_target": "code_patch"},
            meta={
                "promotion_target": "code_patch",
                "applied_artifact_ids": [playbook.record_id, relative],
            },
        )
    )
    txn = runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="txn",
            summary="txn",
            scope=scope,
            source=CODE_APPLY_TRANSACTION_SOURCE,
            status="completed",
            content={
                "schema_version": 1,
                "transaction_type": "code_apply",
                "candidate_id": candidate.record_id,
                "repo_root": str(repo.resolve()),
                "backups": [backup],
                "planned_files": [planned],
                "stage": "completed",
            },
            meta={"candidate_id": candidate.record_id},
        )
    )
    playbook.content["transaction_id"] = txn.record_id
    playbook.content["candidate_id"] = candidate.record_id
    runtime.store.rewrite(playbook)
    candidate.meta["transaction_id"] = txn.record_id
    runtime.store.rewrite(candidate)

    result = rollback_capability_candidate(
        runtime,
        candidate_id=candidate.record_id,
        scope={"agent_id": "hongtu"},
        reason="b02 code undo",
    )
    assert result["ok"] is True, result
    assert target.read_bytes() == original
    assert runtime.store.get_by_id(playbook.record_id, scope={"agent_id": "hongtu"}).status == "rolled_back"
    stored = runtime.store.get_by_id(candidate.record_id, scope={"agent_id": "hongtu"})
    assert stored.status == "rolled_back"
    assert stored.meta.get("applied_artifact_ids") == []


def test_code_artifact_rollback_stays_required_after_production_deploy(tmp_path, monkeypatch) -> None:
    """B02: production deploy undo is out of band — stay fail-closed."""
    from base64 import b64encode
    from hashlib import sha256

    from eimemory.governance.promotion_manager import CODE_APPLY_TRANSACTION_SOURCE
    from eimemory.models.records import RecordEnvelope, ScopeRef

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo.resolve()))
    target = repo / "mod.py"
    original = b"orig\n"
    applied = b"new\n"
    target.write_bytes(applied)

    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu")
    playbook = runtime.store.append(
        RecordEnvelope.create(
            kind="learning_playbook",
            title="pb",
            summary="pb",
            scope=scope,
            source="test",
            status="active",
            content={"production_applied": True},
            meta={"promotion_target": "code_patch", "production_applied": True},
        )
    )
    relative = "mod.py"
    backup = {
        "path": relative,
        "existed": True,
        "content_b64": b64encode(original).decode("ascii"),
        "content_sha256": sha256(original).hexdigest(),
    }
    planned = {"path": relative, "new_content_sha256": sha256(applied).hexdigest()}
    candidate = runtime.store.append(
        RecordEnvelope.create(
            kind="capability_candidate",
            title="c",
            summary="c",
            scope=scope,
            source="test",
            status="promoted",
            content={"promotion_target": "code_patch"},
            meta={
                "promotion_target": "code_patch",
                "applied_artifact_ids": [playbook.record_id, relative],
            },
        )
    )
    txn = runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="txn",
            summary="txn",
            scope=scope,
            source=CODE_APPLY_TRANSACTION_SOURCE,
            status="completed",
            content={
                "schema_version": 1,
                "transaction_type": "code_apply",
                "candidate_id": candidate.record_id,
                "repo_root": str(repo.resolve()),
                "backups": [backup],
                "planned_files": [planned],
                "stage": "completed",
            },
            meta={"candidate_id": candidate.record_id},
        )
    )
    playbook.content["transaction_id"] = txn.record_id
    runtime.store.rewrite(playbook)
    candidate.meta["transaction_id"] = txn.record_id
    runtime.store.rewrite(candidate)

    result = rollback_capability_candidate(
        runtime,
        candidate_id=candidate.record_id,
        scope={"agent_id": "hongtu"},
        reason="cannot undo deploy",
    )
    assert result["ok"] is False
    assert result["blocked_reason"] == "artifact_rollback_required"
    assert target.read_bytes() == applied


def test_promote_post_apply_persist_failure_requires_reconciliation(tmp_path, monkeypatch) -> None:
    """SECURITY §4: mid-flight rewrite failure after side effects is not ok."""
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="sec4",
        experiment_id="exp_sec4",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="post-apply persist fail",
        target_capability="tool.routing",
    )

    original_rewrite = runtime.store.rewrite
    calls = {"n": 0}

    def boom(record, *args, **kwargs):
        calls["n"] += 1
        # Fail only when rewriting the capability_candidate after apply.
        if getattr(record, "kind", None) == "capability_candidate" and calls["n"] >= 1:
            # Allow earlier rewrites during apply; fail the post-apply candidate rewrite.
            status = str(getattr(record, "status", "") or "")
            if status in {"promoted", "watch", "shadow"} or record.meta.get("applied_artifact_ids"):
                raise RuntimeError("simulated_post_apply_rewrite_failure")
        return original_rewrite(record, *args, **kwargs)

    monkeypatch.setattr(runtime.store, "rewrite", boom)
    result = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        eval_result=PASSING_EVAL,
        health={"ok": True},
        apply=True,
    )
    assert result["ok"] is False
    assert result.get("requires_reconciliation") is True
    assert result.get("applied") is True


def test_check_promotion_watch_orphans_fail_closed(tmp_path) -> None:
    from eimemory.governance.promotion_watch import check_promotion_watch_orphans

    runtime = Runtime.create(root=tmp_path)
    report = check_promotion_watch_orphans(runtime, scope={"agent_id": "hongtu"}, limit=50)
    assert report["ok"] is True
    assert report["orphan_count"] == 0
    assert report.get("requires_reconciliation") is False
