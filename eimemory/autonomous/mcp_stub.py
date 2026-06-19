"""MCP-shaped tool surface for the Karpathy loop."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from eimemory.autonomous.compounding import load_recent_kept


class UnknownToolError(Exception):
    """Raised when ``call_tool`` receives an unknown tool name."""


class MissingArgError(Exception):
    """Raised when a registered tool is missing required arguments."""


@dataclass(slots=True)
class _ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: Callable[..., dict[str, Any]] = field(repr=False)


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _propose_hypothesis(*, weaknesses: list, incidents: list, **_kwargs: Any) -> dict[str, Any]:
    signals = [str(item) for item in [*list(weaknesses), *list(incidents)] if str(item).strip()]
    bucket = _bucket_from_signals(signals)
    hypothesis = (
        f"Top failure bucket '{bucket}' appears {len(signals)} times in supplied signals. "
        "Reduce it with a focused retrieval, routing, or governance experiment."
    )
    return _ok(
        {
            "hypothesis": hypothesis,
            "bucket": bucket,
            "signal_count": len(signals),
            "weaknesses": list(weaknesses),
            "incidents": list(incidents),
        }
    )


def _run_experiment(*, hypothesis_id: str, **_kwargs: Any) -> dict[str, Any]:
    return _ok({"experiment_id": f"exp_{hypothesis_id}", "status": "ready"})


def _score_experiment(
    *,
    experiment_id: str,
    hit_at_1: float | None = None,
    baseline: float | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    resolved_baseline = float(0.0 if baseline is None else baseline)
    resolved_hit = float(resolved_baseline if hit_at_1 is None else hit_at_1)
    return _ok({"experiment_id": experiment_id, "hit_at_1": resolved_hit, "baseline": resolved_baseline})


def _keep_or_discard(*, experiment_id: str, hit_at_1: float, baseline: float, **_kwargs: Any) -> dict[str, Any]:
    decision = "keep" if hit_at_1 > baseline else "discard"
    return _ok({"experiment_id": experiment_id, "decision": decision, "delta": hit_at_1 - baseline})


def _get_compounding_context(
    *,
    limit: int = 5,
    exp_log_path: str | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    rows = load_recent_kept(Path(exp_log_path), n=limit) if exp_log_path else []
    return _ok({"kept_experiments": rows, "limit": limit})


def _bucket_from_signals(signals: list[str]) -> str:
    text = " ".join(signals).lower()
    if any(token in text for token in ("recall", "search", "hit@", "retriev", "longmemeval")):
        return "recall"
    if any(token in text for token in ("tool", "mcp", "function")):
        return "tooling"
    if any(token in text for token in ("govern", "policy", "approval", "permission")):
        return "governance"
    if any(token in text for token in ("timeout", "latency", "slow", "hang")):
        return "latency"
    if any(token in text for token in ("error", "exception", "fail", "crash")):
        return "runtime"
    return "general"


_TOOL_SPECS: dict[str, _ToolSpec] = {
    "propose_hypothesis": _ToolSpec(
        name="propose_hypothesis",
        description="Generate a hypothesis from weakness/incident records.",
        input_schema={
            "type": "object",
            "required": ["weaknesses", "incidents"],
            "properties": {
                "weaknesses": {"type": "array", "items": {"type": "string"}},
                "incidents": {"type": "array", "items": {"type": "string"}},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "hypothesis": {"type": "string"},
                "bucket": {"type": "string"},
                "signal_count": {"type": "integer"},
                "weaknesses": {"type": "array", "items": {"type": "string"}},
                "incidents": {"type": "array", "items": {"type": "string"}},
            },
        },
        handler=_propose_hypothesis,
    ),
    "run_experiment": _ToolSpec(
        name="run_experiment",
        description="Prepare a single karpathy-loop experiment for a hypothesis.",
        input_schema={
            "type": "object",
            "required": ["hypothesis_id"],
            "properties": {"hypothesis_id": {"type": "string"}},
        },
        output_schema={
            "type": "object",
            "properties": {"experiment_id": {"type": "string"}, "status": {"type": "string"}},
        },
        handler=_run_experiment,
    ),
    "score_experiment": _ToolSpec(
        name="score_experiment",
        description="Score a completed experiment's hit@1 against baseline.",
        input_schema={
            "type": "object",
            "required": ["experiment_id"],
            "properties": {
                "experiment_id": {"type": "string"},
                "hit_at_1": {"type": "number"},
                "baseline": {"type": "number"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "experiment_id": {"type": "string"},
                "hit_at_1": {"type": "number"},
                "baseline": {"type": "number"},
            },
        },
        handler=_score_experiment,
    ),
    "keep_or_discard": _ToolSpec(
        name="keep_or_discard",
        description="Decide if an experiment is kept or rolled back based on hit@1 vs baseline.",
        input_schema={
            "type": "object",
            "required": ["experiment_id", "hit_at_1", "baseline"],
            "properties": {
                "experiment_id": {"type": "string"},
                "hit_at_1": {"type": "number"},
                "baseline": {"type": "number"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "experiment_id": {"type": "string"},
                "decision": {"type": "string"},
                "delta": {"type": "number"},
            },
        },
        handler=_keep_or_discard,
    ),
    "get_compounding_context": _ToolSpec(
        name="get_compounding_context",
        description="Return the last N kept experiments for the next iteration's context.",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 5},
                "exp_log_path": {"type": "string"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {"kept_experiments": {"type": "array"}, "limit": {"type": "integer"}},
        },
        handler=_get_compounding_context,
    ),
}


class MCPLoopStub:
    """Small in-process dispatcher with MCP-like list/spec/call methods."""

    def list_tools(self) -> list[str]:
        return list(_TOOL_SPECS.keys())

    def get_tool_spec(self, name: str) -> dict[str, Any]:
        spec = self._lookup(name)
        return {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_schema,
            "output_schema": spec.output_schema,
        }

    def call_tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
        spec = self._lookup(name)
        self._validate_required(spec, kwargs)
        return spec.handler(**kwargs)

    @staticmethod
    def _lookup(name: str) -> _ToolSpec:
        if name not in _TOOL_SPECS:
            raise UnknownToolError(f"unknown tool: {name}")
        return _TOOL_SPECS[name]

    @staticmethod
    def _validate_required(spec: _ToolSpec, kwargs: dict[str, Any]) -> None:
        missing = [key for key in spec.input_schema.get("required", []) if key not in kwargs]
        if missing:
            raise MissingArgError(f"missing required args for {spec.name}: {missing}")
