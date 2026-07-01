from __future__ import annotations

import json

from eimemory.knowledge.daily_brief import build_daily_brief, build_daily_brief_delivery_payload
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef


DAY = "2026-04-29"
SCOPE = ScopeRef(tenant_id="tenant-a", agent_id="openclaw", workspace_id="repo-x", user_id="alice")


def _record(
    *,
    kind: str,
    title: str,
    summary: str,
    source: str,
    occurred_at: str = f"{DAY}T08:00:00+08:00",
    detail: str = "",
    content: dict | None = None,
    tags: list[str] | None = None,
    meta: dict | None = None,
    status: str = "active",
) -> RecordEnvelope:
    record = RecordEnvelope.create(
        kind=kind,
        title=title,
        summary=summary,
        detail=detail,
        content=content or {},
        tags=tags or [],
        source=source,
        scope=SCOPE,
        meta=meta or {},
        status=status,
    )
    record.time = TimeRef(created_at=occurred_at, updated_at=occurred_at, occurred_at=occurred_at)
    return record


def test_daily_brief_generates_digest_and_experience_sections_from_today_records() -> None:
    records = [
        _record(
            kind="reflection",
            title="Research digest",
            summary="Two new retrieval papers were synthesized for the operator.",
            source="eimemory.research_digest",
            content={
                "digest": {
                    "items": [
                        {
                            "title": "Retrieval Agents",
                            "summary": "Retrieval agents improve long-horizon task reliability.",
                            "url": "https://example.test/retrieval-agents",
                        }
                    ]
                }
            },
            tags=["research_digest"],
        ),
        _record(
            kind="paper_source",
            title="Embodied Retrieval",
            summary="A paper showing compact retrieval improves embodied planning.",
            source="eimemory.paper_intake",
            content={"canonical_url": "https://arxiv.org/abs/2604.00001"},
            meta={"source_kind": "arxiv"},
        ),
        _record(
            kind="knowledge_page",
            title="Operational Memory",
            summary="Verified operational memories should be promoted into recall views.",
            source="eimemory.knowledge.compiler",
            content={"open_question_ids": ["q-follow-up"], "source_ids": ["paper_1"]},
        ),
        _record(
            kind="memory",
            title="OpenClaw user message",
            summary="Decision: daily brief must not call Feishu directly.",
            source="openclaw.message_received",
            content={"text": "Decision: daily brief must not call Feishu directly."},
            meta={"memory_type": "conversation"},
        ),
        _record(
            kind="memory",
            title="OpenClaw agent outcome",
            summary="Follow up: wire daily brief into scheduler after Runtime exposes a method.",
            source="openclaw.agent_end",
            content={"text": "Follow up: wire daily brief into scheduler after Runtime exposes a method."},
            meta={"memory_type": "conversation"},
        ),
        _record(
            kind="recall_view",
            title="OpenClaw memory injection audit",
            summary="Injected 3 memory records before prompt build",
            source="openclaw.before_prompt_build",
            content={"items": [{"record_id": "mem_1", "title": "Daily brief decision"}]},
            meta={"view_type": "prompt_injection"},
        ),
        _record(
            kind="paper_source",
            title="Yesterday paper",
            summary="This record should not appear in today's brief.",
            source="eimemory.paper_intake",
            occurred_at="2026-04-28T23:00:00+08:00",
        ),
    ]

    brief = build_daily_brief(records, date=DAY)

    assert brief["ok"] is True
    assert brief["date"] == DAY
    assert brief["conversation_summary"]["message_count"] == 2
    assert "daily brief must not call Feishu directly" in brief["decisions"][0]["text"]
    assert [item["title"] for item in brief["new_memories"]] == [
        "OpenClaw user message",
        "OpenClaw agent outcome",
    ]
    assert [item["title"] for item in brief["research_digest"]["items"]] == [
        "Research digest",
        "Embodied Retrieval",
        "Operational Memory",
    ]
    assert brief["followups"][0]["text"].startswith("Follow up: wire daily brief")
    assert brief["source_health"]["by_kind"]["paper_source"] == 1
    assert brief["source_health"]["by_source"]["openclaw.agent_end"] == 1
    json.dumps(brief)


def test_daily_brief_empty_day_returns_ok_empty_report() -> None:
    brief = build_daily_brief([], date=DAY)

    assert brief["ok"] is True
    assert brief["date"] == DAY
    assert brief["conversation_summary"]["message_count"] == 0
    assert brief["decisions"] == []
    assert brief["new_memories"] == []
    assert brief["research_digest"]["items"] == []
    assert brief["followups"] == []
    assert brief["source_health"]["record_count"] == 0
    json.dumps(brief)


def test_daily_brief_can_include_recent_research_without_counting_it_as_today_health() -> None:
    brief = build_daily_brief(
        [
            _record(
                kind="paper_source",
                title="Recent research paper",
                summary="A recent paper can still be surfaced in the research digest.",
                source="eimemory.paper_intake",
                occurred_at="2026-04-28T10:00:00+08:00",
                content={"canonical_url": "https://arxiv.org/abs/2604.00002"},
            )
        ],
        date=DAY,
        research_lookback_days=1,
    )

    assert [item["title"] for item in brief["research_digest"]["items"]] == ["Recent research paper"]
    assert brief["source_health"]["record_count"] == 0
    json.dumps(brief)


def test_daily_brief_delivery_payload_prepares_outbox_and_audit_without_network() -> None:
    brief = build_daily_brief(
        [
            _record(
                kind="memory",
                title="OpenClaw agent outcome",
                summary="Decision: keep daily brief delivery as an outbox payload.",
                source="openclaw.agent_end",
                content={"text": "Decision: keep daily brief delivery as an outbox payload."},
                meta={"memory_type": "conversation"},
            )
        ],
        date=DAY,
    )

    payload = build_daily_brief_delivery_payload(brief, channel="feishu")

    assert payload["ok"] is True
    assert payload["channel"] == "feishu"
    assert payload["network_called"] is False
    assert payload["outbox"]["kind"] == "daily_brief"
    assert payload["outbox"]["body"]["date"] == DAY
    assert payload["audit"]["action"] == "daily_brief.prepared"
    assert payload["audit"]["status"] == "prepared"
    json.dumps(payload)
