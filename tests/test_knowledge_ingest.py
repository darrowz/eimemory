from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef


def test_runtime_ingest_knowledge_source_extracts_multiple_unit_types(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    report = runtime.ingest_knowledge_source(
        {
            "source_kind": "webpage",
            "title": "Project Rocket Knowledge",
            "uri": "https://example.test/project-rocket/readme",
            "text": (
                "# Project Rocket\n"
                "Project Rocket is a compact knowledge utility for repeatable automation.\n"
                "\n"
                "## Features\n"
                "- Supports deterministic ingestion and recall.\n"
                "- Enforces lightweight validation.\n"
                "\n"
                "## Procedure\n"
                "1. Install dependencies.\n"
                "2. Run `rocket ingest --source README.md`.\n"
                "3. Verify with `rocket verify` before publish.\n"
                "\n"
                "## Verification\n"
                "- Run tests.\n"
                "- Run static checks.\n"
                "When the utility receives docs, prefer short verification output.\n"
            ),
            "metadata": {"scope": "knowledge"},
        },
        scope=scope,
        persist=False,
    )

    assert report["ok"] is True
    unit_types = {unit["unit_type"] for unit in report["knowledge_units"]}
    assert unit_types.issuperset({"concept", "procedure", "verification"})
    assert {"concept", "procedure", "constraint", "use_case", "anti_pattern", "verification"} >= unit_types


def test_source_trust_for_api_docs_is_higher_than_webpage_and_blog(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    api_report = runtime.ingest_knowledge_source(
        {
            "source_kind": "api_docs",
            "title": "Widget API",
            "uri": "https://example.test/api/widget",
            "text": "POST /api/v1/widgets creates a widget and returns a JSON object. Use 201 status for success.",
            "metadata": {},
        },
        persist=False,
    )
    web_report = runtime.ingest_knowledge_source(
        {
            "source_kind": "webpage",
            "title": "Random Blog",
            "uri": "https://example.test/random-post",
            "text": "This page lists product news and has mixed confidence snippets from community posts.",
            "metadata": {},
        },
        persist=False,
    )

    assert api_report["ok"] is True
    assert web_report["ok"] is True
    assert api_report["source_trust"] > web_report["source_trust"]
    assert web_report["source_trust"] >= 0.5


def test_persist_knowledge_units_without_storing_fulltext(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main", "workspace_id": "repo-x"}
    long_text = (
        "Deployment notes: release notes can be long and include many details. "
        "This sentence explains configuration, validation, safety, rollback strategy, "
        "error handling, and post-deploy checks. Use the following steps: "
        "1. prepare. 2. run checks. 3. release. 4. monitor. 5. validate."
    ) * 10
    report = runtime.ingest_knowledge_source(
        {
            "source_kind": "manual",
            "title": "Rocket Release Manual",
            "uri": "https://example.test/manual/release",
            "text": long_text,
            "metadata": {"source": "human"},
        },
        scope=scope,
        persist=True,
    )

    assert report["persist"] is True
    units = runtime.store.list_records(kinds=["knowledge_unit"], scope=scope, limit=20)
    assert units
    assert report["persisted_count"] == len(units)
    for unit in units:
        assert unit.kind == "knowledge_unit"
        assert unit.source == "eimemory.knowledge_ingest"
        assert unit.meta["source_kind"] == "manual"
        assert unit.meta["source_uri"] == "https://example.test/manual/release"
        assert unit.meta["unit_type"] in {"concept", "procedure", "constraint", "use_case", "anti_pattern", "verification"}
        assert unit.source == "eimemory.knowledge_ingest"
        assert unit.detail != long_text
        assert len(unit.detail) < len(long_text)
        assert unit.content["text"] != long_text
