from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.living import LIVING_MEMORY_META_KEY
from eimemory.models.records import RecordEnvelope, ScopeRef


def _scope() -> dict[str, str]:
    return {"agent_id": "hongtu", "workspace_id": "living", "user_id": "darrow"}


def _append_legacy_preference(runtime: Runtime, scope: dict[str, str], *, title: str, summary: str) -> str:
    record = RecordEnvelope.create(
        kind="memory",
        title=title,
        summary=summary,
        content={"text": summary, "memory_type": "preference"},
        scope=ScopeRef.from_dict(scope),
        meta={"memory_type": "preference", "force_capture": True},
    )
    # Keep legacy shape for compatibility: no explicit living_memory_v1.
    record.meta.pop(LIVING_MEMORY_META_KEY, None)
    runtime.store.append(record)
    return record.record_id


def test_living_posture_query_builds_strict_checklist_profile(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = _scope()

    preference_record = runtime.memory.ingest(
        text="UUMit 外部订单交付清单要求：验收前先确认，不要臆测，必须有证据。",
        memory_type="preference",
        title="UUMit 外部订单交付品质要求",
        scope=scope,
        force_capture=True,
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="memory",
            title="UUMit 项目交付标准",
            summary="外部订单验收要逐项确认清单，交付品质必须达标并留存证据。",
            content={
                "text": "外部订单验收要逐项确认清单，交付品质必须达标并留存证据。",
                "memory_type": "project",
            },
            scope=ScopeRef.from_dict(scope),
            meta={"memory_type": "project", "force_capture": True},
        )
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="rule",
            title="交付清单规范",
            summary="先确认再执行，避免没有证据地做决定。",
            content={
                "task_type": "delivery_acceptance",
                "response_policy": {"recovery_mode": "strict"},
            },
            scope=ScopeRef.from_dict(scope),
            meta={"task_type": "delivery_acceptance"},
        )
    )

    report = runtime.recommend_action_posture("UUMit 外部订单交付品质要求", scope=scope, limit=10)
    profile = report["profile"]

    assert report["ok"] is True
    assert report["record_count"] >= 3
    assert profile["scope"]["project"] == "UUMit"
    assert profile["scope"]["task_type"] == "delivery_acceptance"
    assert profile["mode"] == "strict_checklist"
    assert profile["recommended_action"] == "act"
    assert preference_record.record_id in profile["source_record_ids"]
    assert "evidence_required" in profile["constraints"] or "strict_acceptance" in profile["constraints"]


def test_living_posture_query_compiles_concise_preference_and_no_fluff(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = _scope()

    legacy_record_id = _append_legacy_preference(
        runtime,
        scope,
        title="鸿哥沟通风格记录",
        summary="鸿哥 沟通风格要极简、直接、讨厌废话。",
    )

    report = runtime.recommend_action_posture("鸿哥 沟通风格/极简/直接/讨厌废话", scope=scope, limit=10)
    profile = report["profile"]

    assert report["ok"] is True
    assert profile["mode"] == "concise_preference"
    assert "no_fluff" in profile["constraints"]
    assert report["record_count"] == 1
    assert report["profile"]["source_record_ids"] == [legacy_record_id]
    assert profile["recommended_action"] in {"nudge", "wait"}

def test_living_posture_query_repair_signal_raises_repair_mode(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = _scope()

    runtime.memory.ingest(
        text="我已经指出你越界，必须修复这个问题并重新确认边界，不要再忽略。",
        memory_type="operator.correction",
        title="运营修复边界",
        scope=scope,
        force_capture=True,
    )
    runtime.evolution.observe(
        signal_type="incident",
        payload={"title": "信任修复反馈", "summary": "修复信任问题，边界被重复突破"},
        scope=scope,
    )

    report = runtime.recommend_action_posture("请处理修复并尊重边界", scope=scope, limit=10)
    profile = report["profile"]

    assert report["ok"] is True
    assert report["record_count"] >= 2
    assert profile["recommended_action"] == "act"
    assert profile["mode"] in {"repair_first", "strict_checklist"}
    assert "repair_trust" in profile["constraints"] or "respect_boundary" in profile["constraints"]


def test_living_posture_uses_legacy_memory_without_living_meta(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = _scope()

    _append_legacy_preference(
        runtime,
        scope,
        title="老偏好记录",
        summary="我希望答案尽量精简，不要讲废话，尽快给出结论。",
    )

    report = runtime.recommend_action_posture("请按简洁风格回答", scope=scope, limit=10)
    profile = report["profile"]

    assert report["ok"] is True
    assert report["record_count"] == 1
    assert profile["mode"] == "concise_preference"
    assert "no_fluff" in profile["constraints"]
    assert profile["source_record_ids"]


def test_living_posture_excludes_rejected_and_internal_audit_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = _scope()

    runtime.memory.ingest(
        text="请保留执行动作优先级。",
        memory_type="preference",
        title="有效偏好",
        scope=scope,
        force_capture=True,
    )
    runtime.memory.ingest(
        text="系统审计记录不应被当作用户姿态。",
        memory_type="audit",
        title="OpenClaw command audit",
        source="ei_bridge.openclaw_feishu",
        scope=scope,
        force_capture=True,
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="memory",
            title="拒绝样本",
            summary="rejected memory should be ignored",
            content={"text": "rejected memory should be ignored", "memory_type": "preference"},
            scope=ScopeRef.from_dict(scope),
            meta={"memory_type": "preference", "force_capture": True},
            status="rejected",
        )
    )

    report = runtime.recommend_action_posture("执行动作优先级", scope=scope, limit=10)
    report_ids = report["profile"]["source_record_ids"]

    assert report["ok"] is True
    assert report["record_count"] == 1
    assert "有效偏好" == report["items"][0]["title"]
    assert all("OpenClaw command audit" not in rid for rid in report_ids)
