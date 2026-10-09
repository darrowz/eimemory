"""Readiness consumes the authoritative v3 L5 assessment, never the legacy one.

Prod 1.14.49 closure: rehearsal blocked at ``assessment_complete`` because the
newest ``l5_assessment`` is written by the legacy structural loop, which is
non-authoritative by design (complete=False, capped at L4.5).  The gate now
reads the v3 product-completion result and reports its gaps.
"""
from __future__ import annotations

from types import SimpleNamespace

from eimemory.api.runtime import Runtime
from eimemory.governance.l5.l5_readiness import _authoritative_l5_assessment
from eimemory.models.records import ScopeRef

SCOPE = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")


def _runtime(report):
    calls = []

    def build(**kwargs):
        calls.append(kwargs)
        return report

    return SimpleNamespace(build_l5_readiness_report=build), calls


def test_incomplete_v3_blocks_and_names_missing_evidence() -> None:
    runtime, calls = _runtime({
        "reader_mode": "v3", "ok": False, "status": "degraded", "product_l5_complete": False,
        "completion_status": "incomplete", "control_plane_status": "degraded", "loop_maturity": "diagnosing",
        "gaps": ["raw_control_plane_not_ready", "no_qualifying_terminal_receipt"],
        "code_evolution": {"gaps": ["provider_not_ready"]},
        "assessment": {"assessment_id": "l5-assessment-x",
                       "gaps": [{"reason": "insufficient_observations", "capability_id": "memory.recall"}]},
    })
    summary = _authoritative_l5_assessment(runtime, scope=SCOPE, repo_root="/repo")
    assert calls == [{"scope": {"tenant_id": "default", "agent_id": "hongtu", "workspace_id": "embodied",
                                "user_id": "darrow"}, "persist": False, "repo_root": "/repo",
                      "reader_mode": "v3", "capability_scope": "global"}]
    assert summary["available"] is True and summary["complete"] is False
    assert summary["missing_evidence"] == [
        "raw_control_plane_not_ready", "no_qualifying_terminal_receipt", "provider_not_ready",
        "insufficient_observations:memory.recall",
    ]
    assert summary["loop_maturity"] == "diagnosing"


def test_complete_requires_v3_product_completion() -> None:
    runtime, _ = _runtime({"reader_mode": "v3", "ok": True, "status": "ready", "product_l5_complete": True,
                           "gaps": [], "assessment": {"gaps": []}})
    assert _authoritative_l5_assessment(runtime, scope=SCOPE, repo_root=None)["complete"] is True
    runtime, _ = _runtime({"reader_mode": "v3", "ok": False, "status": "degraded", "product_l5_complete": True,
                           "gaps": [], "assessment": {"gaps": []}})
    degraded = _authoritative_l5_assessment(runtime, scope=SCOPE, repo_root=None)
    assert degraded["complete"] is False and degraded["missing_evidence"] == ["control_plane:degraded"]


def test_non_v3_or_missing_reader_is_never_complete() -> None:
    runtime, _ = _runtime({"reader_mode": "legacy", "ok": True, "product_l5_complete": True})
    assert _authoritative_l5_assessment(runtime, scope=SCOPE, repo_root=None)["available"] is False
    unavailable = _authoritative_l5_assessment(SimpleNamespace(), scope=SCOPE, repo_root=None)
    assert unavailable == {"available": False, "complete": False, "reason": "v3_reader_unavailable",
                           "missing_evidence": ["authoritative_l5_assessment:unavailable"]}

    def boom(**kwargs):
        raise RuntimeError("x")

    errored = _authoritative_l5_assessment(SimpleNamespace(build_l5_readiness_report=boom), scope=SCOPE, repo_root=None)
    assert errored["complete"] is False and errored["reason"] == "v3_reader_error:RuntimeError"


def test_real_runtime_without_evidence_is_computed_incomplete(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        summary = _authoritative_l5_assessment(runtime, scope=SCOPE, repo_root=str(tmp_path))
    finally:
        runtime.close()
    assert summary["available"] is True
    assert summary["complete"] is False
    assert summary["missing_evidence"]
