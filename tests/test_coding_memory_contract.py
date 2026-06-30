from __future__ import annotations

from eimemory.api.runtime import Runtime


SCOPE = {"agent_id": "hongtu", "workspace_id": "graph-contract", "user_id": "darrow"}


def _coding_observation() -> dict:
    return {
        "session_id": "sess-sqlite-l5",
        "task": {"title": "Fix L5 SQLite disk I/O replay scan", "type": "bugfix"},
        "agent": {"id": "codex", "name": "Codex"},
        "project": {"name": "eimemory", "repo": "darrowz/eimemory"},
        "files": [{"path": "eimemory/governance/learning_state.py"}],
        "tools": [{"name": "pytest"}, {"name": "git"}],
        "commands": [
            {
                "command": "python -m pytest -q tests/test_autonomous_learning_state.py",
                "tool": "pytest",
                "summary": "Verified indexed idempotency lookup.",
            }
        ],
        "errors": [{"type": "sqlite", "message": "SQLite disk I/O error during L5 replay"}],
        "decisions": [
            {
                "summary": "Use indexed idempotency lookup and avoid fallback pagination.",
                "because": "SQLite disk I/O error during L5 replay",
            }
        ],
        "outcomes": [{"status": "fixed", "summary": "eimemory 1.7.5 deployed healthy"}],
        "replay_cases": [
            {
                "case_id": "l5-idempotency-scan",
                "query": "learn l5 --no-network",
                "expected_relations": ["FAILED_WITH", "DECIDED_BECAUSE", "VERIFIED_BY"],
            }
        ],
    }


def test_memory_observe_projects_coding_session_to_typed_graph(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = runtime.observe_coding_memory(_coding_observation(), scope=SCOPE)

    assert report["ok"] is True
    assert report["report_type"] == "coding_observation"
    assert report["record_id"]
    assert report["node_count"] >= 8
    assert {
        "TOUCHED_FILE",
        "RAN_COMMAND",
        "FAILED_WITH",
        "DECIDED_BECAUSE",
        "VERIFIED_BY",
        "PREVENTED_BY_REPLAY",
    }.issubset(set(report["relations"]))

    record = runtime.store.get_by_id(report["record_id"], scope=SCOPE)
    assert record is not None
    assert record.meta["report_type"] == "coding_observation"
    assert record.content["memory_type"] == "coding_session"

    edges = runtime.store.list_memory_edges(scope=SCOPE, record_ids=[report["record_id"]], limit=50)
    relations = {edge.meta.get("relation") for edge in edges}
    assert "FAILED_WITH" in relations
    assert any(edge.meta.get("relation") == "VERIFIED_BY" and edge.meta.get("node_type") == "command" for edge in edges)
    assert any(edge.to_id == "file:eimemory/governance/learning_state.py" for edge in edges)


def test_memory_graph_returns_evidence_paths_for_coding_query(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    observed = runtime.observe_coding_memory(_coding_observation(), scope=SCOPE)

    graph = runtime.query_coding_memory_graph("sqlite disk I/O error", scope=SCOPE, limit=5)
    fallback_limit_graph = runtime.query_coding_memory_graph("sqlite disk I/O error", scope=SCOPE, limit="bad")

    assert graph["ok"] is True
    assert graph["report_type"] == "coding_graph_query"
    assert graph["paths"]
    assert fallback_limit_graph["paths"]
    assert graph["evidence_refs"][0]["record_id"] == observed["record_id"]
    relations = {step["relation"] for path in graph["paths"] for step in path["steps"]}
    assert {"FAILED_WITH", "DECIDED_BECAUSE", "VERIFIED_BY"}.issubset(relations)


def test_graph_replay_gate_persists_pass_or_fail_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.observe_coding_memory(_coding_observation(), scope=SCOPE)

    passed = runtime.run_coding_graph_replay(
        query="sqlite disk I/O error",
        expected_relations=["FAILED_WITH", "DECIDED_BECAUSE", "VERIFIED_BY"],
        scope=SCOPE,
        persist=True,
    )
    failed = runtime.run_coding_graph_replay(
        query="sqlite disk I/O error",
        expected_relations=["ROLLED_BACK_BY"],
        scope=SCOPE,
        persist=False,
    )

    assert passed["ok"] is True
    assert passed["verdict"] == "pass"
    assert passed["pass_rate"] == 1.0
    assert passed["persisted_record_id"]
    persisted = runtime.store.get_by_id(passed["persisted_record_id"], scope=SCOPE)
    assert persisted is not None
    assert persisted.kind == "replay_result"
    assert persisted.meta["report_type"] == "coding_graph_replay"

    assert failed["ok"] is False
    assert failed["verdict"] == "fail"
    assert failed["missing_relations"] == ["ROLLED_BACK_BY"]
