from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.governance.replay_dataset import build_replay_dataset, _cases_from_outcome_traces


def test_replay_dataset_extracts_user_correction_from_event_outcome(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    event = runtime.record_event(
        {
            "id": "evt_music",
            "user_phrase": "给我唱首歌",
            "event_type": "media_playback",
            "interpreted_intent": "Play audible music",
            "goal": "User hears music",
            "next_policy": "Confirm song and playback output first.",
        },
        scope=scope,
    )
    runtime.record_outcome(
        event["id"],
        {
            "outcome": "bad",
            "reason": "Misread playback as lyric writing",
            "correction_from_user": "不是写歌词，是要让我能听见",
            "policy_update": "Ask song and playback channel before acting.",
        },
        scope=scope,
    )

    report = build_replay_dataset(runtime, scope=scope, persist=True)

    assert report["ok"] is True
    assert report["schema_version"] == "real_task_replay.v1"
    assert report["case_count"] == 1
    assert report["correction_count"] == 1
    assert report["cases"][0]["correction_from_user"] == "不是写歌词，是要让我能听见"
    assert report["persisted_record_id"]


def test_replay_dataset_collects_bad_outcomes_and_operator_corrections(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    scope_ref = ScopeRef.from_dict(scope)

    runtime.record_event(
        {
            "id": "evt_wrong_song",
            "user_phrase": "再放一次",
            "event_type": "media_playback",
            "goal": "用户要再次听到歌曲",
        },
        scope=scope,
    )
    runtime.record_outcome(
        "evt_wrong_song",
        {
            "outcome": "bad",
            "reason": "错误回复为歌词文本，没有播放媒体",
        },
        scope=scope,
    )
    runtime.record_event(
        {
            "id": "evt_fixed_song",
            "user_phrase": "请帮我打开音乐",
            "event_type": "media_playback",
            "goal": "打开音乐播放器并播放",
            "next_policy": "先确认播放器状态",
        },
        scope=scope,
    )
    runtime.record_outcome(
        "evt_fixed_song",
        {
            "outcome": "success",
            "correction_from_user": "不是文字说明，是直接播放",
            "reason": "用户需要听到音乐",
        },
        scope=scope,
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="memory",
            title="用户要求直接播放",
            summary="不要再给我歌词，直接播放",
            content={"text": "请直接播放音乐", "memory_type": "operator.correction"},
            scope=scope_ref,
            source="operator.correction",
            meta={"memory_type": "operator.correction"},
        )
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="replay_result",
            title="Replay suggestion for media fallback",
            summary="Replay expected playlist selection before response.",
            scope=scope_ref,
            source="unit.test",
            meta={"verdict": "fail", "pass_rate": 0.2},
            content={
                "suggested_replay_dataset": [
                    {
                        "query": "播放音乐",
                        "expect_any_text": ["播放完成"],
                        "negative_expected_text": ["只显示歌词"],
                        "task_type": "media_playback",
                    }
                ]
            },
        )
    )

    report = build_replay_dataset(runtime, scope=scope, limit=50, persist=True)

    assert report["report_type"] == "proactive_replay_dataset"
    assert report["schema_version"] == "real_task_replay.v1"
    assert report["case_count"] >= 3
    assert report["correction_count"] >= 2
    assert report["persisted_record_id"]
    assert {case.get("source") for case in report["cases"]} >= {"event_outcome", "operator_correction", "replay_result"}
    assert any("直接播放" in str(case.get("correction_from_user") or "") for case in report["cases"])

    persisted = runtime.store.get_by_id(report["persisted_record_id"], scope=scope)
    assert persisted is not None
    assert persisted.meta.get("report_type") == "proactive_replay_dataset"
    assert persisted.meta.get("schema_version") == "real_task_replay.v1"


def test_replay_dataset_ignores_previous_dataset_and_replay_reports(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    scope_ref = ScopeRef.from_dict(scope)
    runtime.store.append(
        RecordEnvelope.create(
            kind="replay_result",
            title="Previous proactive replay dataset",
            summary="Should not recursively generate more cases.",
            scope=scope_ref,
            source="unit.test",
            meta={"report_type": "proactive_replay_dataset", "schema_version": "real_task_replay.v1"},
            content={"cases": [{"query": "recursive noise", "expected_text": ["noise"]}]},
        )
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="replay_result",
            title="Previous real task replay",
            summary="Should not become a new replay case either.",
            scope=scope_ref,
            source="unit.test",
            meta={"report_type": "real_task_replay", "verdict": "fail", "pass_rate": 0.0},
            content={"report": {"samples": [{"query": "old failed sample", "expected_text": ["old"]}]}},
        )
    )

    report = build_replay_dataset(runtime, scope=scope, limit=50, persist=False)

    assert report["case_count"] == 0
    assert report["cases"] == []


def test_replay_dataset_reports_quality_filtering_and_targets(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    scope_ref = ScopeRef.from_dict(scope)

    runtime.store.append(
        RecordEnvelope.create(
            kind="replay_result",
            title="Noisy replay suggestions",
            summary="Includes operational noise and one real task.",
            scope=scope_ref,
            source="unit.test",
            meta={"verdict": "fail", "task_type": "ops.inspect"},
            content={
                "suggested_replay_dataset": [
                    {"query": "heartbeat ping", "expected_text": ["alive"]},
                    {"query": "usage-limit exceeded", "expected_text": ["wait"]},
                    {
                        "query": "msg_01HZY8R9WQ4WVX7M5G2Q3Q4Q4Q",
                        "expected": "Inspect the failed job before replying.",
                        "expected_text": [
                            "Open the operations console before answering.",
                            "Inspect the latest failed job.",
                            "Summarize the concrete failure cause.",
                        ],
                        "correction": "The user wanted live job inspection, not generic advice.",
                        "task_type": "ops.inspect",
                    },
                ]
            },
        )
    )

    report = build_replay_dataset(runtime, scope=scope, limit=50, persist=True)

    assert report["case_count"] == 1
    assert report["filtered_count"] == 2
    assert report["filter_reasons"] == {"heartbeat": 1, "usage_limit": 1}
    assert 0.0 <= report["quality_score"] <= 1.0
    assert report["case_quality_breakdown"]["accepted"] == 1
    assert report["target_pass_rate"] == 0.85
    assert report["cases"][0]["query"] == "Inspect the failed job before replying."
    assert len(report["cases"][0]["expected_text"]) >= 3

    persisted = runtime.store.get_by_id(report["persisted_record_id"], scope=scope)
    assert persisted is not None
    assert persisted.meta.get("filtered_count") == 2
    assert persisted.meta.get("target_pass_rate") == 0.85


def test_outcome_trace_replay_cases_use_indexed_report_type_lookup(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict({"agent_id": "hongtu"})
    runtime.store.append(
        RecordEnvelope.create(
            kind="reflection",
            title="Outcome trace",
            summary="User corrected the routing answer.",
            scope=scope,
            source="unit.test",
            meta={"report_type": "outcome_trace", "primary_label": "user_correction"},
            content={
                "input_summary": "latest version?",
                "policy_update": "Query git/runtime before answering version questions.",
                "expected_text": ["git", "runtime"],
            },
        )
    )

    def fail_record_scan(*_args, **_kwargs):
        raise AssertionError("outcome trace replay lookup must not scan record pages")

    monkeypatch.setattr(runtime.store, "list_records", fail_record_scan)
    cases = _cases_from_outcome_traces(runtime, scope=scope, limit=10)

    assert len(cases) == 1
    assert cases[0]["source"] == "outcome_trace"
