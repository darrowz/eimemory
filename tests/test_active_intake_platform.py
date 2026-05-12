from __future__ import annotations

import json

from eimemory.cli.main import main as cli_main
from eimemory.scheduler.jobs import run_nightly_jobs


def test_intake_review_promote_policy_and_pack_cli_flow(tmp_path, monkeypatch, capsys) -> None:
    runtime_root = tmp_path / "runtime"
    doc = tmp_path / "source.md"
    pack_dir = tmp_path / "pack"
    doc.write_text(
        "Durable active intake knowledge can be reviewed, promoted, packed, and migrated.",
        encoding="utf-8",
    )
    monkeypatch.setenv("EIMEMORY_ROOT", str(runtime_root))

    assert cli_main(["source", "add", "--source-kind", "manual", "--title", "Durable note", "--uri", str(doc)]) == 0
    capsys.readouterr()
    assert cli_main(["intake", "run", "--persist"]) == 0
    run_report = json.loads(capsys.readouterr().out)
    fingerprint = run_report["candidates"][0]["fingerprint"]

    assert cli_main(["intake", "queue"]) == 0
    queue = json.loads(capsys.readouterr().out)
    candidate_id = queue[0]["record_id"]
    assert fingerprint[:12] in candidate_id

    assert cli_main(["intake", "review", candidate_id, "approve", "--reviewer", "tester"]) == 0
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["status"] == "reviewed"

    assert cli_main(["intake", "promote", candidate_id, "--promoter", "tester"]) == 0
    promoted = json.loads(capsys.readouterr().out)
    assert promoted["kind"] == "memory"
    assert promoted["meta"]["promoted_from"] == candidate_id

    assert cli_main(["intake", "policy", "--gap", "active intake gaps"]) == 0
    policy = json.loads(capsys.readouterr().out)
    assert "active intake gaps" in policy["gap_queries"]

    assert cli_main(["intake", "pack", "export", str(pack_dir), "--include-candidates"]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["record_count"] >= 1

    assert cli_main(["intake", "pack", "import", str(pack_dir), "--dry-run"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["dry_run"] is True
    assert imported["record_count"] == exported["record_count"]


def test_nightly_jobs_include_active_intake_reports(tmp_path) -> None:
    from eimemory.api.runtime import Runtime

    runtime = Runtime.create(root=tmp_path / "runtime")
    doc = tmp_path / "nightly.md"
    doc.write_text(
        "Nightly intake can safely persist durable knowledge candidates for later review.",
        encoding="utf-8",
    )
    runtime.sources.add_source(
        {
            "source_kind": "manual",
            "title": "Nightly source",
            "uri": str(doc),
            "enabled": True,
        }
    )

    report = run_nightly_jobs(runtime, scope={"agent_id": "main"})
    reloaded_source = runtime.sources.list_sources()[0]

    assert report["knowledge_intake"]["candidate_count"] == 1
    assert report["knowledge_intake"]["written_count"] == 1
    assert report["source_quality"]["source_count"] == 1
    assert reloaded_source.last_scanned_at
    assert reloaded_source.metadata["last_scan"]["status"] == "candidate"


def test_nightly_jobs_do_not_reset_reviewed_candidates(tmp_path) -> None:
    from eimemory.api.runtime import Runtime

    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "main"}
    doc = tmp_path / "reviewed.md"
    doc.write_text(
        "Reviewed candidates should not be reset by the next nightly run.",
        encoding="utf-8",
    )
    runtime.sources.add_source(
        {
            "source_kind": "manual",
            "title": "Reviewed source",
            "uri": str(doc),
            "enabled": True,
        }
    )

    first = run_nightly_jobs(runtime, scope=scope)
    candidate = runtime.store.list_records(kinds=["knowledge_candidate"], scope=scope, limit=1)[0]
    runtime.review_intake_candidate(
        record_id=candidate.record_id,
        decision="approve",
        reviewer="tester",
        scope=scope,
    )
    second = run_nightly_jobs(runtime, scope=scope)
    reloaded = runtime.store.get_by_id(candidate.record_id)

    assert first["knowledge_intake"]["written_count"] == 1
    assert second["knowledge_intake"]["skipped_existing_count"] == 1
    assert reloaded.status == "reviewed"


def test_nightly_jobs_fetch_and_persist_external_candidates(tmp_path) -> None:
    from eimemory.api.runtime import Runtime

    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "main"}
    source = runtime.sources.add_source(
        {
            "source_kind": "rss",
            "title": "External feed",
            "uri": "https://example.test/feed.xml",
            "enabled": True,
        }
    )

    def fake_fetch_text(url: str) -> str:
        assert url == "https://example.test/feed.xml"
        return """<?xml version="1.0"?>
        <rss><channel><item>
          <title>External intake item</title>
          <link>https://example.test/items/1</link>
          <description>Fetched external knowledge with enough durable detail for review.</description>
          <pubDate>Thu, 23 Apr 2026 03:30:00 GMT</pubDate>
        </item></channel></rss>"""

    report = run_nightly_jobs(runtime, scope=scope, external_fetch_text=fake_fetch_text)
    collected_records = runtime.store.list_records(kinds=["knowledge_candidate", "news"], scope=scope, limit=10)
    reloaded_source = runtime.sources.list_sources()[0]

    assert report["external_collection"]["ok"] is True
    assert report["external_collection"]["source_count"] == 1
    assert report["external_collection"]["fetched_item_count"] == 1
    assert report["external_collection"]["written_count"] == 1
    assert report["external_collection"]["error_count"] == 0
    assert reloaded_source.source_id == source.source_id
    assert reloaded_source.last_scanned_at
    assert reloaded_source.metadata["last_scan"]["status"] == "ok"
    assert reloaded_source.metadata["last_scan"]["item_count"] == 1
    assert reloaded_source.metadata["last_scan"]["written_count"] == 1
    assert any("External intake item" in record.title for record in collected_records)


def test_external_collection_applies_source_and_global_item_limits(tmp_path) -> None:
    from eimemory.api.runtime import Runtime

    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "main"}
    runtime.sources.add_source(
        {
            "source_kind": "rss",
            "title": "Large feed",
            "uri": "https://example.test/feed.xml",
            "enabled": True,
            "metadata": {"max_items": 2},
        }
    )

    def fake_fetch_text(_url: str) -> str:
        return """<?xml version="1.0"?>
        <rss><channel>
          <item><title>Item 1</title><link>https://example.test/1</link><description>Durable fetched content one.</description></item>
          <item><title>Item 2</title><link>https://example.test/2</link><description>Durable fetched content two.</description></item>
          <item><title>Item 3</title><link>https://example.test/3</link><description>Durable fetched content three.</description></item>
        </channel></rss>"""

    report = runtime.collect_external_sources(
        fetch=True,
        persist=True,
        fetch_text=fake_fetch_text,
        limit=1,
        scope=scope,
    )

    assert report["source_count"] == 1
    assert report["item_count"] == 1
    assert report["written_count"] == 1
    assert report["results"][0]["metadata"]["truncated"] is True


