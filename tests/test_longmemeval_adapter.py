from __future__ import annotations

import json
from types import SimpleNamespace

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.evaluation.longmemeval import normalize_longmemeval_dataset, run_longmemeval
from eimemory.governance.snapshot import build_governance_snapshot


def _smoke_dataset(scope: dict | None = None) -> dict:
    return {
        "name": "longmem-smoke",
        "scope": scope or {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        "cases": [
            {
                "id": "postgres-pref",
                "question": "Which database does the user prefer for backups?",
                "question_type": "preference",
                "answer": "PostgreSQL",
                "haystack_sessions": [
                    {
                        "session_id": "sess-db",
                        "turns": [
                            {
                                "turn_id": "turn-db-1",
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": "I prefer PostgreSQL because backups are easier.",
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "session_id": "sess-ui",
                        "messages": [
                            {"role": "user", "content": "The dashboard should keep a compact activity feed."}
                        ],
                    },
                ],
                "evidence_session_ids": ["sess-db"],
                "evidence_turn_ids": ["turn-db-1"],
            }
        ],
    }


def test_longmemeval_normalization_accepts_smoke_dataset_shapes() -> None:
    normalized = normalize_longmemeval_dataset(_smoke_dataset())

    assert normalized["name"] == "longmem-smoke"
    assert normalized["scope"]["agent_id"] == "hongtu"
    assert normalized["cases"][0]["case_id"] == "postgres-pref"
    assert normalized["cases"][0]["question_type"] == "preference"
    assert normalized["cases"][0]["expected_answer"] == "PostgreSQL"
    assert normalized["cases"][0]["evidence_session_ids"] == ["sess-db"]
    assert [chunk["session_id"] for chunk in normalized["cases"][0]["chunks"]] == ["sess-db", "sess-ui"]
    assert normalized["cases"][0]["chunks"][0]["turn_id"] == "turn-db-1"
    assert "PostgreSQL" in normalized["cases"][0]["chunks"][0]["text"]


def test_longmemeval_raw_mode_retrieval_recall_hits_expected_session(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = run_longmemeval(runtime, _smoke_dataset(), mode="raw", granularity="session", limit=5)

    assert report["ok"] is True
    assert report["report_type"] == "longmemeval_eval"
    assert report["sample_count"] == 1
    assert report["retrieval_recall_at_1"] == 1.0
    assert report["retrieval_recall_at_5"] == 1.0
    assert report["recall_any_at_5"] == 1.0
    assert report["recall_all_at_5"] == 1.0
    assert report["samples"][0]["hit_session_ids"][0] == "sess-db"


def test_longmemeval_uses_case_scope_for_ingestion_and_retrieval(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    dataset_scope = {"agent_id": "dataset-agent", "workspace_id": "dataset-workspace"}
    case_scope = {"agent_id": "case-agent", "workspace_id": "case-workspace", "user_id": "case-user"}
    dataset = _smoke_dataset(dataset_scope)
    dataset["cases"][0]["scope"] = case_scope

    report = run_longmemeval(runtime, dataset, mode="raw", granularity="session", limit=5)

    dataset_scope_chunks = runtime.store.list_records(kinds=["raw_chunk"], scope=dataset_scope, limit=10)
    case_scope_chunks = runtime.store.list_records(kinds=["raw_chunk"], scope=case_scope, limit=10)
    assert report["retrieval_recall_at_5"] == 1.0
    assert not dataset_scope_chunks
    assert case_scope_chunks


def test_longmemeval_turn_granularity_scores_evidence_from_non_first_turn(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    dataset = {
        "name": "longmem-turns",
        "scope": {"agent_id": "hongtu", "workspace_id": "embodied"},
        "cases": [
            {
                "id": "second-turn",
                "question": "Which cache does the user prefer?",
                "question_type": "preference",
                "haystack_sessions": [
                    {
                        "session_id": "sess-cache",
                        "turns": [
                            {
                                "turn_id": "turn-cache-1",
                                "messages": [{"role": "user", "content": "The dashboard needs compact cards."}],
                            },
                            {
                                "turn_id": "turn-cache-2",
                                "messages": [{"role": "user", "content": "I prefer Redis for cache recovery."}],
                            },
                        ],
                    }
                ],
                "evidence_session_ids": ["sess-cache"],
                "evidence_turn_ids": ["turn-cache-2"],
            }
        ],
    }

    report = run_longmemeval(runtime, dataset, mode="raw", granularity="turn", limit=5)

    assert report["retrieval_recall_at_5"] == 1.0
    assert "turn-cache-2" in report["samples"][0]["returned_ids"]
    assert report["samples"][0]["hit_turn_ids"] == ["turn-cache-2"]


def test_longmemeval_supports_raw_haystack_session_lists_with_has_answer_turns(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    dataset = {
        "name": "raw-longmemeval-case",
        "scope": {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        "cases": [
            {
                "question_id": "raw-has-ans-1",
                "question": "Where did I move?",
                "question_type": "temporal",
                "answer": "Austin",
                "answer_session_ids": ["session-a"],
                "haystack_session_ids": ["session-a", "session-b"],
                "haystack_sessions": [
                    [
                        {"role": "user", "content": "I moved to Austin in March.", "has_answer": True},
                        {"role": "assistant", "content": "Congrats."},
                    ],
                    [
                        {"role": "user", "content": "The office needs more plants."},
                    ],
                ],
            }
        ],
    }
    normalized = normalize_longmemeval_dataset(dataset)["cases"][0]

    assert normalized["case_id"] == "raw-has-ans-1"
    assert normalized["evidence_session_ids"] == ["session-a"]
    assert normalized["evidence_turn_ids"] == ["session-a:m0"]
    assert normalized["chunks"][0]["session_id"] == "session-a"
    assert "session-a:m0" in normalized["chunks"][0]["turn_ids"]

    report = run_longmemeval(runtime, dataset, mode="raw", granularity="turn", limit=5)

    assert report["sample_count"] == 1
    assert report["samples"][0]["case_id"] == "raw-has-ans-1"
    assert report["samples"][0]["expected_ids"] == ["session-a:m0"]
    assert report["samples"][0]["hit_turn_ids"] == ["session-a:m0"]


def test_longmemeval_hybrid_mode_uses_raw_hybrid_retrieval_end_to_end(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = run_longmemeval(runtime, _smoke_dataset(), mode="hybrid", granularity="session", limit=5)

    assert report["ok"] is True
    assert report["mode"] == "hybrid"
    assert report["retrieval_recall_at_5"] == 1.0


def test_longmemeval_hybrid_mode_consumes_raw_evidence_from_memory_recall(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    dataset = {
        "name": "longmem-hybrid-evidence",
        "scope": {"agent_id": "hongtu", "workspace_id": "embodied"},
        "cases": [
            {
                "id": "forced-raw-evidence",
                "question": "zzzzzz impossible lexical query",
                "question_type": "preference",
                "haystack_sessions": [
                    {
                        "session_id": "sess-forced",
                        "messages": [{"role": "user", "content": "Buried evidence without matching query terms."}],
                    }
                ],
                "evidence_session_ids": ["sess-forced"],
            }
        ],
    }
    calls: list[dict] = []

    def fake_recall(**kwargs):
        calls.append(kwargs)
        records = runtime.store.list_records(kinds=["raw_chunk"], scope=kwargs["scope"], limit=10)
        target = next(record for record in records if record.content.get("session_id") == "sess-forced")
        return SimpleNamespace(explanation={"raw_evidence": [{"record": {"record_id": target.record_id}}]})

    runtime.memory.recall = fake_recall

    report = run_longmemeval(runtime, dataset, mode="hybrid", granularity="session", limit=5)

    assert calls
    assert calls[0]["task_context"]["recall_mode"] == "raw_hybrid"
    assert report["retrieval_recall_at_1"] == 1.0


def test_runtime_run_longmemeval_delegates_to_adapter(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = runtime.run_longmemeval(_smoke_dataset(), mode="raw", granularity="session", limit=5)

    assert report["ok"] is True
    assert report["report_type"] == "longmemeval_eval"
    assert report["retrieval_recall_at_5"] == 1.0


def test_longmemeval_report_uses_retrieval_metrics_not_qa_accuracy(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = run_longmemeval(runtime, _smoke_dataset(), mode="raw", granularity="session", limit=10)

    for key in (
        "retrieval_recall_at_1",
        "retrieval_recall_at_5",
        "retrieval_recall_at_10",
        "recall_any_at_5",
        "recall_all_at_5",
        "ndcg_at_5",
        "mrr",
        "latency_ms_avg",
        "latency_ms_p95",
        "by_question_type",
        "samples",
    ):
        assert key in report
    assert "qa_accuracy" not in report
    assert "accuracy" not in report
    assert report["by_question_type"]["preference"]["retrieval_recall_at_5"] == 1.0


def test_cli_eval_longmem_writes_report_file(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "runtime"
    monkeypatch.setenv("EIMEMORY_ROOT", str(root))
    dataset_path = tmp_path / "longmem.json"
    output_path = tmp_path / "longmem-report.json"
    dataset_path.write_text(json.dumps(_smoke_dataset(), ensure_ascii=False), encoding="utf-8")

    assert cli_main(["eval", "longmem", str(dataset_path), "--mode", "raw", "--output", str(output_path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert printed["output"] == str(output_path)
    assert written["report_type"] == "longmemeval_eval"
    assert written["retrieval_recall_at_5"] == 1.0


def test_longmemeval_persist_report_writes_reflection_and_governance_reads_it(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    report = run_longmemeval(runtime, _smoke_dataset(scope), mode="raw", persist_report=True)
    records = runtime.store.list_records(kinds=["reflection"], scope=scope, limit=10)
    snapshot = build_governance_snapshot(runtime, scope)

    assert report["persisted"] is True
    assert report["persisted_record_id"]
    assert records[0].source == "eimemory.longmemeval"
    assert records[0].meta["report_type"] == "longmemeval_eval"
    assert snapshot["longmemeval"]["count"] == 1
    assert snapshot["longmemeval"]["latest"]["record_id"] == report["persisted_record_id"]
    assert snapshot["longmemeval"]["latest"]["retrieval_recall_at_5"] == 1.0
