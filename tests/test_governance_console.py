from __future__ import annotations

from pathlib import Path

from eimemory.governance.console import render_evolution_console, write_evolution_console


def _sample_snapshot() -> dict:
    return {
        "ok": True,
        "generated_at": "2026-04-23T10:30:00+08:00",
        "snapshot_schema_version": 1,
        "scope": {"agent_id": "main", "workspace_id": "repo-x"},
        "memory_quality": {
            "memory_count": 2,
            "accepted_count": 1,
            "quality_distribution": {"candidate": 1, "confirmed": 1, "core": 0, "rejected": 0},
            "average_salience": 0.625,
            "by_source": {"cli": 1, "openclaw.message_received": 1},
            "by_memory_type": {"decision": 1, "conversation": 1},
        },
        "reflection_stats": {"reflection_count": 1, "unknown_count": 1},
        "rules": {
            "active_count": 1,
            "accepted_count": 1,
            "candidate_count": 0,
            "rejected_count": 0,
            "total_count": 2,
        },
        "recall_gaps": {
            "unknown_count": 1,
            "latest": {
                "title": '<script>alert("x")</script>',
                "summary": "Missing memory gap",
                "meta": {"query": "operator recall gap"},
            },
        },
        "source_candidates": {
            "count": 1,
            "latest": {"title": "Source candidate", "summary": "Potential source", "meta": {}},
            "list": [{"title": "Source candidate", "summary": "Potential source", "meta": {}}],
        },
        "active_intake": {
            "candidate_count": 1,
            "promoted_candidate_count": 1,
            "paper_source_count": 1,
            "knowledge_page_count": 1,
            "external_collection": {"latest_report": {"ok": True, "written_count": 1, "error_count": 0}},
            "paper_promotion": {"latest_report": {"ok": True, "promoted_count": 1, "skipped_count": 0}},
            "operational_projection": {
                "projected_memory_count": 1,
                "latest_report": {"ok": True, "projected_count": 1, "skipped_count": 0},
                "recent_projected_memories": [
                    {
                        "title": "Operational page: Runtime Memory",
                        "summary": "Runtime recall should prefer verified operational knowledge.",
                        "meta": {"projection_type": "operational_knowledge"},
                    }
                ],
            },
            "recent_candidates": [
                {
                    "title": "Knowledge candidate: Operational paper",
                    "summary": "Paper candidate ready for promotion",
                    "status": "promoted",
                    "source_kind": "paper",
                    "source_uri": "https://arxiv.org/abs/2604.19740",
                    "promotion": {"paper_source_id": "psrc_operational"},
                    "meta": {},
                }
            ],
            "recent_paper_sources": [
                {
                    "title": "Operational Memory Paper",
                    "summary": "A paper about operational memory.",
                    "source_kind": "arxiv",
                    "source_uri": "https://arxiv.org/abs/2604.19740",
                    "meta": {},
                }
            ],
            "recent_knowledge_pages": [
                {
                    "title": "Operational Memory",
                    "summary": "Runtime recall should prefer verified operational memory records.",
                    "page_type": "topic",
                    "meta": {},
                }
            ],
        },
        "memory_eval_ci": {
            "count": 1,
            "latest": {
                "name": "nightly-memory-ci-smoke",
                "pass_rate": 0.75,
                "passed_threshold": False,
                "fail_count": 1,
                "incident_count": 2,
            },
        },
        "backups": {
            "count": 1,
            "latest": {"path": "backups/run-1", "ok": True, "verified": True},
            "list": [{"path": "backups/run-1", "ok": True, "verified": True}],
        },
        "health": {"ok": True, "warnings": []},
    }


def test_render_evolution_console_includes_key_sections() -> None:
    html = render_evolution_console(_sample_snapshot())

    assert "<section" in html
    assert "Memory Quality" in html
    assert "Rules" in html
    assert "Recall Gaps" in html
    assert "Source Candidates" in html
    assert "Backups/Health" in html
    assert "Reflections" in html
    assert "External Intake" in html
    assert "Paper Promotion" in html
    assert "Memory Eval CI" in html
    assert "Operational Projection" in html
    assert "Recent Papers / Candidates" in html
    assert "0.75" in html
    assert "Operational Memory Paper" in html
    assert 'data-reset-layout' in html
    assert 'draggable="true"' in html
    assert 'data-card-id="active-intake"' in html
    assert "Generated 2026-04-23T10:30:00+08:00" in html
    assert "Schema v1" in html


def test_render_evolution_console_escapes_user_content() -> None:
    html = render_evolution_console(_sample_snapshot())

    assert "<script>alert(\"x\")</script>" not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html


def test_write_evolution_console_writes_html_file(tmp_path) -> None:
    output_path = tmp_path / "nested" / "governance" / "evolution-console.html"

    report = write_evolution_console(_sample_snapshot(), output_path)

    assert output_path.exists()
    assert output_path.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert report["path"] == str(output_path)
    assert report["bytes_written"] > 0
