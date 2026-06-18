from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.evaluation.longmemeval import _ingest_case_chunks, _retrieve, normalize_longmemeval_dataset, run_longmemeval
from eimemory.evaluation.locomo import run_locomo
from eimemory.models.records import ScopeRef


def _sorted_cross_case_raw_chunks(records, limit: int) -> list[dict]:
    def _case_id(record) -> str:
        return str(record.meta.get("longmemeval_case_id") or record.content.get("longmemeval_case_id") or "")

    ordered = sorted(records, key=_case_id, reverse=True)
    return [{"record": record, "base_score": float(index)} for index, record in enumerate(ordered[:limit])]


def _patch_search_raw_chunks(monkeypatch, limit: int = 2) -> None:
    def fake_search_raw_chunks(store, *, query, scope, task_context=None, limit=8):  # noqa: ARG001
        records = store.list_records(kinds=["raw_chunk"], scope=scope, limit=max(limit, limit * 2))
        return _sorted_cross_case_raw_chunks(records, max(limit, 2))

    monkeypatch.setattr("eimemory.raw.retrieval.search_raw_chunks", fake_search_raw_chunks)


def _longmemeval_isolation_dataset() -> dict:
    return {
        "name": "cross-case-isolation-lme",
        "scope": {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        "cases": [
            {
                "question_id": "a-case",
                "question": "Which city did I move to?",
                "question_type": "temporal",
                "answer": "Austin",
                "answer_session_ids": ["case-a-session"],
                "haystack_session_ids": ["case-a-session", "case-a-noise"],
                "haystack_sessions": [
                    [
                        {"role": "user", "content": "I moved to Austin in March."},
                    ],
                    [
                        {"role": "assistant", "content": "case A noise"},
                    ],
                ],
            },
            {
                "question_id": "z-case",
                "question": "Which city did I move to?",
                "question_type": "temporal",
                "answer": "Berlin",
                "answer_session_ids": ["case-z-session"],
                "haystack_session_ids": ["case-z-session", "case-z-noise"],
                "haystack_sessions": [
                    [
                        {"role": "user", "content": "I moved to Berlin in June."},
                    ],
                    [
                        {"role": "assistant", "content": "case Z noise"},
                    ],
                ],
            },
        ],
    }


def _locomo_isolation_dataset() -> dict:
    return {
        "name": "cross-case-isolation-locomo",
        "scope": {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        "cases": [
            {
                "case_id": "iso-a",
                "question": "Which city did I move to?",
                "answer": "Austin",
                "question_type": "temporal",
                "sessions": [
                    {
                        "session_id": "iso-a-session",
                        "turns": [
                            {"turn_id": "iso-a-1", "speaker": "user", "text": "I moved to Austin in March."},
                        ],
                    },
                ],
                "evidence_turn_ids": ["iso-a-1"],
                "evidence_session_ids": ["iso-a-session"],
            },
            {
                "case_id": "iso-z",
                "question": "Which city did I move to?",
                "answer": "Berlin",
                "question_type": "temporal",
                "sessions": [
                    {
                        "session_id": "iso-z-session",
                        "turns": [
                            {"turn_id": "iso-z-1", "speaker": "user", "text": "I moved to Berlin in June."},
                        ],
                    },
                ],
                "evidence_turn_ids": ["iso-z-1"],
                "evidence_session_ids": ["iso-z-session"],
            },
        ],
    }


def test_longmemeval_query_isolation_for_case_scope(monkeypatch, tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    _patch_search_raw_chunks(monkeypatch)

    try:
        report = run_longmemeval(
            runtime,
            _longmemeval_isolation_dataset(),
            mode="raw",
            granularity="session",
            limit=2,
        )
    finally:
        runtime.close()

    by_case = {sample["case_id"]: sample for sample in report["samples"]}
    assert by_case["a-case"]["rank"] == 1
    assert by_case["a-case"]["returned_ids"][0] == "case-a-session"
    assert by_case["z-case"]["rank"] == 1
    assert by_case["z-case"]["returned_ids"][0] == "case-z-session"


def test_locomo_query_isolation_for_case_scope(monkeypatch, tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    _patch_search_raw_chunks(monkeypatch)

    try:
        report = run_locomo(
            runtime,
            _locomo_isolation_dataset(),
            mode="raw",
            granularity="turn",
            limit=2,
        )
    finally:
        runtime.close()

    by_case = {sample["case_id"]: sample for sample in report["samples"]}
    assert by_case["iso-a"]["rank"] == 1
    assert by_case["iso-a"]["returned_ids"][0] == "iso-a-1"
    assert by_case["iso-z"]["rank"] == 1
    assert by_case["iso-z"]["returned_ids"][0] == "iso-z-1"


def test_case_isolation_falls_back_to_current_case_raw_chunks(monkeypatch, tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    dataset = normalize_longmemeval_dataset(_longmemeval_isolation_dataset())
    scope = ScopeRef.from_dict(dataset["scope"])
    try:
        for case in dataset["cases"]:
            _ingest_case_chunks(runtime, case=case, scope=scope)
        wrong_case = next(
            record
            for record in runtime.store.list_records(kinds=["raw_chunk"], scope=scope, limit=10)
            if record.meta.get("benchmark_case_id") == "a-case"
        )

        def wrong_raw_search(_store, *, query, scope, task_context=None, limit=8):  # noqa: ARG001
            return [{"record": wrong_case, "base_score": 9.0}]

        monkeypatch.setattr("eimemory.raw.retrieval.search_raw_chunks", wrong_raw_search)
        monkeypatch.setattr(runtime.store, "search", lambda **_kwargs: [wrong_case])

        retrieved = _retrieve(
            runtime,
            query="Which city did I move to?",
            scope=scope,
            mode="raw",
            limit=2,
            benchmark_case_id="z-case",
        )
    finally:
        runtime.close()

    assert retrieved
    assert retrieved[0].meta["benchmark_case_id"] == "z-case"
    assert retrieved[0].content["session_id"] == "case-z-session"
