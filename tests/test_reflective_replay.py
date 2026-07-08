from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest


def _load_reflective_replay():
    path = Path("scripts/reflective_replay.py")
    spec = importlib.util.spec_from_file_location("reflective_replay", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE event_outcomes (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            reason TEXT NOT NULL,
            correction_from_user TEXT NOT NULL,
            policy_update TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE recall_index (
            storage_key TEXT PRIMARY KEY,
            record_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            lane TEXT NOT NULL,
            visibility TEXT NOT NULL,
            source_class TEXT NOT NULL,
            memory_type TEXT NOT NULL,
            projection_type TEXT NOT NULL,
            quality_score REAL NOT NULL DEFAULT 0.0,
            title_text TEXT NOT NULL,
            body_text TEXT NOT NULL,
            anchor_terms TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    return conn


def _insert_outcome(conn: sqlite3.Connection, *, row_id: str, reason: str, recorded_at: str) -> None:
    conn.execute(
        """
        INSERT INTO event_outcomes (
            id, event_id, outcome, reason, correction_from_user, policy_update,
            tenant_id, agent_id, workspace_id, user_id, payload_json, recorded_at
        ) VALUES (?, ?, 'bad', ?, '', '', 'default', 'hongtu', '', '', '{}', ?)
        """,
        (row_id, f"event-{row_id}", reason, recorded_at),
    )


def test_gpt_failure_is_skipped_without_minimax_fallback() -> None:
    rr = _load_reflective_replay()
    calls: list[str] = []

    def executor(model: str, _prompt: str) -> str:
        calls.append(model)
        raise RuntimeError("rate limited")

    result = rr.analyze_case(
        {"id": "bad-1", "reason": "context window overflow"},
        model="gpt-5.5",
        fallback_model="MiniMax-M3",
        allow_fallback_minimax=False,
        executor=executor,
    )

    assert calls == ["gpt-5.5"]
    assert result["status"] == "skipped"
    assert result["skipped_reason"] == "primary_model_failed"
    assert result["model_used"] is None
    assert result["root_cause"] == ""


def test_top_up_excludes_actual_selected_case_ids() -> None:
    rr = _load_reflective_replay()
    conn = _make_conn()
    _insert_outcome(conn, row_id="other-new", reason="bridge timeout", recorded_at="2026-07-08T00:03:00+00:00")
    _insert_outcome(conn, row_id="context-old", reason="context window overflow", recorded_at="2026-07-08T00:02:00+00:00")
    _insert_outcome(conn, row_id="other-old", reason="tool timeout", recorded_at="2026-07-08T00:01:00+00:00")

    cases = rr.select_replay_cases(
        conn,
        limit=3,
        context_limit=1,
        capability_limit=0,
        source_snapshot_at="2026-07-08T00:04:00+00:00",
    )

    ids = [case["id"] for case in cases]
    assert ids.count("context-old") == 1
    assert ids == ["context-old", "other-new", "other-old"]


def test_minimax_key_is_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    rr = _load_reflective_replay()
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("EIMEMORY_MINIMAX_API_KEY", raising=False)

    assert rr.load_minimax_api_key() is None

    monkeypatch.setenv("EIMEMORY_MINIMAX_API_KEY", "env-key")

    assert rr.load_minimax_api_key() == "env-key"


def test_allowed_minimax_fallback_requires_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    rr = _load_reflective_replay()
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("EIMEMORY_MINIMAX_API_KEY", raising=False)
    calls: list[str] = []

    def executor(model: str, _prompt: str) -> str:
        calls.append(model)
        if model == "gpt-5.5":
            raise RuntimeError("rate limited")
        return "fallback analysis"

    result = rr.analyze_case(
        {"id": "bad-1", "reason": "context window overflow"},
        model="gpt-5.5",
        fallback_model="MiniMax-M3",
        allow_fallback_minimax=True,
        executor=executor,
    )

    assert calls == ["gpt-5.5"]
    assert result["status"] == "skipped"
    assert result["skipped_reason"] == "fallback_minimax_key_missing"
    assert result["root_cause"] == ""


def test_markdown_report_includes_source_snapshot_metadata() -> None:
    rr = _load_reflective_replay()

    markdown = rr.render_markdown_report(
        {
            "report_type": "reflective_replay_pilot",
            "generated_at": "2026-07-08T00:10:00+00:00",
            "source_snapshot_at": "2026-07-08T00:04:00+00:00",
            "model_usage": {"gpt-5.5": 2, "MiniMax-M3": 0},
            "case_count": 2,
            "skipped_count": 0,
            "cases": [],
        }
    )

    assert "source_snapshot_at: 2026-07-08T00:04:00+00:00" in markdown
    assert "generated_at: 2026-07-08T00:10:00+00:00" in markdown
