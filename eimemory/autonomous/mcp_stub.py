"""MCP server stub for the karpathy loop tool surface.

The karpathy autoresearch loop (Phase 2) and its cross-capability
extensions (Phase 3) want a stable way to call into the loop from
multiple call sites: the local LLM, the cron-driven ``loop.py``
runner, and any future HTTP wrapper. This module defines a minimal
server stub shaped like Anthropic's Model Context Protocol (MCP)
``tools/list`` + ``tools/call`` surface so all of those callers can
be wired against the same dispatch and schema.

The handlers in this file are deliberately tiny — they do not touch
the audit log, the circuit breaker, or any state-mutating path. Real
implementations are filled in by later Phase 2 / Phase 3 work
(``hypothesis.py``, ``loop.py``, ``capability_discovery.py``,
``business_feedback.py``). The point of shipping the stub now is
to lock the contract: tool names, input/output schemas, and the
``ok`` / ``data`` response shape, so the loop runner, the
``seven_day_review`` bot, and any external integration can be
written against it without waiting on the real handlers.

Reference: ``docs/superpowers/specs/2026-06-17-eimemory-karpathy-loop-design.md`` §Phase 3.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class UnknownToolError(Exception):
    """Raised when :meth:`MCPLoopStub.call_tool` is called with an unknown tool name."""


class MissingArgError(Exception):
    """Raised when :meth:`MCPLoopStub.call_tool` is called without a required argument."""


@dataclass(slots=True)
class _ToolSpec:
    """Internal description of one tool the stub exposes.

    The dataclass is private (``_ToolSpec``); the public surface is
    the dict returned by :meth:`MCPLoopStub.get_tool_spec`, which is
    the shape an MCP client expects.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: Callable[..., dict[str, Any]] = field(repr=False)


def _ok(data: Any) -> dict[str, Any]:
    """Wrap a handler's return value in the standard ``{ok, data}`` envelope."""
    return {"ok": True, "data": data}


# --- Stub handlers ----------------------------------------------------------
#
# Each handler is intentionally minimal: it returns a dict that matches
# the ``output_schema`` declared in its ``_ToolSpec`` so callers can
# rely on the shape. Real logic lands in Phase 2 / Phase 3.


def _stub_propose_hypothesis(*, weaknesses: list, incidents: list, **_kwargs: Any) -> dict[str, Any]:
    """Stub: surface a placeholder hypothesis from weakness/incident inputs."""
    return _ok({
        "hypothesis": "stub: see Phase 2 hypothesis.py",
        "weaknesses": list(weaknesses),
        "incidents": list(incidents),
    })


def _stub_run_experiment(*, hypothesis_id: str, **_kwargs: Any) -> dict[str, Any]:
    """Stub: schedule an experiment for the given hypothesis_id."""
    return _ok({"experiment_id": f"exp_{hypothesis_id}", "status": "scheduled"})


def _stub_score_experiment(*, experiment_id: str, **_kwargs: Any) -> dict[str, Any]:
    """Stub: report a null hit@1 — real score lands when eval plumbing exists."""
    return _ok({"experiment_id": experiment_id, "hit_at_1": None, "baseline": None})


def _stub_keep_or_discard(*, experiment_id: str, hit_at_1: float, baseline: float, **_kwargs: Any) -> dict[str, Any]:
    """Decide keep vs discard purely from hit@1 vs baseline.

    A real implementation also reads the audit log, the anomaly
    detector, and the 7-day review deltas; this stub is the
    arithmetic core that all of those layers will reduce to.
    """
    decision = "keep" if hit_at_1 > baseline else "discard"
    return _ok({
        "experiment_id": experiment_id,
        "decision": decision,
        "delta": hit_at_1 - baseline,
    })


def _stub_get_compounding_context(*, limit: int = 5, **_kwargs: Any) -> dict[str, Any]:
    """Stub: return an empty list of kept experiments with the requested cap."""
    return _ok({"kept_experiments": [], "limit": limit})


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
                "weaknesses": {"type": "array", "items": {"type": "string"}},
                "incidents": {"type": "array", "items": {"type": "string"}},
            },
        },
        handler=_stub_propose_hypothesis,
    ),
    "run_experiment": _ToolSpec(
        name="run_experiment",
        description="Schedule a single karpathy-loop experiment for a hypothesis.",
        input_schema={
            "type": "object",
            "required": ["hypothesis_id"],
            "properties": {
                "hypothesis_id": {"type": "string"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "experiment_id": {"type": "string"},
                "status": {"type": "string"},
            },
        },
        handler=_stub_run_experiment,
    ),
    "score_experiment": _ToolSpec(
        name="score_experiment",
        description="Score a completed experiment's hit@1 against the held-out baseline.",
        input_schema={
            "type": "object",
            "required": ["experiment_id"],
            "properties": {
                "experiment_id": {"type": "string"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "experiment_id": {"type": "string"},
                "hit_at_1": {"type": ["number", "null"]},
                "baseline": {"type": ["number", "null"]},
            },
        },
        handler=_stub_score_experiment,
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
        handler=_stub_keep_or_discard,
    ),
    "get_compounding_context": _ToolSpec(
        name="get_compounding_context",
        description="Return the last N kept experiments for the next iteration's context.",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 5},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "kept_experiments": {"type": "array"},
                "limit": {"type": "integer"},
            },
        },
        handler=_stub_get_compounding_context,
    ),
}


class MCPLoopStub:
    """MCP-shaped server stub exposing the karpathy loop tool surface.

    The class is intentionally small: it owns a fixed registry of
    tools (``_TOOL_SPECS``) and three dispatch methods. No I/O, no
    state, no thread safety — those are concerns of the real
    transport layer (Phase 4's eiskills bridge or an HTTP wrapper)
    that will sit on top of this stub.
    """

    def list_tools(self) -> list[str]:
        """Return the names of every tool the stub exposes.

        Order is the registration order in :data:`_TOOL_SPECS`. Callers
        should treat the result as a set, not a sequence.
        """
        return list(_TOOL_SPECS.keys())

    def get_tool_spec(self, name: str) -> dict[str, Any]:
        """Return the schema for one tool as a plain dict.

        Raises:
            UnknownToolError: If ``name`` is not in the registry.
        """
        spec = self._lookup(name)
        return {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_schema,
            "output_schema": spec.output_schema,
        }

    def call_tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
        """Dispatch a call to the named tool and return its result envelope.

        Args:
            name: Tool name; must appear in :meth:`list_tools`.
            **kwargs: Tool arguments. Required arguments are declared
                in the tool's ``input_schema.required``; missing them
                raises :class:`MissingArgError` before the handler runs.

        Returns:
            ``{"ok": True, "data": <handler-returned-dict>}`` on success.

        Raises:
            UnknownToolError: ``name`` is not a registered tool.
            MissingArgError: A required argument for ``name`` is absent.
        """
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
        required = spec.input_schema.get("required", [])
        missing = [key for key in required if key not in kwargs]
        if missing:
            raise MissingArgError(f"missing required args for {spec.name}: {missing}")