def test_nightly_jobs_reports_external_errors_without_failing(tmp_path) -> None:
    from eimemory.api.runtime import Runtime

    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "main"}
    runtime.sources.add_source(
        {
            "source_kind": "rss",
            "title": "Broken feed",
            "uri": "https://example.test/broken.xml",
            "enabled": True,
        }
    )

    def fake_fetch_text(url: str) -> str:
        raise OSError("network unavailable")

    report = run_nightly_jobs(runtime, scope=scope, external_fetch_text=fake_fetch_text)
    reloaded_source = runtime.sources.list_sources()[0]

    assert report["ok"] is True
    assert report["external_collection"]["ok"] is False
    assert report["external_collection"]["source_count"] == 1
    assert report["external_collection"]["written_count"] == 0
    assert report["external_collection"]["error_count"] == 1
    assert report["external_collection"]["errors"][0]["error"] == "fetch failed"
    assert reloaded_source.last_scanned_at
    assert reloaded_source.metadata["last_scan"]["status"] == "error"
    assert reloaded_source.metadata["last_scan"]["error"]


def test_nightly_jobs_fetch_persist_and_promote_paper_candidates(tmp_path) -> None:
    from eimemory.api.runtime import Runtime

    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "main"}
    runtime.sources.add_source(
        {
            "source_kind": "url",
            "title": "ChatPaper arXiv cs.AI",
            "uri": "https://www.chatpaper.ai/zh/dashboard/arxiv/cs/AI",
            "enabled": True,
            "tags": ["chatpaper", "arxiv", "paper"],
        }
    )

    def fake_fetch_text(url: str) -> str:
        assert url == "https://www.chatpaper.ai/api/papers/arxiv?category=cs.AI&page=1&language=zh"
        return json.dumps(
            {
                "papers": [
                    {
                        "id": "2604.19740v1",
                        "title": "Operational Memory for OpenClaw",
                        "abstract": "This paper shows that memory recall policy improves OpenClaw runtime responses with enough reusable detail.",
                        "publishedDate": "2026-04-21T17:59:02Z",
                        "arxivUrl": "https://arxiv.org/abs/2604.19740v1",
                        "pdfUrl": "https://arxiv.org/pdf/2604.19740v1.pdf",
                        "primaryCategory": "cs.AI",
                        "categories": ["cs.AI"],
                        "paper_translations": [
                            {
                                "language_code": "zh",
                                "title": "OpenClaw 的运行时记忆",
                                "abstract": "本文表明记忆召回策略可以改善 OpenClaw runtime responses，并提供足够可复用的细节。",
                            }
                        ],
                    }
                ],
                "total": 1,
            },
            ensure_ascii=False,
        )

    report = run_nightly_jobs(runtime, scope=scope, external_fetch_text=fake_fetch_text)

    paper_sources = runtime.store.list_records(kinds=["paper_source"], scope=scope, limit=10)
    claim_cards = runtime.store.list_records(kinds=["claim_card"], scope=scope, limit=10)
    knowledge_pages = runtime.store.list_records(kinds=["knowledge_page"], scope=scope, limit=10)
    candidates = runtime.store.list_records(kinds=["knowledge_candidate"], scope=scope, limit=10)

    assert report["external_collection"]["written_count"] == 1
    assert report["paper_promotion"]["promoted_count"] == 1
    assert paper_sources
    assert claim_cards
    assert knowledge_pages
    assert candidates[0].status == "promoted"
    assert candidates[0].meta["promoted_to_paper_source_id"] == paper_sources[0].record_id


