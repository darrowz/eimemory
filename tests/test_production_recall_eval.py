from __future__ import annotations

import json

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.evaluation.production_recall import evaluate_production_recall_quality_gate, run_production_recall_eval
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef


def _scope() -> dict[str, str]:
    return {
        "agent_id": "hongtu",
        "workspace_id": "production",
        "user_id": "darrow",
    }


def _dataset() -> dict:
    return {
        "name": "production-recall-smoke",
        "scope": _scope(),
        "seed": [
            {
                "id": "uumit-preference",
                "kind": "memory",
                "title": "UUMit 项目交付记录",
                "text": "UUMit 项目交付优先级：先保证里程碑可交付，再优化体验，执行计划必须明确。",
                "memory_type": "preference",
            },
            {
                "id": "hongtu-style",
                "kind": "memory",
                "title": "鸿哥沟通风格",
                "text": "鸿哥沟通风格：先给结论，再给证据，不讲废话。",
                "memory_type": "preference",
            },
            {
                "id": "graphiti-knowledge-page",
                "kind": "knowledge_page",
                "title": "Graphiti temporal knowledge graph 论文",
                "text": "Graphiti temporal knowledge graph 论文介绍了时序知识图谱推断与检索方法。",
                "source": "external.research",
                "content": {
                    "page_type": "paper",
                    "summary": "Graphiti temporal knowledge graph",
                },
                "meta": {
                    "page_type": "paper",
                    "source": "research",
                },
            },
            {
                "id": "openclaw-outcome-record",
                "kind": "memory",
                "title": "OpenClaw agent outcome",
                "text": "OpenClaw agent outcome summary for openclaw.agent_end",
                "memory_type": "fact",
                "source": "openclaw.agent_end",
            },
            {
                "id": "rag-match-claim",
                "kind": "claim_card",
                "title": "RAG-Match claim card",
                "text": "RAG-Match 评测用于检查检索增强生成的匹配质量和召回排序稳定性。",
                "source": "eimemory.knowledge.claims",
                "meta": {
                    "claim_type": "evaluation",
                },
            },
            {
                "id": "rule-evolution-reflection-noise",
                "kind": "reflection",
                "title": "Governance report reflection",
                "text": "Governance report reflection is an internal reflection report.",
                "source": "eimemory.rule_evolution_loop",
                "meta": {
                    "report_type": "rule_evolution",
                },
                "provenance": {
                    "report_type": "rule_evolution",
                },
            },
        ],
        "cases": [
            {
                "case_id": "case-uumit",
                "query": "UUMit 项目交付优先级",
                "expected_record_ids": ["uumit-preference"],
                "expected_titles": ["UUMit 项目交付记录"],
                "expected_text": ["里程碑", "交付", "计划"],
                "forbid_kinds": [],
                "forbid_title_contains": [],
                "forbid_source_contains": [],
                "topk": 5,
                "task_context": {"task_type": "project_delivery"},
                "scope": _scope(),
            },
            {
                "case_id": "case-hongtu-style",
                "query": "鸿哥 沟通风格",
                "expected_record_ids": ["hongtu-style"],
                "expected_titles": ["鸿哥沟通风格"],
                "expected_text": ["先给结论", "不讲废话"],
                "forbid_kinds": [],
                "forbid_title_contains": [],
                "forbid_source_contains": [],
                "topk": 5,
                "task_context": {"task_type": "operator_preference"},
                "scope": _scope(),
            },
            {
                "case_id": "case-research-knowledge",
                "query": "Graphiti temporal knowledge graph 论文",
                "expected_record_ids": ["graphiti-knowledge-page"],
                "expected_titles": ["Graphiti temporal knowledge graph 论文"],
                "expected_text": ["时序", "知识图谱"],
                "forbid_kinds": [],
                "forbid_title_contains": [],
                "forbid_source_contains": [],
                "topk": 5,
                "task_context": {
                    "task_type": "knowledge_search",
                    "knowledge_scope": "research",
                },
                "scope": _scope(),
            },
            {
                "case_id": "case-rag-match-claim",
                "query": "RAG-Match 召回排序稳定性",
                "expected_record_ids": ["rag-match-claim"],
                "expected_titles": ["RAG-Match claim card"],
                "expected_text": ["RAG-Match", "召回排序"],
                "forbid_kinds": [],
                "forbid_title_contains": [],
                "forbid_source_contains": ["openclaw.agent_end"],
                "topk": 5,
                "task_context": {
                    "task_type": "knowledge_search",
                    "knowledge_scope": "research",
                },
                "scope": _scope(),
            },
            {
                "case_id": "case-reflection-noise",
                "query": "governance report",
                "expected_record_ids": ["rule-evolution-reflection-noise"],
                "expected_titles": ["Governance report reflection"],
                "expected_text": ["rule_evolution"],
                "forbid_kinds": [],
                "forbid_title_contains": [],
                "forbid_source_contains": [],
                "topk": 5,
                "task_context": {"task_type": "governance report"},
                "scope": _scope(),
            },
        ],
    }


