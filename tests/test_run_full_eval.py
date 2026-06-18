from __future__ import annotations

from scripts import run_full_eval


def test_run_full_eval_env_config_defaults() -> None:
    env = run_full_eval.load_eval_config({})
    assert env["n_workers"] == 32
    assert env["lme_granularity"] == "turn"
    assert env["locomo_granularity"] == "turn"
    assert env["lme_limit"] is None
    assert env["locomo_limit"] is None
    assert env["reranker"] == "auto"


def test_run_full_eval_env_config_override() -> None:
    env = run_full_eval.load_eval_config(
        {
            "EIMEMORY_WORKERS": "16",
            "EIMEMORY_LME_GRANULARITY": "session",
            "EIMEMORY_LME_LIMIT": "7",
            "EIMEMORY_LOCOMO_LIMIT": "4",
            "EIMEMORY_RERANKER": "deterministic",
        }
    )
    assert env["n_workers"] == 16
    assert env["lme_granularity"] == "session"
    assert env["lme_limit"] == 7
    assert env["locomo_limit"] == 4
    assert env["reranker"] == "deterministic"


def test_aggregate_report_robust_to_zero_ranks_and_missing_latency() -> None:
    results = [
        {
            "ok": True,
            "chunk_id": 0,
            "n": 2,
            "report": {
                "report_type": "longmemeval_eval",
                "samples": [
                    {
                        "rank": 0,
                        "retrieval_recall_at_1": 0.0,
                        "retrieval_recall_at_5": 0.0,
                        "retrieval_recall_at_10": 0.0,
                        "recall_any_at_5": 0.0,
                        "recall_all_at_5": 0.0,
                        "ndcg_at_5": 0.0,
                    },
                    {
                        "rank": 2,
                        "latency_ms": 10.0,
                        "retrieval_recall_at_1": 0.0,
                        "retrieval_recall_at_5": 0.0,
                        "retrieval_recall_at_10": 0.0,
                        "recall_any_at_5": 0.0,
                        "recall_all_at_5": 0.0,
                        "ndcg_at_5": 0.0,
                    },
                ],
            },
            "elapsed": 0.0,
        },
    ]
    lme_agg = run_full_eval.aggregate_lme_reports(results)
    assert lme_agg["ok"] is True
    assert lme_agg["sample_count"] == 2
    assert lme_agg["mrr"] == 0.25
    assert lme_agg["latency_ms_avg"] == 10.0

    loc_results = [
        {
            "ok": True,
            "chunk_id": 1,
            "n": 2,
            "report": {
                "samples": [
                    {"rank": 0, "recall_at_1": 0.0, "recall_at_5": 0.0, "recall_at_10": 0.0, "recall_any_at_5": 0.0, "ndcg_at_5": 0.0},
                    {"rank": 1, "latency_ms": 20.0, "recall_at_1": 1.0, "recall_at_5": 1.0, "recall_at_10": 1.0, "recall_any_at_5": 1.0, "ndcg_at_5": 1.0},
                ],
            },
            "elapsed": 0.0,
        },
    ]
    loc_agg = run_full_eval.aggregate_loc_reports(loc_results)
    assert loc_agg["ok"] is True
    assert loc_agg["sample_count"] == 2
    assert loc_agg["failure_count"] == 1
    assert loc_agg["latency_ms_avg"] == 20.0


def test_full_eval_final_report_includes_required_metadata() -> None:
    lme_agg = {"ok": True, "sample_count": 2, "retrieval_recall_at_1": 0.1, "mrr": 0.2}
    loc_agg = {"ok": True, "sample_count": 3, "recall_at_1": 0.4, "mrr": 0.5}
    config = run_full_eval.load_eval_config({"EIMEMORY_WORKERS": "8", "EIMEMORY_RERANKER": "llm", "EIMEMORY_LME_LIMIT": "7"})
    report = run_full_eval.build_full_eval_report(
        lme_agg=lme_agg,
        loc_agg=loc_agg,
        config=config,
        lme_cases=8,
        locomo_cases=12,
        lme_chunk_count=3,
        locomo_chunk_count=4,
        lme_name="longmemeval-s-cleaned",
        locomo_name="locomo10-full",
        generated_at="2026-06-18T00:00:00",
    )
    assert report["n_workers"] == 8
    assert report["reranker"] == "llm"
    assert report["candidate_metadata"]["lme"]["dataset_name"] == "longmemeval-s-cleaned"
    assert report["candidate_metadata"]["locomo"]["dataset_name"] == "locomo10-full"
    assert report["lme"]["granularity"] == "turn"
    assert report["locomo"]["granularity"] == "turn"
    assert report["lme"]["limit"] == 7
    assert report["locomo"]["limit"] == 3
    assert report["generated_at"] == "2026-06-18T00:00:00"


def test_worker_reranker_env_maps_cli_modes(monkeypatch) -> None:
    monkeypatch.delenv("EIMEMORY_RAW_RETRIEVAL_RERANK", raising=False)
    monkeypatch.setenv("EIMEMORY_RAW_RETRIEVAL_RERANK_ENABLED", "1")
    monkeypatch.delenv("EIMEMORY_RAW_RERANK_API_KEY", raising=False)
    monkeypatch.delenv("EIMEMORY_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    run_full_eval.apply_worker_reranker_env("llm")
    assert run_full_eval.os.environ["EIMEMORY_RAW_RETRIEVAL_RERANK"] == "1"

    run_full_eval.apply_worker_reranker_env("deterministic")
    assert run_full_eval.os.environ["EIMEMORY_RAW_RETRIEVAL_RERANK"] == "0"
    assert run_full_eval.os.environ["EIMEMORY_RAW_RETRIEVAL_RERANK_ENABLED"] == "0"

    run_full_eval.apply_worker_reranker_env("auto")
    assert run_full_eval.os.environ["EIMEMORY_RAW_RETRIEVAL_RERANK"] == "0"
