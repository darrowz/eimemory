from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.evaluation.production_recall import run_production_recall_eval


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
    assert report["report_type"] == "production_recall_eval"
    assert report["schema_version"] == 1
    assert report["sample_count"] == 5
    assert report["hit_at_1"] == 1.0
    assert report["hit_at_k"] == 1.0
    assert report["mrr"] == 1.0
    assert report["outcome_pollution_rate"] == 0.0
    assert report["reflection_pollution_rate"] == 0.2
    assert report["empty_rate"] == 0.0
    assert report["latency_ms_avg"] >= 0.0
    assert report["latency_ms_p95"] >= 0.0
    assert len(report["samples"]) == 5
    assert report["samples"][3]["outcome_polluted"] is False
    assert report["samples"][4]["reflection_polluted"] is True
    assert report["samples"][3]["forbid_hit"] is False
    assert report["samples"][4]["forbid_hit"] is False


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
    assert written["report_type"] == "production_recall_eval"
    assert written["sample_count"] == 5
    assert written["outcome_pollution_rate"] == 0.0
