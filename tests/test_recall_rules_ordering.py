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