def test_production_recall_eval_reports_regression_metrics(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = run_production_recall_eval(runtime, _dataset())

    assert report["ok"] is True
    assert report["report_type"] == "recall_quality_report"
    assert report["legacy_report_type"] == "production_recall_eval"
    assert report["schema_version"] == 2
    assert report["sample_count"] == 5
    assert report["hit_at_1"] == 1.0
    assert report["hit_at_k"] == 1.0
    assert report["hit_at_5"] == 1.0
    assert report["mrr"] == 1.0
    assert report["quality_gate"]["ok"] is True
    assert report["quality_gate"]["thresholds"]["hit_at_1"] == 0.7
    assert report["quality_gate"]["thresholds"]["latency_ms_p95"] == 1500.0
    assert report["passed_threshold"] is True
    assert report["outcome_pollution_rate"] == 0.0
    assert report["reflection_pollution_rate"] == 0.0
    assert report["empty_rate"] == 0.0
    assert report["cross_channel_leakage_count"] == 0
    assert report["source_filter_leakage_count"] == 0
    assert report["latency_ms_avg"] >= 0.0
    assert report["latency_ms_p95"] >= 0.0
    assert len(report["samples"]) == 5
    assert report["samples"][3]["outcome_polluted"] is False
    assert report["samples"][4]["reflection_returned"] is True
    assert report["samples"][4]["reflection_allowed"] is True
    assert report["samples"][4]["reflection_polluted"] is False
    assert report["samples"][3]["forbid_hit"] is False
    assert report["samples"][4]["forbid_hit"] is False
    assert all(sample["cross_channel_leakage_count"] == 0 for sample in report["samples"])
    assert all(sample["source_filter_leakage_count"] == 0 for sample in report["samples"])


def test_production_recall_eval_blocks_cross_channel_leaks_for_all_authority_channels(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)

    channel_pairs = (("openclaw", "codex"), ("codex", "hermes"), ("hermes", "openclaw"))
    for expected_channel, actual_channel in channel_pairs:
        expected_scope = resolve_channel_scope(expected_channel, _scope())
        leaked = RecordEnvelope.create(
            kind="knowledge_page",
            title=f"{expected_channel} expected answer",
            summary="A correct result returned from the wrong authority channel.",
            scope=ScopeRef.from_dict(resolve_channel_scope(actual_channel, _scope())),
            source="test.production_recall",
        )
        monkeypatch.setattr(
            runtime.memory,
            "recall",
            lambda **_kwargs: RecallBundle(
                items=[leaked],
                rules=[],
                reflections=[],
                confidence=1.0,
                next_action_hint="",
                explanation={},
            ),
        )

        report = run_production_recall_eval(
            runtime,
            {
                "name": f"cross-channel-{expected_channel}",
                "scope": expected_scope,
                "cases": [
                    {
                        "case_id": expected_channel,
                        "query": "expected answer",
                        "expected_titles": [leaked.title],
                        "scope": expected_scope,
                        "task_context": {"runtime_channel": expected_channel},
                    }
                ],
            },
            seed=False,
        )

        assert report["cross_channel_leakage_count"] == 1
        assert report["samples"][0]["cross_channel_leakage_count"] == 1
        assert report["source_filter_leakage_count"] == 0
        assert report["quality_gate"]["ok"] is False
        assert report["quality_gate"]["blocking_metrics"]["cross_channel_leakage_count"]["actual"] == 1


def test_production_recall_eval_uses_scope_channel_fallback_and_explicit_override(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)

    for case_scope_channel, task_context, returned_channel in (
        ("hermes", {}, "hermes"),
        ("openclaw", {"runtime_channel": "codex"}, "codex"),
    ):
        returned = RecordEnvelope.create(
            kind="knowledge_page",
            title="Authority answer",
            scope=ScopeRef.from_dict(resolve_channel_scope(returned_channel, _scope())),
            source="test.production_recall",
        )
        monkeypatch.setattr(
            runtime.memory,
            "recall",
            lambda **_kwargs: RecallBundle(
                items=[returned], rules=[], reflections=[], confidence=1.0,
                next_action_hint="", explanation={},
            ),
        )
        case_scope = resolve_channel_scope(case_scope_channel, _scope())

        report = run_production_recall_eval(
            runtime,
            {
                "scope": case_scope,
                "cases": [{
                    "query": "authority answer",
                    "expected_titles": [returned.title],
                    "scope": case_scope,
                    "task_context": task_context,
                }],
            },
            seed=False,
        )

        assert report["cross_channel_leakage_count"] == 0
        assert report["quality_gate"]["ok"] is True


def test_production_recall_eval_blocks_source_filter_leaks_and_treats_no_filter_as_unconstrained(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    returned = RecordEnvelope.create(
        kind="knowledge_page",
        title="Filtered source answer",
        summary="A result from source beta.",
        scope=ScopeRef.from_dict(_scope()),
        source="test.production_recall",
        source_id="beta",
    )
    monkeypatch.setattr(
        runtime.memory,
        "recall",
        lambda **_kwargs: RecallBundle(
            items=[returned],
            rules=[],
            reflections=[],
            confidence=1.0,
            next_action_hint="",
            explanation={},
        ),
    )

    constrained = run_production_recall_eval(
        runtime,
        {
            "name": "source-filter-leak",
            "scope": _scope(),
            "cases": [
                {
                    "query": "filtered source answer",
                    "expected_titles": [returned.title],
                    "scope": _scope(),
                    "task_context": {"source_ids": ["alpha"]},
                }
            ],
        },
        seed=False,
    )
    target_only = run_production_recall_eval(
        runtime,
        {
            "name": "target-source-is-not-a-filter",
            "scope": _scope(),
            "cases": [
                {
                    "query": "filtered source answer",
                    "expected_titles": [returned.title],
                    "scope": _scope(),
                    "task_context": {"target_source_id": "alpha"},
                }
            ],
        },
        seed=False,
    )
    unconstrained = run_production_recall_eval(
        runtime,
        {
            "name": "source-filter-unconstrained",
            "scope": _scope(),
            "cases": [
                {
                    "query": "filtered source answer",
                    "expected_titles": [returned.title],
                    "scope": _scope(),
                    "task_context": {},
                }
            ],
        },
        seed=False,
    )

    assert constrained["source_filter_leakage_count"] == 1
    assert constrained["samples"][0]["source_filter_leakage_count"] == 1
    assert constrained["quality_gate"]["ok"] is False
    assert constrained["quality_gate"]["blocking_metrics"]["source_filter_leakage_count"]["actual"] == 1
    assert target_only["source_filter_leakage_count"] == 0
    assert target_only["quality_gate"]["ok"] is True
    assert unconstrained["source_filter_leakage_count"] == 0
    assert unconstrained["samples"][0]["source_filter_leakage_count"] == 0
    assert unconstrained["quality_gate"]["ok"] is True


def test_production_recall_eval_treats_empty_source_allowlist_as_deny_all(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    returned = RecordEnvelope.create(
        kind="knowledge_page",
        title="Defective engine result",
        scope=ScopeRef.from_dict(_scope()),
        source="test.production_recall",
        source_id="alpha",
    )
    monkeypatch.setattr(
        runtime.memory,
        "recall",
        lambda **_kwargs: RecallBundle(
            items=[returned], rules=[], reflections=[], confidence=1.0,
            next_action_hint="", explanation={},
        ),
    )

    report = run_production_recall_eval(
        runtime,
        {
            "scope": _scope(),
            "cases": [{
                "query": "deny all sources",
                "expected_titles": [returned.title],
                "scope": _scope(),
                "task_context": {"source_ids": []},
            }],
        },
        seed=False,
    )

    assert report["source_filter_leakage_count"] == 1
    assert report["samples"][0]["source_filter_leakage_count"] == 1
    assert report["quality_gate"]["ok"] is False


def test_production_recall_eval_uses_candidate_source_id_normalization(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    returned = RecordEnvelope.create(
        kind="knowledge_page",
        title="Normalized source answer",
        scope=ScopeRef.from_dict(_scope()),
        source="test.production_recall",
        source_id="alpha",
    )
    monkeypatch.setattr(
        runtime.memory,
        "recall",
        lambda **_kwargs: RecallBundle(
            items=[returned], rules=[], reflections=[], confidence=1.0,
            next_action_hint="", explanation={},
        ),
    )

    for task_context in (
        {"source_ids": ["ALPHA"]},
        {"source_ids": ["ＡＬＰＨＡ"]},
        {"target_source_id": " BETA "},
    ):
        report = run_production_recall_eval(
            runtime,
            {
                "scope": _scope(),
                "cases": [{
                    "query": "normalized source answer",
                    "expected_titles": [returned.title],
                    "scope": _scope(),
                    "task_context": task_context,
                }],
            },
            seed=False,
        )

        assert report["source_filter_leakage_count"] == 0
        assert report["quality_gate"]["ok"] is True


def test_production_recall_eval_rejects_invalid_source_filter_contracts(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    returned = RecordEnvelope.create(
        kind="knowledge_page",
        title="Source answer",
        scope=ScopeRef.from_dict(_scope()),
        source="test.production_recall",
        source_id="alpha",
    )
    monkeypatch.setattr(
        runtime.memory,
        "recall",
        lambda **_kwargs: RecallBundle(
            items=[returned], rules=[], reflections=[], confidence=1.0,
            next_action_hint="", explanation={},
        ),
    )

    invalid_contexts = (
        {"source_ids": "alpha"},
        {"source_ids": {"alpha"}},
        {"source_ids": [" alpha "]},
        {"source_ids": ["ALPHA", "ＡＬＰＨＡ"]},
        {"target_source_id": "bad source"},
        {"source_ids": ["alpha"], "target_source_id": "bad source"},
    )
    for task_context in invalid_contexts:
        with pytest.raises(ValueError):
            run_production_recall_eval(
                runtime,
                {
                    "scope": _scope(),
                    "cases": [{
                        "query": "source answer",
                        "expected_titles": [returned.title],
                        "scope": _scope(),
                        "task_context": task_context,
                    }],
                },
                seed=False,
            )


def test_production_recall_eval_seeds_temporary_runtime(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    dataset = _dataset()

    report = run_production_recall_eval(runtime, dataset)

    assert report["seeded"] is True
    assert len(runtime.store.list_records(scope=_scope(), limit=20)) == 0


def test_cli_eval_production_recall_writes_report_file(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    dataset_path = tmp_path / "production_recall_smoke.json"
    output_path = tmp_path / "production-recall-report.json"
    dataset_path.write_text(json.dumps(_dataset(), ensure_ascii=False), encoding="utf-8")

    assert cli_main(["eval", "production-recall", str(dataset_path), "--output", str(output_path)]) == 0

    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert printed["output"] == str(output_path)
    assert written["report_type"] == "recall_quality_report"
    assert written["legacy_report_type"] == "production_recall_eval"
    assert written["sample_count"] == 5
    assert written["outcome_pollution_rate"] == 0.0


def test_production_recall_quality_gate_blocks_pollution_and_latency() -> None:
    gate = evaluate_production_recall_quality_gate(
        {
            "sample_count": 20,
            "hit_at_1": 0.7,
            "hit_at_5": 0.9,
            "false_recall_rate": 0.0,
            "forbidden_hit_rate": 0.0,
            "audit_pollution_rate": 0.06,
            "incident_pollution_rate": 0.0,
            "evolution_pollution_rate": 0.0,
            "stale_rule_pollution_rate": 0.0,
            "selected_record_pollution_rate": 0.0,
            "latency_ms_p95": 1500.1,
        }
    )

    assert gate["ok"] is False
    assert gate["blocked_reason"] == "recall_quality_gate_failed"
    assert gate["blocking_metrics"]["audit_pollution_rate"]["actual"] == 0.06
    assert gate["blocking_metrics"]["latency_ms_p95"]["actual"] == 1500.1
