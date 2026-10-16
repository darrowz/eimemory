"""Resolve controlled benchmark authority from caller scope and private pins."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import Any

from eimemory.adapters.runtime.channel import (
    resolve_channel_scope, runtime_channel_from_scope,
)
from eimemory.adapters.runtime.host_auth import _read_private_file
from eimemory.adapters.runtime.sources import runtime_source_ids
from eimemory.evaluation.production_query_dataset import resolve_bound_logical_scope
from eimemory.governance.evidence_contract import _pin_scope_matches
from eimemory.models.records import ScopeRef

_SCOPE_FIELDS = {"tenant_id", "agent_id", "workspace_id", "user_id"}
_MARKER = "::channel::"


def resolve_memory_benchmark_context(*, scope: dict | ScopeRef) -> dict[str, Any]:
    """Never derive permission from stored records or an installed client.

    An explicitly channel-qualified caller keeps that channel. An unqualified
    request needs exactly one matching channel in the operator's private
    release-scope binding file. Logical user translation uses the existing
    binding resolver and must be supported by the selected channel's binding.
    """
    requested = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    requested_payload = asdict(requested)
    requested_channel = runtime_channel_from_scope(requested)
    if (_MARKER in requested.workspace_id
            and (not requested_channel or requested.workspace_id.count(_MARKER) != 1)):
        return _blocked("memory_benchmark_channel_scope_invalid")
    logical_scope, logical_resolution = resolve_bound_logical_scope(requested)
    logical = ScopeRef.from_dict(logical_scope)
    resolution = {
        "logical_scope_reason": str(logical_resolution.get("reason") or ""),
        "logical_user_rewritten": logical_resolution.get("rewritten") is True,
        "channel_source": "requested_suffix" if requested_channel else "operator_binding",
    }
    bindings, bindings_status = _bindings()
    channels = _matching_channels(bindings, logical)
    resolution["matching_channel_count"] = len(channels)
    if requested_channel:
        if resolution["logical_user_rewritten"] and requested_channel not in channels:
            return _blocked("memory_benchmark_logical_user_channel_binding_missing", resolution)
        # An explicit logical request is already authority for its exact channel;
        # a binding to a different channel cannot redirect it.
        selected = requested_channel
    else:
        if bindings_status != "available":
            return _blocked("memory_benchmark_bindings_" + bindings_status, resolution)
        if len(channels) != 1:
            return _blocked("memory_benchmark_channel_binding_ambiguous" if channels
                            else "memory_benchmark_channel_binding_missing", resolution)
        selected = next(iter(channels))
    try:
        sources = runtime_source_ids(selected)
    except ValueError:
        return _blocked("memory_benchmark_source_ids_invalid", resolution)
    exact = resolve_channel_scope(selected, logical_scope if resolution["logical_user_rewritten"]
                                  else requested_payload)
    return {"ok": True, "reason": "", "channel": selected, "scope": exact,
            "source_ids": sources, "resolution": resolution}


def _bindings() -> tuple[list[dict[str, Any]], str]:
    path = os.environ.get("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", "").strip()
    if not path:
        return [], "missing"
    try:
        raw = _read_private_file(Path(path), max_bytes=64 * 1024)
        data = json.loads(raw) if raw else None
    except (OSError, ValueError, UnicodeError):
        return [], "unreadable"
    return (data, "available") if isinstance(data, list) else ([], "unreadable")


def _matching_channels(bindings: list[dict[str, Any]], requested: ScopeRef) -> set[str]:
    wanted_base = requested.workspace_id.split(_MARKER, 1)[0]
    channels = set()
    for binding in bindings:
        value = binding.get("scope") if isinstance(binding, dict) else None
        if (not isinstance(value, dict) or set(value) != _SCOPE_FIELDS
                or not all(isinstance(item, str) and item for item in value.values())):
            continue
        try:
            bound = ScopeRef.from_dict(value)
        except ValueError:
            continue
        channel = runtime_channel_from_scope(bound)
        if _MARKER in bound.workspace_id and (not channel or bound.workspace_id.count(_MARKER) != 1):
            continue
        channel = channel or "openclaw"
        target = ScopeRef.from_dict(resolve_channel_scope(
            channel, {**asdict(requested), "workspace_id": wanted_base}))
        if _pin_scope_matches(value, target):
            channels.add(channel)
    return channels


def _blocked(reason: str, resolution: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "channel": "", "scope": {}, "source_ids": [],
            "resolution": resolution or {}}
