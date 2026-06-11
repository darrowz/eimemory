from __future__ import annotations

from eimemory.governance.replay_quality import govern_replay_cases


def test_replay_quality_filters_operational_noise_and_normalizes_cases() -> None:
    long_log = "\n".join(
        [
            "Traceback (most recent call last):",
            "  File \"worker.py\", line 10, in run",
            "TimeoutError: request timed out after 30000ms",
            "[2026-06-01T00:00:00Z] retrying task",
            "[2026-06-01T00:00:01Z] retrying task",
            "[2026-06-01T00:00:02Z] retrying task",
        ]
    )
    cases = [
        {"case_id": "heartbeat", "query": "heartbeat ping", "expected_text": ["alive"]},
        {"case_id": "timeout", "query": "TimeoutError: request timed out", "expected_text": ["retry"]},
        {"case_id": "usage", "query": "usage-limit exceeded", "expected_text": ["wait"]},
        {"case_id": "message", "query": "msg_01HZY8R9WQ4WVX7M5G2Q3Q4Q4Q", "expected_text": ["route"]},
        {"case_id": "system_error", "query": "Error: 500 Internal Server Error", "expected_text": ["recover"]},
        {"case_id": "short", "query": "ok", "expected_text": ["respond"]},
        {"case_id": "log", "query": long_log, "expected_text": ["debug"]},
        {
            "case_id": "real",
            "source": "event_outcome",
            "query": "msg_01HZY8R9WQ4WVX7M5G2Q3Q4Q4Q",
            "expected": "Inspect the failed job before replying.",
            "expected_text": [
                "Open the operations console and inspect the failed job before answering.",
                "Summarize the failure cause in one short sentence.",
                "Do not answer from memory when live job evidence is required.",
                "Include the next recovery action for the operator.",
                "Avoid fabricating logs.",
                "This extra item should be trimmed.",
            ],
            "correction_from_user": "The user wanted a live failed-job inspection, not a generic status.",
            "task_type": "ops.inspect",
        },
    ]

    governed = govern_replay_cases(cases, limit=20)

    assert governed["filtered_count"] == 7
    assert governed["filter_reasons"] == {
        "heartbeat": 1,
        "timeout": 1,
        "usage_limit": 1,
        "message_id": 1,
        "system_error": 1,
        "short_query": 1,
        "long_log_fragment": 1,
    }
    assert governed["case_quality_breakdown"]["accepted"] == 1
    assert governed["target_pass_rate"] == 0.85

    accepted = governed["cases"]
    assert len(accepted) == 1
    assert accepted[0]["query"] == "Inspect the failed job before replying."
    assert accepted[0]["input"] == accepted[0]["query"]
    assert len(accepted[0]["expected_text"]) == 5
    assert all(len(item) <= 160 for item in accepted[0]["expected_text"])
    assert accepted[0]["quality_score"] >= 0.7
