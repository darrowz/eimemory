from __future__ import annotations

from pathlib import Path

from eimemory.governance.console import render_evolution_console, write_evolution_console


def _sample_snapshot() -> dict:
    return {
        "ok": True,
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


def test_render_evolution_console_escapes_user_content() -> None:
    html = render_evolution_console(_sample_snapshot())

    assert "<script>alert(\"x\")</script>" not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html


def test_write_evolution_console_writes_html_file(tmp_path) -> None:
    output_path = tmp_path / "evolution-console.html"

    report = write_evolution_console(_sample_snapshot(), output_path)

    assert output_path.exists()
    assert output_path.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert report["path"] == str(output_path)
    assert report["bytes_written"] > 0
