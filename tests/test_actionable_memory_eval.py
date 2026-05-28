from __future__ import annotations

import json
from pathlib import Path

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.evaluation.actionable_memory import run_actionable_memory_eval
from eimemory.governance.snapshot import build_governance_snapshot


def _scope() -> dict:
    return {
        "agent_id": "hongtu",
        "workspace_id": "actionable",
        "user_id": "darrow",
    }


def _dataset() -> dict:
    return {
        "name": "actionable-memory-smoke",
        "scope": _scope(),
        "seed": [
            {
                "id": "uumit-posture-profile",
                "kind": "memory",
                "title": "UUMit project delivery profile",
                "text": "UUMit项目交付优先级：先保证里程碑可交付，再优化体验；执行证据必须充分且严格验收。",
                "memory_type": "preference",
                "meta": {
                    "constraints": {
                        "evidence_required": True,
                        "strict_acceptance": True,
                    }
                },
            },
            {
                "id": "uumit-project-memory",
                "kind": "memory",
                "title": "UUMit 项目交付记录",
                "text": "UUMit项目交付应优先处理关键里程碑并记录可执行交付计划。",
                "memory_type": "preference",
            },
            {
                "id": "hongtu-style",
                "kind": "memory",
                "title": "鸿哥沟通风格",
                "text": "鸿哥沟通风格：不讲废话，先给结论，再给证据，语气简洁。",
                "memory_type": "preference",
            },
            {
                "id": "siren-paper",
                "kind": "knowledge_page",
                "title": "SIREN 论文检索页",
                "text": "SIREN is a contaminated project paper page not related to UUMit.",
                "source": "external.research",
                "content": {
                    "page_type": "paper",
                    "source_ids": ["siren-1"],
                },
                "meta": {
                    "page_type": "paper",
                    "source_ids": ["siren-1"],
                },
            },
            {
                "id": "prism-paper",
                "kind": "knowledge_page",
                "title": "PRISM 论文检索页",
                "text": "PRISM is a contaminated project paper page not related to UUMit.",
                "source": "external.research",
                "content": {
                    "page_type": "paper",
                    "source_ids": ["prism-1"],
                },
                "meta": {
                    "page_type": "paper",
                    "source_ids": ["prism-1"],
                },
            },
            {
                "id": "graphiti-paper-page",
                "kind": "knowledge_page",
                "title": "Graphiti temporal knowledge graph 论文",
                "text": "Graphiti temporal knowledge graph 论文总结了时序关系推断与知识检索的新方法。",
                "source": "external.research",
                "content": {
                    "page_type": "paper",
                    "source_ids": ["graphiti-1"],
                },
                "meta": {
                    "page_type": "paper",
                    "source_ids": ["graphiti-1"],
                },
            },
        ],
        "cases": [
            {
                "id": "uumit-project",
                "case_type": "recall",
                "query_type": "project",
                "query": "UUMit 项目交付里程碑重点是什么？",
                "seed_id": "uumit-project-memory",
                "limit": 5,
                "expect_any_title": ["UUMit 项目交付记录"],
                "expect_any_kind": ["memory"],
                "forbid_any_title": ["SIREN", "PRISM"],
                "forbid_any_kind": ["knowledge_page"],
            },
            {
                "id": "uumit-posture",
                "case_type": "posture",
                "query": "UUMit 交付姿态",
                "seed_id": "uumit-posture-profile",
                "limit": 5,
                "expect_profile_non_empty": True,
                "expected_constraints": ["evidence_required", "strict_acceptance"],
            },
            {
                "id": "hongtu-style",
                "case_type": "recall",
                "query": "鸿哥 沟通风格",
                "seed_id": "hongtu-style",
                "limit": 5,
                "expect_any_title": ["鸿哥沟通风格"],
                "expect_any_text": ["no fluff", "简洁", "先给结论"],
            },
            {
                "id": "graphiti-query",
                "case_type": "recall",
                "query_type": "research",
                "query": "Graphiti temporal knowledge graph 论文",
                "limit": 5,
                "expect_any_title": ["Graphiti temporal knowledge graph 论文"],
                "expect_any_kind": ["knowledge_page"],
            },
        ],
    }


