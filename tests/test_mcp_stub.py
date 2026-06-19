"""Tests for the karpathy-loop MCP server stub.

Mirrors `eimemory/autonomous/mcp_stub.py`. RED-GREEN TDD per
`docs/superpowers/plans/2026-06-17-eimemory-karpathy-loop.md` Task 3.4.

The stub exposes a small tool surface (propose_hypothesis,
run_experiment, score_experiment, keep_or_discard,
get_compounding_context) shaped like a Model Context Protocol (MCP)
``tools/list`` + ``tools/call`` interface. The real handlers are
filled in by later Phase 3 / Phase 4 work; this module just defines
the dispatch and schema so callers (a local LLM, the loop runner,
or a future HTTP wrapper) can be wired without waiting on those
handlers.
"""
from __future__ import annotations

import pytest

from eimemory.autonomous.mcp_stub import (
    MCPLoopStub,
    MissingArgError,
    UnknownToolError,
)


# Tools the karpathy loop exposes through the stub. Names are part of
# the public contract; if a tool is renamed, callers break. Keep this
# list in sync with the implementation.
EXPECTED_TOOLS = {
    "propose_hypothesis",
    "run_experiment",
    "score_experiment",
    "keep_or_discard",
    "get_compounding_context",
}


def test_list_tools_returns_known_karpathy_loop_tools() -> None:
    """``list_tools`` must return the canonical karpathy-loop tool names."""
    stub = MCPLoopStub()
    tools = set(stub.list_tools())
    assert EXPECTED_TOOLS.issubset(tools), f"missing tools: {EXPECTED_TOOLS - tools}"


def test_get_tool_spec_returns_schema_with_required_fields() -> None:
    """``get_tool_spec`` must return a schema dict with the four required keys."""
    stub = MCPLoopStub()
    spec = stub.get_tool_spec("propose_hypothesis")
    assert spec["name"] == "propose_hypothesis"
    assert isinstance(spec["description"], str) and spec["description"]
    assert "input_schema" in spec
    assert "output_schema" in spec
    # The required fields must be a list (possibly empty)
    assert isinstance(spec["input_schema"].get("required", []), list)


def test_call_tool_propose_hypothesis_dispatches() -> None:
    """``call_tool`` must dispatch to the propose_hypothesis handler and return ok."""
    stub = MCPLoopStub()
    result = stub.call_tool(
        "propose_hypothesis",
        weaknesses=["weak_query_too_broad"],
        incidents=["recall_drop_2026_06"],
    )
    assert result["ok"] is True
    assert "hypothesis" in result["data"]
    assert result["data"]["weaknesses"] == ["weak_query_too_broad"]
    assert result["data"]["incidents"] == ["recall_drop_2026_06"]


def test_call_tool_run_experiment_requires_hypothesis_id() -> None:
    """``run_experiment`` must require hypothesis_id and return a concrete response."""
    stub = MCPLoopStub()
    result = stub.call_tool("run_experiment", hypothesis_id="hyp_001")
    assert result["ok"] is True
    assert result["data"]["experiment_id"] == "exp_hyp_001"
    assert result["data"]["status"] == "ready"


def test_call_tool_keep_or_discard_keeps_when_hit_at_1_above_baseline() -> None:
    """``keep_or_discard`` returns keep when hit@1 > baseline, discard otherwise."""
    stub = MCPLoopStub()
    result = stub.call_tool(
        "keep_or_discard",
        experiment_id="exp_001",
        hit_at_1=0.62,
        baseline=0.55,
    )
    assert result["ok"] is True
    assert result["data"]["decision"] == "keep"
    assert result["data"]["delta"] == pytest.approx(0.07)


def test_call_tool_keep_or_discard_discards_when_hit_at_1_below_baseline() -> None:
    """``keep_or_discard`` returns discard when hit@1 <= baseline."""
    stub = MCPLoopStub()
    result = stub.call_tool(
        "keep_or_discard",
        experiment_id="exp_002",
        hit_at_1=0.50,
        baseline=0.55,
    )
    assert result["ok"] is True
    assert result["data"]["decision"] == "discard"
    assert result["data"]["delta"] == pytest.approx(-0.05)


def test_call_tool_unknown_raises_unknown_tool_error() -> None:
    """``call_tool`` must raise :class:`UnknownToolError` for an unknown name."""
    stub = MCPLoopStub()
    with pytest.raises(UnknownToolError) as excinfo:
        stub.call_tool("nonexistent_tool", foo="bar")
    assert "nonexistent_tool" in str(excinfo.value)


def test_call_tool_missing_required_arg_raises_missing_arg_error() -> None:
    """``call_tool`` must raise :class:`MissingArgError` when a required arg is missing."""
    stub = MCPLoopStub()
    # ``run_experiment`` requires ``hypothesis_id``; calling without it must fail.
    with pytest.raises(MissingArgError) as excinfo:
        stub.call_tool("run_experiment")
    assert "hypothesis_id" in str(excinfo.value)


def test_mcp_stub_no_longer_returns_placeholder_stub_text() -> None:
    stub = MCPLoopStub()
    result = stub.call_tool(
        "propose_hypothesis",
        weaknesses=["LongMemEval recall miss on turn localization"],
        incidents=["R@1 dropped after session-only retrieval"],
    )

    assert result["ok"] is True
    assert "stub:" not in result["data"]["hypothesis"].lower()
    assert "recall" in result["data"]["hypothesis"].lower()


def test_call_tool_score_experiment_returns_numeric_metrics() -> None:
    stub = MCPLoopStub()
    result = stub.call_tool("score_experiment", experiment_id="exp_001")

    assert result["ok"] is True
    assert isinstance(result["data"]["hit_at_1"], float)
    assert isinstance(result["data"]["baseline"], float)
