from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.evidence_first import EvidenceQuery, require_query_first
from eimemory.scheduler.jobs import run_nightly_jobs


def test_status_version_answer_succeeds_when_health_evidence_has_version_and_commit() -> None:
    result = require_query_first(
        "status/version",
        [
            EvidenceQuery(
                source="health",
                query="Read the live health endpoint before answering version status.",
                fact_fields=("version", "commit", "deployed_at"),
            ),
            EvidenceQuery(
                source="deployment",
                query="Check deployment metadata when available.",
                fact_fields=("environment",),
                required=False,
            ),
        ],
        evidence={
            "health": {
                "facts": {
                    "version": "1.4.4",
                    "commit": "abc1234",
                    "deployed_at": "",
                },
                "summary": "Health endpoint returned a successful response.",
            },
            "deployment": {"facts": {"environment": "production"}},
            "impressions": {"facts": {"version": "1.4.3", "commit": "guess"}},
        },
    )

    assert result["ok"] is True
    assert result["blocked_reason"] == ""
    assert result["facts"] == {
        "version": "1.4.4",
        "commit": "abc1234",
        "environment": "production",
    }


def test_deployment_answer_blocks_when_required_health_evidence_missing() -> None:
    result = require_query_first(
        "deployment",
        [
            EvidenceQuery(
                source="health",
                query="Check health before making a deployment claim.",
                fact_fields=("version", "commit"),
            )
        ],
        evidence={
            "deployment": {"facts": {"version": "1.4.4", "commit": "abc1234"}},
            "impressions": {"facts": {"health": "looked healthy"}},
        },
    )

    assert result["ok"] is False
    assert result["blocked_reason"] == "missing_required_evidence:health"
    assert result["facts"] == {}


def test_autonomous_nightly_report_carries_query_first_evidence_without_impressions(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}
    query_first_evidence = {
        "ok": True,
        "subject": "status/version",
        "facts": {"version": "1.4.4", "commit": "abc1234"},
        "blocked_reason": "",
    }

    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_ENABLED", "1")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_DRY_RUN", "1")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_LEARNING_APPLY", "0")
    monkeypatch.setattr(
        runtime,
        "run_memory_eval_ci",
        lambda dataset, *, emit_incidents=False: {
            "ok": True,
            "pass_rate": 1.0,
            "passed_threshold": True,
            "fail_count": 0,
            "name": "stub",
        },
    )
    monkeypatch.setattr(
        runtime,
        "run_autonomous_learning_cycle",
        lambda **_: {
            "ok": True,
            "dry_run": True,
            "apply": False,
            "goal_count": 1,
            "candidate_ids": ["candidate_1"],
            "promotions": [],
            "query_first_evidence": query_first_evidence,
            "impressions": {"version": "1.4.3"},
        },
    )

    report = run_nightly_jobs(runtime, scope=scope)

    assert report["autonomous_learning"]["query_first_evidence"] == query_first_evidence
    assert "impressions" not in report["autonomous_learning"]
