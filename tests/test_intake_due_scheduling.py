"""PERF: intake multi-source loop should skip sources that are not due."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from eimemory.intake.loop import KnowledgeIntakeLoop
from eimemory.intake.registry import SourceEntry, SourceRegistry, source_is_due


def test_source_frequency_gates_due_checks() -> None:
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    paused = SourceEntry(source_id="paused", source_kind="manual", metadata={"frequency": "paused"})
    recent = SourceEntry(
        source_id="recent",
        source_kind="manual",
        metadata={"frequency": "daily"},
        last_scanned_at=(now - timedelta(hours=2)).isoformat(),
    )
    due = SourceEntry(
        source_id="due",
        source_kind="manual",
        metadata={"frequency": "daily"},
        last_scanned_at=(now - timedelta(hours=30)).isoformat(),
    )
    assert source_is_due(paused, now=now) is False
    assert source_is_due(recent, now=now) is False
    assert source_is_due(due, now=now) is True


def test_intake_run_skips_sources_not_due(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    registry = SourceRegistry(tmp_path / "sources.json")
    due_doc = tmp_path / "due.md"
    skip_doc = tmp_path / "skip.md"
    due_doc.write_text("# due source\n\nEnough content for intake screening to accept.", encoding="utf-8")
    skip_doc.write_text("# skip source\n\nEnough content for intake screening to accept.", encoding="utf-8")
    registry.add_source(
        {
            "source_id": "due-src",
            "source_kind": "manual",
            "title": "due",
            "uri": str(due_doc),
            "last_scanned_at": (now - timedelta(hours=30)).isoformat(),
            "metadata": {"frequency": "daily"},
        }
    )
    registry.add_source(
        {
            "source_id": "skip-src",
            "source_kind": "manual",
            "title": "skip",
            "uri": str(skip_doc),
            "last_scanned_at": (now - timedelta(hours=1)).isoformat(),
            "metadata": {"frequency": "daily"},
        }
    )
    import eimemory.intake.registry as registry_mod

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(registry_mod, "datetime", _FrozenDateTime)
    loop = KnowledgeIntakeLoop(sources=registry, store=type("Store", (), {"root": tmp_path})())
    result = loop.run(scope={"tenant_id": "t", "agent_id": "a", "workspace_id": "w", "user_id": "u"})
    assert result["skipped_not_due_count"] == 1
    assert result["scanned_count"] == 1
    assert result["candidates"][0]["source_id"] == "due-src"
