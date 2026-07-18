from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.experience import record_outcome_trace
from eimemory.governance.capability_ledger import record_capability_score
from eimemory.governance.evidence_contract import current_release_identity, release_identity_payload
from eimemory.governance.capability_replay_packs import (
    MANIFEST_REPORT_TYPE,
    MANIFEST_SCHEMA_VERSION,
    capability_replay_manifest_digest,
)
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = {"agent_id": "agent-l5-readiness", "workspace_id": "l5-readiness", "user_id": "darrow"}


def test_l5_readiness_report_is_read_only_by_default_and_surfaces_gaps(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        before = runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        report = runtime.build_l5_readiness_report(scope=SCOPE)
        after = runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["report_type"] == "l5_readiness_report"
    assert report["current_stage"] == "L3.5"
    assert report["persisted_record_id"] == ""
    assert before == after
    assert report["capability_gaps"]
    assert any(gap["capability"] == "search.discovery" for gap in report["capability_gaps"])
    assert "deployment" in report["risk_boundary"]
    assert report["next_actions"]


def test_l5_readiness_report_uses_existing_evidence_without_running_learning(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_ref = ScopeRef.from_dict(SCOPE)
    try:
        for capability in ("memory.recall", "tool.routing", "knowledge.intake", "safety.boundary"):
            record_capability_score(
                runtime,
                scope=SCOPE,
                loop_id="readiness-test",
                capability=capability,
                score=0.82,
                evidence_record_ids=[f"{capability}-e1", f"{capability}-e2", f"{capability}-e3"],
            )
        for index in range(3):
            runtime.store.append(
                RecordEnvelope.create(
                    kind="replay_result",
                    title=f"Replay {index}",
                    summary="pass",
                    scope=scope_ref,
                        content={
                            "report_type": "capability_replay_pack",
                            "verdict": "pass",
                            "capability": "memory.recall",
                            "hit": True,
                            "evidence_source_id": f"memory-replay-{index}",
                        },
                        meta={
                            "report_type": "capability_replay_pack",
                            "verdict": "pass",
                            "capability": "memory.recall",
                            "hit": True,
                            "evidence_source_id": f"memory-replay-{index}",
                        },
                        source="eimemory.capability_replay",
                    )
            )
        runtime.store.append(
            RecordEnvelope.create(
                kind="promotion_request",
                title="Readiness promotion",
                summary="promoted",
                scope=scope_ref,
                status="promoted",
                content={"action": "promote", "target_capability": "memory.recall"},
                meta={"action": "promote", "target_capability": "memory.recall"},
            )
        )

        report = runtime.build_l5_readiness_report(scope=SCOPE, persist=True)
        stored = runtime.store.get_by_id(report["persisted_record_id"], scope=SCOPE)
    finally:
        runtime.close()

    assert report["current_stage"] == "L4"
    assert report["evidence_counts"]["replay_result"] == 3
    assert report["verified_replay"]["observed_executed_count"] == 3
    assert report["verified_replay"]["executed_count"] == 0
    assert report["evidence_counts"]["promotion_applied"] == 1
    assert stored.kind == "reflection"
    assert stored.meta["report_type"] == "l5_readiness_report"


def test_l5_readiness_counts_policy_rollout_rollback_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        pattern_id = "readiness-policy-rollback"
        runtime.upsert_intent_pattern(
            {
                "id": pattern_id,
                "pattern": "readiness rollback rehearsal",
                "default_event_type": "repair",
                "interpreted_intent": "non-destructive readiness rollback",
                "confidence": 0.9,
            },
            scope=SCOPE,
        )
        rollback = runtime.rollback_intent_pattern(
            pattern_id,
            scope=SCOPE,
            reason="readiness should count policy rollback ledger",
        )

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert rollback["ok"] is True
    assert report["hard_metrics"]["rollback_count"] == 1
    assert report["evidence_counts"]["rollback_or_quarantine"] == 1


def test_l5_readiness_rejects_status_only_rollback_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_ref = ScopeRef.from_dict(SCOPE)
    try:
        runtime.store.append(
            RecordEnvelope.create(
                kind="promotion_request",
                title="Unverified rollback",
                summary="status only",
                scope=scope_ref,
                status="rolled_back",
                content={"action": "rollback"},
                meta={"action": "rollback"},
            )
        )
        runtime.store.sqlite.upsert_policy_rollout_ledger_payload(
            {
                "id": "readiness-blocked-rollback",
                "scope": SCOPE,
                "action_type": "rollback",
                "promotion_id": "readiness-blocked",
                "budget_decision": "blocked",
                "applied_pattern_id": "",
                "details": {"blocked": True, "status": "rolled_back"},
            }
        )

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["evidence_counts"]["rollback_or_quarantine"] == 0
    assert report["hard_metrics"]["rollback_count"] == 0


def test_l5_readiness_does_not_treat_replay_only_weak_capabilities_as_l5(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_l5_prerequisites(
            runtime,
            scope=SCOPE,
            weak_outcomes=True,
            patch_samples=10,
            execute_weak_replays=False,
            assessment_complete=True,
            verified_patch_evidence=True,
        )

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["current_stage"] != "L5"
    assert report["readiness_score"] < 1.0
    assert report["verified_replay"]["weak_capabilities_missing"] == sorted(WEAK_CAPABILITIES)


def test_l5_readiness_rejects_status_only_patch_samples(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_l5_prerequisites(
            runtime,
            scope=SCOPE,
            weak_outcomes=True,
            patch_samples=2,
            execute_weak_replays=True,
            assessment_complete=True,
            verified_patch_evidence=False,
            verified_live_tasks=False,
        )

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["current_stage"] != "L5"
    assert report["hard_metric_quality"]["auto_patch_success_rate"]["sample_count"] == 1
    assert report["hard_metrics"]["auto_patch_success_rate"] == 1.0


def test_l5_readiness_reaches_l5_only_with_attributed_weak_outcomes_and_patch_samples(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_l5_prerequisites(
            runtime,
            scope=SCOPE,
            weak_outcomes=True,
            patch_samples=10,
            execute_weak_replays=True,
            assessment_complete=True,
            verified_patch_evidence=True,
        )

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["current_stage"] == "L5"
    assert report["readiness_score"] == 1.0
    assert report["hard_metric_quality"]["auto_patch_success_rate"]["sufficient"] is True
    assert report["weak_outcome_evidence"]["missing"] == []
    assert report["verified_replay"]["weak_capabilities_missing"] == []
    assert all(item["distinct_evidence_count"] == 3 for item in report["verified_replay"]["by_capability"].values())
    assert report["latest_l5_assessment"]["complete"] is True


def test_l5_readiness_reports_data_accumulating_without_current_release_real_tasks(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_l5_prerequisites(
            runtime,
            scope=SCOPE,
            weak_outcomes=True,
            patch_samples=10,
            execute_weak_replays=True,
            assessment_complete=True,
            verified_patch_evidence=True,
            verified_live_tasks=False,
        )
        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["current_stage"] == "data_accumulating"
    assert report["readiness_score"] == 0.9
    assert report["live_task_gate"]["ok"] is False
    assert report["live_task_gate"]["sample_count"] == 0
    assert report["live_task_gate"]["sample_deficit"] == 10
    assert report["live_task_gate"]["task_type_deficit"] == 5
    assert any("real user tasks" in action for action in report["next_actions"])


def test_l5_readiness_uses_latest_execution_batch_instead_of_legacy_case_ids(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_ref = ScopeRef.from_dict(SCOPE)
    try:
        _seed_l5_prerequisites(
            runtime,
            scope=SCOPE,
            weak_outcomes=True,
            patch_samples=10,
            execute_weak_replays=True,
            assessment_complete=True,
            verified_patch_evidence=True,
        )
        for capability in sorted(WEAK_CAPABILITIES):
            for index in range(3):
                case_id = f"legacy-{capability}-{index}"
                payload = {
                    "report_type": "capability_replay_pack",
                    "capability": capability,
                    "execution_id": f"legacy-{capability}",
                    "executed_at": "2025-01-01T00:00:00+00:00",
                    "case": {"case_id": case_id},
                    "result": {
                        "case_id": case_id,
                        "verdict": "pass",
                        "hit": True,
                        "evidence_source_id": f"legacy-source-{capability}-{index}",
                    },
                }
                runtime.store.append(
                    RecordEnvelope.create(
                        kind="replay_result",
                        title=f"Legacy replay {case_id}",
                        summary="legacy pass without a v2 replay trace",
                        scope=scope_ref,
                        content=payload,
                        meta=payload,
                        source="eimemory.capability_replay",
                    )
                )
        for capability in ("code.implementation", "office.daily_task"):
            for index in range(3):
                case_id = f"non-weak-{capability}-{index}"
                payload = {
                    "report_type": "capability_replay_pack",
                    "capability": capability,
                    "execution_id": f"non-weak-{capability}",
                    "executed_at": "2027-01-01T00:00:00+00:00",
                    "case": {"case_id": case_id},
                    "result": {
                        "case_id": case_id,
                        "verdict": "pass",
                        "hit": True,
                        "evidence_source_id": f"non-weak-source-{capability}-{index}",
                    },
                }
                runtime.store.append(
                    RecordEnvelope.create(
                        kind="replay_result",
                        title=f"Non-weak replay {case_id}",
                        summary="non-weak replay must not change the weak-capability readiness rate",
                        scope=scope_ref,
                        content=payload,
                        meta=payload,
                        source="eimemory.capability_replay",
                    )
                )

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["verified_replay"]["executed_count"] == 12
    assert report["verified_replay"]["pass_count"] == 12
    assert report["verified_replay"]["fail_count"] == 0
    assert report["verified_replay"]["pass_rate"] == 1.0
    assert report["verified_replay"]["rejection_reasons"] == {}
    assert report["verified_replay"]["weak_capabilities_missing"] == []
    assert set(report["verified_replay"]["manifest_record_ids"]) == WEAK_CAPABILITIES
    assert len(set(report["verified_replay"]["manifest_record_ids"].values())) == 1
    assert report["current_stage"] == "L5"


def test_l5_readiness_fails_closed_on_latest_incomplete_manifest_even_with_older_passes(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_ref = ScopeRef.from_dict(SCOPE)
    try:
        _seed_l5_prerequisites(
            runtime,
            scope=SCOPE,
            weak_outcomes=True,
            patch_samples=10,
            execute_weak_replays=True,
            assessment_complete=True,
            verified_patch_evidence=True,
        )
        search_record = next(
            record
            for record in runtime.store.list_records(kinds=["replay_result"], scope=SCOPE, limit=100)
            if record.meta.get("report_type") == "capability_replay_pack"
            and record.meta.get("capability") == "search.discovery"
        )
        manifest_payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "report_type": MANIFEST_REPORT_TYPE,
            "execution_id": "partial-new-execution",
            "executed_at": "2000-01-01T00:00:00+08:00",
            "capabilities": ["search.discovery"],
            "sequence_by_capability": {"search.discovery": 999},
            "expected_case_ids": {
                "search.discovery": ["search_recent_source", "search_trending_github", "search_primary_source"]
            },
            "member_record_ids": {"search.discovery": [search_record.record_id]},
            "member_digests": {"search.discovery": {}},
            "complete": False,
        }
        manifest_payload["manifest_digest"] = capability_replay_manifest_digest(manifest_payload)
        partial_manifest = RecordEnvelope.create(
                kind="replay_result",
                title="Partial latest replay manifest",
                summary="must block fallback to the older complete manifest",
                scope=scope_ref,
                source="eimemory.capability_replay",
                content=manifest_payload,
                meta={
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "report_type": MANIFEST_REPORT_TYPE,
                    "execution_id": manifest_payload["execution_id"],
                    "manifest_digest": manifest_payload["manifest_digest"],
                    "complete": False,
                },
                provenance={
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "report_type": MANIFEST_REPORT_TYPE,
                    "execution_id": manifest_payload["execution_id"],
                    "manifest_digest": manifest_payload["manifest_digest"],
                },
            )
        partial_manifest.time.created_at = "2030-01-01T00:00:00+00:00"
        partial_manifest.time.updated_at = "2030-01-01T00:00:00+00:00"
        partial_manifest.time.occurred_at = "2030-01-01T00:00:00+00:00"
        runtime.store.append(partial_manifest)

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["verified_replay"]["manifest_rejection_reasons"] == {
        "search.discovery": "manifest_high_water_mismatch"
    }
    assert "search.discovery" in report["verified_replay"]["weak_capabilities_missing"]
    assert report["current_stage"] != "L5"


def test_l5_readiness_rejects_manifest_identity_time_and_membership_tampering(tmp_path) -> None:
    cases = {
        "missing_execution": "manifest_high_water_execution_mismatch",
        "future_time": "manifest_time_in_future",
        "duplicate_member": "manifest_member_count_mismatch",
    }
    for mode, expected_reason in cases.items():
        runtime = Runtime.create(root=tmp_path / mode)
        try:
            _seed_l5_prerequisites(
                runtime,
                scope=SCOPE,
                weak_outcomes=True,
                patch_samples=10,
                execute_weak_replays=True,
                assessment_complete=True,
                verified_patch_evidence=True,
            )
            manifest = next(
                record
                for record in runtime.store.list_records(kinds=["replay_result"], scope=SCOPE, limit=100)
                if record.meta.get("report_type") == MANIFEST_REPORT_TYPE
            )
            if mode == "missing_execution":
                manifest.content["execution_id"] = ""
            elif mode == "future_time":
                manifest.content["executed_at"] = "2100-01-01T00:00:00+00:00"
            else:
                members = list(manifest.content["member_record_ids"]["search.discovery"])
                manifest.content["member_record_ids"]["search.discovery"] = [members[0], members[0], members[1]]
            digest = capability_replay_manifest_digest(manifest.content)
            manifest.content["manifest_digest"] = digest
            manifest.meta["manifest_digest"] = digest
            manifest.provenance["manifest_digest"] = digest
            if mode == "missing_execution":
                manifest.meta["execution_id"] = ""
                manifest.provenance["execution_id"] = ""
            runtime.store.rewrite(manifest)

            report = runtime.build_l5_readiness_report(scope=SCOPE)
        finally:
            runtime.close()

        assert report["verified_replay"]["manifest_rejection_reasons"]["search.discovery"] == expected_reason
        assert "search.discovery" in report["verified_replay"]["weak_capabilities_missing"]
        assert report["current_stage"] != "L5"


def test_l5_readiness_uses_monotonic_batch_order_after_future_clock_skew(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_l5_prerequisites(
            runtime,
            scope=SCOPE,
            weak_outcomes=True,
            patch_samples=10,
            execute_weak_replays=True,
            assessment_complete=True,
            verified_patch_evidence=True,
        )
        first_manifest = next(
            record
            for record in runtime.store.list_records(kinds=["replay_result"], scope=SCOPE, limit=100)
            if record.meta.get("report_type") == MANIFEST_REPORT_TYPE
        )
        first_manifest.time.created_at = "2100-01-01T00:00:00+00:00"
        first_manifest.time.updated_at = "2100-01-01T00:00:01+00:00"
        runtime.store.rewrite(first_manifest)
        for score in runtime.store.list_records(kinds=["capability_score"], scope=SCOPE, limit=100):
            if score.meta.get("manifest_record_id") == first_manifest.record_id:
                score.time.created_at = "2100-01-01T00:00:00+00:00"
                score.time.updated_at = "2100-01-01T00:00:01+00:00"
                runtime.store.rewrite(score)

        runtime.run_capability_replay_case = lambda case: {  # type: ignore[attr-defined]
            "verdict": "fail",
            "hit": False,
            "observed": f"failed:{case['case_id']}",
            "reason": "regression_detected",
        }
        runtime.build_capability_replay_packs(
            scope=SCOPE,
            capabilities=sorted(WEAK_CAPABILITIES),
            persist=True,
            loop_id="post-clock-recovery-failure",
        )

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["verified_replay"]["executed_count"] == 12
    assert report["verified_replay"]["pass_count"] == 0
    assert report["verified_replay"]["fail_count"] == 12
    assert report["current_stage"] != "L5"


def test_l5_readiness_rejects_disabled_manifest_and_high_water(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_l5_prerequisites(
            runtime,
            scope=SCOPE,
            weak_outcomes=True,
            patch_samples=10,
            execute_weak_replays=True,
            assessment_complete=True,
            verified_patch_evidence=True,
        )
        manifest = next(
            record
            for record in runtime.store.list_records(kinds=["replay_result"], scope=SCOPE, limit=100)
            if record.meta.get("report_type") == MANIFEST_REPORT_TYPE
        )
        manifest.status = "quarantined"
        runtime.store.rewrite(manifest)
        for score in runtime.store.list_records(kinds=["capability_score"], scope=SCOPE, limit=100):
            if score.meta.get("manifest_record_id") == manifest.record_id:
                score.status = "disabled"
                runtime.store.rewrite(score)

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert set(report["verified_replay"]["manifest_rejection_reasons"].values()) == {
        "manifest_high_water_status_invalid"
    }
    assert report["verified_replay"]["weak_capabilities_missing"] == sorted(WEAK_CAPABILITIES)
    assert report["current_stage"] != "L5"


def test_l5_readiness_rejects_disabled_member_probe_and_trace(tmp_path) -> None:
    for target in ("member", "probe", "trace"):
        runtime = Runtime.create(root=tmp_path / target)
        try:
            _seed_l5_prerequisites(
                runtime,
                scope=SCOPE,
                weak_outcomes=True,
                patch_samples=10,
                execute_weak_replays=True,
                assessment_complete=True,
                verified_patch_evidence=True,
            )
            manifest = next(
                record
                for record in runtime.store.list_records(kinds=["replay_result"], scope=SCOPE, limit=100)
                if record.meta.get("report_type") == MANIFEST_REPORT_TYPE
            )
            member_id = manifest.content["member_record_ids"]["search.discovery"][0]
            member = runtime.store.get_by_id(member_id, scope=SCOPE)
            assert member is not None
            if target == "member":
                record = member
            elif target == "probe":
                record = runtime.store.get_by_id(member.content["result"]["probe_source_id"], scope=SCOPE)
            else:
                record = runtime.store.get_by_id(member.content["result"]["trace_record_id"], scope=SCOPE)
            assert record is not None
            record.status = "quarantined"
            runtime.store.rewrite(record)

            report = runtime.build_l5_readiness_report(scope=SCOPE)
        finally:
            runtime.close()

        assert "search.discovery" in report["verified_replay"]["weak_capabilities_missing"]
        assert report["current_stage"] != "L5"


def test_l5_readiness_does_not_fallback_after_latest_batch_is_physically_removed(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_l5_prerequisites(
            runtime,
            scope=SCOPE,
            weak_outcomes=True,
            patch_samples=10,
            execute_weak_replays=True,
            assessment_complete=True,
            verified_patch_evidence=True,
        )
        runtime.run_capability_replay_case = lambda case: {  # type: ignore[attr-defined]
            "verdict": "fail",
            "hit": False,
            "observed": f"failed:{case['case_id']}",
            "reason": "regression_detected",
        }
        latest = runtime.build_capability_replay_packs(
            scope=SCOPE,
            capabilities=sorted(WEAK_CAPABILITIES),
            persist=True,
            loop_id="latest-failing-batch",
        )
        deleted_ids = [latest["manifest_record_id"], *latest["score_record_ids"], *latest["persisted_replay_ids"]]
        placeholders = ",".join("?" for _ in deleted_ids)
        runtime.store.sqlite.conn.execute(
            f"DELETE FROM records WHERE record_id IN ({placeholders})",
            deleted_ids,
        )
        runtime.store.sqlite.conn.commit()

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert set(report["verified_replay"]["manifest_rejection_reasons"].values()) == {
        "manifest_log_high_water_mismatch"
    }
    assert report["verified_replay"]["weak_capabilities_missing"] == sorted(WEAK_CAPABILITIES)
    assert report["current_stage"] != "L5"


def test_l5_readiness_rejects_same_sequence_manifest_collision(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_l5_prerequisites(
            runtime,
            scope=SCOPE,
            weak_outcomes=True,
            patch_samples=10,
            execute_weak_replays=True,
            assessment_complete=True,
            verified_patch_evidence=True,
        )
        original = next(
            record
            for record in runtime.store.list_records(kinds=["replay_result"], scope=SCOPE, limit=100)
            if record.meta.get("report_type") == MANIFEST_REPORT_TYPE
        )
        duplicate = RecordEnvelope.create(
            kind="replay_result",
            title="Concurrent duplicate manifest",
            summary="same sequence must fail closed",
            scope=ScopeRef.from_dict(SCOPE),
            source=original.source,
            status="active",
            content=dict(original.content),
            meta=dict(original.meta),
            provenance=dict(original.provenance),
            evidence=list(original.evidence),
        )
        duplicate.time.created_at = original.time.created_at
        duplicate.time.updated_at = original.time.updated_at
        duplicate.time.occurred_at = original.time.occurred_at
        runtime.store.append(duplicate)
        for score in runtime.store.list_records(kinds=["capability_score"], scope=SCOPE, limit=100):
            if score.meta.get("manifest_record_id") == original.record_id:
                score.meta["manifest_record_id"] = duplicate.record_id
                runtime.store.rewrite(score)

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert set(report["verified_replay"]["manifest_rejection_reasons"].values()) == {
        "manifest_sequence_collision"
    }
    assert report["verified_replay"]["weak_capabilities_missing"] == sorted(WEAK_CAPABILITIES)
    assert report["current_stage"] != "L5"


def test_l5_readiness_rejects_not_run_weak_replays(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_l5_prerequisites(
            runtime,
            scope=SCOPE,
            weak_outcomes=True,
            patch_samples=10,
            execute_weak_replays=False,
            assessment_complete=True,
            verified_patch_evidence=True,
        )

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["current_stage"] != "L5"
    assert report["verified_replay"]["executed_count"] == 0
    assert report["verified_replay"]["weak_capabilities_missing"] == sorted(WEAK_CAPABILITIES)
    # Unexecuted replays are excluded from readiness evidence rather than
    # counted as a completed replay batch.
    assert report["verified_replay"]["not_run_count"] == 0


def test_l5_readiness_rejects_incomplete_latest_assessment(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_l5_prerequisites(
            runtime,
            scope=SCOPE,
            weak_outcomes=True,
            patch_samples=10,
            execute_weak_replays=True,
            assessment_complete=False,
            verified_patch_evidence=True,
        )

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["current_stage"] != "L5"
    assert report["latest_l5_assessment"]["complete"] is False
    assert report["latest_l5_assessment"]["missing_evidence"] == ["promotion_or_block"]


def test_l5_readiness_rejects_untrusted_pass_records_and_assessment(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_ref = ScopeRef.from_dict(SCOPE)
    try:
        _seed_l5_prerequisites(
            runtime,
            scope=SCOPE,
            weak_outcomes=True,
            patch_samples=10,
            execute_weak_replays=False,
            assessment_complete=False,
            verified_patch_evidence=True,
            assessment_present=False,
        )
        for capability in WEAK_CAPABILITIES:
            for index in range(3):
                runtime.store.append(
                    RecordEnvelope.create(
                        kind="replay_result",
                        title=f"untrusted {capability} {index}",
                        summary="pass",
                        scope=scope_ref,
                        content={"verdict": "pass", "capability": capability, "hit": True},
                        meta={"verdict": "pass", "capability": capability, "hit": True},
                        source="external.untrusted",
                    )
                )
        runtime.store.append(
            RecordEnvelope.create(
                kind="l5_assessment",
                title="untrusted assessment",
                summary="complete",
                scope=scope_ref,
                content={"report_type": "l5_assessment", "level": "L5", "complete": True, "missing_evidence": []},
                meta={"report_type": "l5_assessment", "level": "L5", "missing_evidence_count": 0},
                source="external.untrusted",
            )
        )

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["current_stage"] != "L5"
    assert report["verified_replay"]["weak_capabilities_missing"] == sorted(WEAK_CAPABILITIES)
    assert report["latest_l5_assessment"]["complete"] is False
    assert report["latest_l5_assessment"]["trusted"] is False


def test_l5_readiness_rejects_legacy_ledger_outcomes_without_verified_contracts(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        for capability in WEAK_CAPABILITIES:
            record_capability_score(
                runtime,
                scope=SCOPE,
                loop_id="historical_legacy_attribution",
                capability=capability,
                score=0.82,
                evidence_record_ids=[f"legacy-{capability}-{index}" for index in range(3)],
                evidence_sources=["outcome_trace"],
            )

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["weak_outcome_evidence"]["counts"] == {capability: 0 for capability in sorted(WEAK_CAPABILITIES)}
    assert report["weak_outcome_evidence"]["missing"] == sorted(WEAK_CAPABILITIES)
    weak_gaps = {
        gap["capability"]: gap
        for gap in report["capability_gaps"]
        if gap["capability"] in WEAK_CAPABILITIES
    }
    assert set(weak_gaps) == WEAK_CAPABILITIES
    assert {gap["outcome_evidence_count"] for gap in weak_gaps.values()} == {0}
    assert {gap["reason"] for gap in weak_gaps.values()} == {"insufficient_attributed_outcome_evidence"}
    assert report["current_stage"] != "L5"


def test_l5_readiness_reparses_contract_chain_and_rejects_forged_probe_digest(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        acceptance = runtime.run_capability_acceptance(scope=SCOPE, persist=True)
        runtime.build_capability_replay_packs(
            scope=SCOPE,
            capabilities=sorted(WEAK_CAPABILITIES),
            persist=True,
            loop_id="forged_probe_readiness",
        )
        probe = runtime.store.get_by_id(acceptance["results"][0]["probe_id"], scope=SCOPE)
        assert probe is not None
        probe.provenance["artifact_digest"] = "forged-artifact-digest"
        runtime.store.append(probe)

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["verified_replay"]["pass_count"] == 11
    assert report["verified_replay"]["fail_count"] == 1
    assert report["verified_replay"]["rejection_reasons"] == {"probe_artifact_digest_mismatch": 1}
    assert "search.discovery" in report["verified_replay"]["weak_capabilities_missing"]


def test_cli_l5_readiness_returns_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    exit_code = cli_main(["learn", "l5-readiness", "--limit", "25", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["report_type"] == "l5_readiness_report"
    assert payload["current_stage"] == "L3.5"
    assert payload["persisted_record_id"] == ""


WEAK_CAPABILITIES = {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}
READINESS_CAPABILITIES = {
    "memory.recall",
    "tool.routing",
    "knowledge.intake",
    "proactive.judgment",
    "search.discovery",
    "research.synthesis",
    "operations.uumit",
    "device.control",
    "safety.boundary",
}


def _seed_l5_prerequisites(
    runtime: Runtime,
    *,
    scope: dict,
    weak_outcomes: bool,
    patch_samples: int,
    execute_weak_replays: bool = False,
    assessment_complete: bool = False,
    verified_patch_evidence: bool = False,
    assessment_present: bool = True,
    verified_live_tasks: bool = True,
) -> None:
    scope_ref = ScopeRef.from_dict(scope)
    release = _seed_current_release(runtime, scope=scope)
    release_payload = release_identity_payload(release)
    for capability in READINESS_CAPABILITIES - WEAK_CAPABILITIES:
        record_capability_score(
            runtime,
            scope=scope,
            loop_id="seed_l5",
            capability=capability,
            score=0.84,
            evidence_record_ids=[f"{capability}-e1", f"{capability}-e2", f"{capability}-e3"],
        )
    if execute_weak_replays:
        runtime.run_capability_acceptance(scope=scope, persist=True)
    runtime.build_capability_replay_packs(scope=scope, capabilities=sorted(WEAK_CAPABILITIES), persist=True, loop_id="seed_weak_replay")
    if weak_outcomes and not execute_weak_replays:
        runtime.run_capability_acceptance(scope=scope, persist=True)
        for capability, task_type, summary in (
            ("search.discovery", "搜索最近 GitHub 热门项目", "搜索 GitHub created range stars sort source verification"),
            ("research.synthesis", "research_synthesis", "Synthesized research papers and claim evidence into a brief"),
            ("operations.uumit", "uumit_delivery", "UUMit delivery checklist and customer acceptance verified"),
            ("device.control", "media_playback", "Device speaker playback controlled and physical audio output verified"),
        ):
            for index in range(3):
                record_outcome_trace(
                    runtime,
                    {
                        "trace_id": f"{capability}-{index}",
                        "idempotency_key": f"idem-{capability}-{index}",
                        "task_type": task_type,
                        "input_summary": summary,
                        "outcome": {"status": "success"},
                        "verifier": {"passed": True},
                        "feedback": {"summary": "verified"},
                    },
                    scope=scope,
                )
    for index in range(10):
        runtime.store.append(
            RecordEnvelope.create(
                kind="replay_result",
                title=f"L5 replay {index}",
                summary="pass",
                scope=scope_ref,
                content={"verdict": "pass", "capability": "memory.recall", "hit": True},
                meta={"verdict": "pass", "capability": "memory.recall", "hit": True},
            )
        )
    for kind in ("l5_world_model", "l5_strategic_roadmap", "l5_closed_loop"):
        runtime.store.append(
            RecordEnvelope.create(
                kind=kind,
                title=kind,
                summary="present",
                scope=scope_ref,
                content={"report_type": kind, "evidence_class": "structural", **release_payload},
                meta={"report_type": kind, "evidence_class": "structural", **release_payload},
                source="eimemory.l5_loop",
            )
        )
    if assessment_present:
        assessment_payload = {
            "report_type": "l5_assessment",
            "schema_version": "l5_closed_loop.v1",
            "evidence_class": "structural",
            **release_payload,
            "level": "L5" if assessment_complete else "L4",
            "complete": assessment_complete,
            "missing_evidence": [] if assessment_complete else ["promotion_or_block"],
        }
        runtime.store.append(
            RecordEnvelope.create(
                kind="l5_assessment",
                title="l5_assessment",
                summary="complete" if assessment_complete else "incomplete",
                scope=scope_ref,
                content=assessment_payload,
                meta={
                    "report_type": "l5_assessment",
                    "schema_version": "l5_closed_loop.v1",
                    "evidence_class": "structural",
                    **release_payload,
                    "level": assessment_payload["level"],
                    "missing_evidence_count": len(assessment_payload["missing_evidence"]),
                },
                source="eimemory.l5_loop",
            )
        )
    for index in range(3):
        runtime.store.append(
            RecordEnvelope.create(
                kind="promotion_request",
                title=f"Promotion {index}",
                summary="promoted",
                scope=scope_ref,
                status="promoted",
                content={"action": "promote", "target_capability": "memory.recall"},
                meta={"action": "promote", "target_capability": "memory.recall"},
            )
        )
    pattern_id = "seed-l5-policy-rollback"
    runtime.upsert_intent_pattern(
        {
            "id": pattern_id,
            "pattern": "seed L5 rollback",
            "default_event_type": "repair",
            "interpreted_intent": "verify reversible L5 policy path",
            "confidence": 0.9,
            "status": "active",
        },
        scope=scope,
    )
    runtime.rollback_intent_pattern(pattern_id, scope=scope, reason="seed verified L5 rollback", auto=False)
    for index in range(patch_samples):
        commit = f"{index + 1:040x}"
        prior_commit = f"{index:040x}"
        version = "1.9.16"
        release_path = f"/opt/eimemory/releases/{commit}"
        patch_evidence = (
            {
                "gate": {"ok": True},
                "side_effect": {
                    "ok": True,
                    "production_applied": True,
                    "deployment_executed": True,
                    "verification": {"ok": True, "skipped": False},
                    "deployment": {"ok": True, "skipped": False, "release_path": release_path},
                    "post_deploy_health": {
                        "ok": True,
                        "skipped": False,
                        "commit": commit,
                        "version": version,
                        "release_path": release_path,
                    },
                    "commit": {"ok": True, "commit_sha": commit},
                    "release": {"version": version, "release_path": release_path},
                    "rollback_evidence": {
                        "service_name": "eimemory-rpc.service",
                        "prior_commit_sha": prior_commit,
                        "rollback_command": f"git reset --hard {prior_commit}",
                    },
                },
            }
            if verified_patch_evidence
            else {}
        )
        runtime.store.append(
            RecordEnvelope.create(
                kind="promotion_request",
                title=f"Patch promotion {index}",
                summary="deployed",
                scope=scope_ref,
                status="deployed",
                content={
                    "candidate_id": f"readiness-patch-{index}",
                    "action": "code_patch",
                    "promotion_target": "code_patch",
                    **patch_evidence,
                },
                meta={
                    "candidate_id": f"readiness-patch-{index}",
                    "action": "code_patch",
                    "promotion_target": "code_patch",
                    "gate_ok": bool(patch_evidence),
                    "commit_sha": commit if patch_evidence else "",
                    "version": version if patch_evidence else "",
                    "release_path": release_path if patch_evidence else "",
                },
            )
        )
    if verified_live_tasks:
        _seed_verified_live_tasks(runtime, scope=scope)


def _seed_verified_live_tasks(runtime: Runtime, *, scope: dict) -> None:
    scope_ref = ScopeRef.from_dict(scope)
    release = current_release_identity(runtime, scope_ref)
    assert release is not None
    task_types = ("repo.deploy", "memory.recall", "knowledge.intake", "tool.routing", "feishu.delivery")
    for index in range(10):
        task_type = task_types[index % len(task_types)]
        trace_id = f"verified-real-task-{index}"
        session_id = f"openclaw-session-{index}"
        event = runtime.store.record_event(
            {
                "source": "openclaw.agent_end",
                "hook": "agent_end",
                "session_id": session_id,
                "event_type": task_type,
                "outcome_trace_id": trace_id,
                "outcome_trace_task_type": task_type,
                "external_correlation_id": f"feishu-message-{index}",
                "message_id": f"feishu-message-{index}",
                **release_identity_payload(release),
            },
            scope=scope_ref,
        )
        runtime.record_outcome(
            event["id"],
            {
                "outcome": "good",
                "success": True,
                "verified": True,
                "source": "openclaw.agent_end",
                "source_trust": "system_verified",
            },
            scope=scope,
        )
        result = record_outcome_trace(
            runtime,
            {
                "source": "openclaw.agent_end",
                "session_id": session_id,
                "trace_id": trace_id,
                "task_type": task_type,
                "input_summary": f"Verified OpenClaw task {index}",
                "outcome": {"status": "success", "success": True, "rehearsal": False},
                "verifier": {
                    "passed": True,
                    "method": "openclaw.agent_end",
                    "evidence_refs": [event["id"]],
                },
            },
            scope=scope,
        )
        assert result["ok"] is True


def _seed_current_release(runtime: Runtime, *, scope: dict):
    scope_ref = ScopeRef.from_dict(scope)
    commit = "f" * 40
    runtime._test_runtime_commit = commit
    existing = current_release_identity(runtime, scope_ref)
    if existing is not None:
        return existing
    release_path = f"/opt/eimemory/releases/{commit}"
    receipt_payload = {
        "report_type": "deployment_receipt",
        "promotion_target": "code_patch",
        "action": "code_patch",
        "gate": {"ok": True, "receipt_verified": True},
        "side_effect": {
            "ok": True,
            "production_applied": True,
            "deployment_executed": True,
            "verification": {"ok": True, "skipped": False},
            "deployment": {"ok": True, "skipped": False, "release_path": release_path},
            "post_deploy_health": {
                "ok": True,
                "skipped": False,
                "commit": commit,
                "version": "1.9.70",
                "release_path": release_path,
            },
            "commit": {"commit_sha": commit},
            "release": {"version": "1.9.70", "release_path": release_path},
            "rollback_evidence": {"prior_commit_sha": "e" * 40, "rollback_command": "verified rollback"},
        },
    }
    receipt = runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Current deployment receipt",
            summary="verified",
            scope=scope_ref,
            source="eimemory.deployment_receipt",
            status="deployed",
            content=receipt_payload,
            meta={"report_type": "deployment_receipt", "commit_sha": commit, "version": "1.9.70", "release_path": release_path, "gate_ok": True},
        )
    )
    release = current_release_identity(runtime, scope_ref)
    assert release is not None and release.receipt_id == receipt.record_id
    return release
