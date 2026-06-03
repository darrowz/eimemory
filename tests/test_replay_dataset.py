from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.governance.replay_dataset import build_replay_dataset


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
    assert report["case_count"] >= 3
    assert report["correction_count"] >= 2
    assert report["persisted_record_id"]
    assert {case.get("source") for case in report["cases"]} >= {"event_outcome", "operator_correction", "replay_result"}
    assert any("直接播放" in str(case.get("correction_from_user") or "") for case in report["cases"])

    persisted = runtime.store.get_by_id(report["persisted_record_id"], scope=scope)
    assert persisted is not None
    assert persisted.meta.get("report_type") == "proactive_replay_dataset"