def test_actionable_memory_eval_reports_required_fields(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = run_actionable_memory_eval(runtime, _dataset())

    assert report["ok"] is True
    assert report["report_type"] == "actionable_memory_eval"
    assert report["name"] == "actionable-memory-smoke"
    assert report["sample_count"] == 4
    assert report["pass_count"] == 4
    assert report["pass_rate"] == 1.0
    assert report["recall_topk_pass_rate"] == 1.0
    assert report["posture_pass_rate"] == 1.0
    assert report["contamination_rate"] == 0.0
    assert report["project_query_contamination_rate"] == 0.0
    assert len(report["samples"]) == 4
    project_sample = next(sample for sample in report["samples"] if sample["case_id"] == "uumit-project")
    posture_sample = next(sample for sample in report["samples"] if sample["case_id"] == "uumit-posture")
    assert project_sample["case_type"] == "recall"
    assert project_sample["passed"] is True
    assert posture_sample["case_type"] == "posture"
    assert posture_sample["posture_profile_non_empty"] is True
    assert posture_sample["constraints_present"] is True
    assert "evidence_required" in posture_sample["constraints"]
    assert "strict_acceptance" in posture_sample["constraints"]


def test_actionable_memory_eval_seeds_temporary_runtime_only(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = run_actionable_memory_eval(runtime, _dataset(), persist_report=True)

    assert report["seed_count"] == 6
    stored = runtime.store.list_records(kinds=["memory", "knowledge_page", "reflection"], scope=_scope(), limit=20)
    assert [record.kind for record in stored] == ["reflection"]
    assert stored[0].record_id == report["persisted_record_id"]
    assert stored[0].meta["report_type"] == "actionable_memory_eval"


def test_runtime_run_actionable_memory_eval_wrapper(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    assert runtime.run_actionable_memory_eval(_dataset())["pass_rate"] == 1.0


def test_cli_eval_actionable_memory_writes_report(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "runtime"
    monkeypatch.setenv("EIMEMORY_ROOT", str(root))
    dataset_path = tmp_path / "actionable_memory_smoke.json"
    output_path = tmp_path / "actionable-report.json"
    dataset_path.write_text(json.dumps(_dataset(), ensure_ascii=False), encoding="utf-8")

    assert cli_main(["eval", "actionable", str(dataset_path), "--output", str(output_path)]) == 0

    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert printed["output"] == str(output_path)
    assert written["report_type"] == "actionable_memory_eval"
    assert written["sample_count"] == 4
    assert written["pass_rate"] == 1.0


def test_governance_snapshot_surfaces_actionable_memory_metrics(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = _scope()
    scope_ref = scope

    report = run_actionable_memory_eval(runtime, _dataset(), persist_report=True)
    snapshot = build_governance_snapshot(runtime, scope_ref)

    assert snapshot["actionable_memory"]["count"] == 1
    assert snapshot["actionable_memory"]["latest"]["record_id"] == report["persisted_record_id"]
    assert snapshot["actionable_memory"]["posture_profile_count"] == 1
    assert snapshot["actionable_memory"]["posture_coverage"] == 1.0
    assert snapshot["actionable_memory"]["project_query_contamination_rate"] == 0.0
    assert snapshot["actionable_memory"]["latest"]["pass_rate"] == 1.0
    assert snapshot["actionable_memory"]["latest"]["posture_pass_rate"] == 1.0

def test_actionable_memory_snapshot_is_safe_without_reports(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = _scope()

    snapshot = build_governance_snapshot(runtime, scope)

    assert snapshot["actionable_memory"]["count"] == 0
    assert snapshot["actionable_memory"]["posture_profile_count"] == 0
    assert snapshot["actionable_memory"]["posture_coverage"] == 0.0
    assert snapshot["actionable_memory"]["project_query_contamination_rate"] == 0.0