def test_nightly_jobs_expand_sources_before_external_collection(tmp_path) -> None:
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope, ScopeRef

    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    runtime.sources.add_source(
        {
            "source_kind": "url",
            "title": "ChatPaper arXiv cs.AI",
            "uri": "https://www.chatpaper.ai/zh/dashboard/arxiv/cs/AI",
            "enabled": True,
            "tags": ["chatpaper", "arxiv", "paper"],
            "metadata": {"categories": ["cs.AI"], "max_items": 10},
        }
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="unknown",
            title="Need embodied robotics papers",
            summary="Recall missed embodied robotics papers.",
            detail="Recall missed embodied robotics papers.",
            scope=ScopeRef.from_dict(scope),
        )
    )
    fetched_urls: list[str] = []

    def fake_fetch_text(url: str) -> str:
        fetched_urls.append(url)
        return json.dumps({"papers": [], "total": 0})

    report = run_nightly_jobs(runtime, scope=scope, external_fetch_text=fake_fetch_text)
    source = runtime.sources.list_sources()[0]
    runtime.close()

    assert report["source_expansion"]["applied_count"] >= 1
    assert "cs.RO" in source.metadata["categories"]
    assert any("category=cs.RO" in url for url in fetched_urls)


def test_nightly_jobs_close_daily_brief_rule_evolution_and_source_discovery(tmp_path) -> None:
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope, ScopeRef

    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    memory = runtime.memory.ingest(
        text="Decision: nightly should prepare a daily brief outbox without calling Feishu.",
        memory_type="decision",
        title="Daily brief decision",
        source="openclaw.agent_end",
        force_capture=True,
        scope=scope,
    )
    runtime.evolution.feedback(
        target_ref={"kind": "memory", "record_id": memory.record_id},
        decision="accept",
        reason="Prefer concise operator-facing daily memory summaries",
        reviewed_by="tester",
        scope=scope,
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="unknown",
            title="Need news and product launches for AI memory tools",
            summary="Track news and product launches for AI memory tools.",
            scope=ScopeRef.from_dict(scope),
        )
    )

    report = run_nightly_jobs(runtime, scope=scope)
    rules = runtime.store.list_records(kinds=["rule"], scope=scope, status="accepted", limit=10)
    briefs = [
        record
        for record in runtime.store.list_records(kinds=["reflection"], scope=scope, limit=20)
        if record.source == "eimemory.daily_brief"
    ]
    source_candidates = [
        record
        for record in runtime.store.list_records(kinds=["source_candidate"], scope=scope, limit=20)
        if record.source == "eimemory.source_discovery"
    ]
    from eimemory.governance.snapshot import build_governance_snapshot

    snapshot = build_governance_snapshot(runtime, scope)
    runtime.close()

    assert report["daily_brief"]["ok"] is True
    assert report["daily_brief"]["persisted"] is True
    assert report["daily_brief"]["delivery_pending"] is False
    assert report["daily_brief"]["message_count"] >= 1
    assert briefs
    assert briefs[0].content["delivery"]["network_called"] is False
    assert report["rule_evolution"]["ok"] is True
    assert report["rule_evolution"]["created_rule_count"] == 1
    assert rules[0].meta["evolution_source"] == "rule_evolution_loop"
    assert report["source_discovery"]["ok"] is True
    assert report["source_discovery"]["proposal_count"] >= 1
    assert source_candidates
    assert source_candidates[0].meta["decision"] in {"approve", "needs_review"}
    assert snapshot["daily_brief"]["latest"]["delivery_status"] == "prepared"
    assert snapshot["rule_evolution"]["latest"]["created_rule_count"] == 1
    assert snapshot["source_discovery"]["count"] >= 1
