"""Gate top5 relevance: bundle.rules must be query-ordered before gate merge."""
import tempfile
from pathlib import Path

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef


def _make_rule(rt, scope, title, summary, source="probe"):
    rule = RecordEnvelope.create(
        kind="rule", title=title, summary=summary,
        scope=scope, source=source, status="active",
    )
    rt.store.append(rule)
    return rule


def test_bundle_rules_head_is_query_relevant(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_ref = ScopeRef(agent_id="hongtu", workspace_id="embodied")
    scope = {"agent_id": "hongtu", "workspace_id": "embodied"}

    # Older ground-truth rule matching the query; newer boilerplate that does not.
    gt = _make_rule(
        runtime, scope_ref,
        "Ground truth behavior: proactive.judgment",
        "When a capability is missing, create a concrete plan, replay, gated "
        "implementation path, and rollback boundary",
    )
    for i in range(3):
        _make_rule(
            runtime, scope_ref,
            f"Capability candidate: memory.recall recall rule {i}",
            f"Build a memory.recall policy/SOP/eval case for boilerplate topic {i}",
        )

    query = (
        "capability gap implementation path replay gate rollback concrete plan "
        "proactive judgment"
    )
    bundle = runtime.memory.recall(
        query=query,
        scope=scope,
        task_context={"source_ids": ["default"], "target_source_id": "default",
                      "recall_profile": "precision", "candidate_limit": 24},
        limit=5,
    )

    assert bundle.rules
    # The query-matching rule must outrank newer zero-overlap rules.
    head_titles = [(r.title or "") for r in bundle.rules[:2]]
    assert any("Ground truth" in t for t in head_titles), head_titles
    # And a rules-first consumer (the gate) would surface it at slot 1.
    merged = [*(bundle.rules or []), *(bundle.items or [])]
    seen: set[str] = set()
    returned = []
    for item in merged:
        rid = str(item.record_id or "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        returned.append(item)
        if len(returned) >= 5:
            break
    assert returned[0].record_id == gt.record_id


def test_dedupe_records_by_ranking_identity_collapses_ground_truth_clones() -> None:
    from eimemory.evaluation.real_query_gate import (
        _dedupe_records_by_ranking_identity,
        _record_ranking_ref,
    )

    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied")
    clones = []
    for _ in range(5):
        clones.append(
            RecordEnvelope.create(
                kind="rule",
                title="Ground truth behavior: proactive.judgment",
                summary="When a capability is missing, create a concrete plan.",
                scope=scope,
                source="probe",
                status="active",
                content={
                    "report_type": "ground_truth_behavior_rule",
                    "priority": "T0",
                    "must_use": True,
                    "target_capability": "proactive.judgment",
                    "expected_behavior": "plan then replay",
                },
                meta={
                    "report_type": "ground_truth_behavior_rule",
                    "priority": "T0",
                    "must_use": True,
                    "target_capability": "proactive.judgment",
                },
            )
        )
    memory = RecordEnvelope.create(
        kind="memory",
        title="ToolBench-X hazard recovery",
        summary="Five hazard classes for unreliable tools",
        scope=scope,
        source="probe",
        status="active",
    )
    unique = _dedupe_records_by_ranking_identity([*clones, memory])
    assert len(unique) == 2
    assert unique[0].kind == "rule"
    assert unique[1].record_id == memory.record_id
    assert _record_ranking_ref(unique[0]).startswith("gtr_")
    assert len({_record_ranking_ref(item) for item in unique}) == 2


def test_candidate_records_keep_at_most_one_rule_ahead_of_items() -> None:
    from eimemory.evaluation.real_query_gate import _candidate_records_for_case

    class _Bundle:
        def __init__(self) -> None:
            scope = ScopeRef(agent_id="hongtu", workspace_id="embodied")
            self.rules = [
                RecordEnvelope.create(kind="rule", title=f"r{i}", summary="s", scope=scope, source="p", status="active")
                for i in range(5)
            ]
            self.items = [
                RecordEnvelope.create(kind="memory", title=f"m{i}", summary="s", scope=scope, source="p", status="active")
                for i in range(4)
            ]

    merged = _candidate_records_for_case(_Bundle(), label_kinds={"rule"})
    assert merged[0].kind == "rule"
    assert [item.kind for item in merged[1:]] == ["memory"] * 4
    assert len(merged) == 5
