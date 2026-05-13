from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Literal


type BridgeJSONValue = str | int | float | bool | None | list["BridgeJSONValue"] | dict[str, "BridgeJSONValue"]
type BridgeScope = dict[str, str]
type BridgePayload = dict[str, Any]
type BridgeCommandDict = dict[str, BridgeJSONValue]
type BridgeResultDict = dict[str, BridgeJSONValue]
type BridgeEventDict = dict[str, BridgeJSONValue]
type EIMemoryRPCMethod = Literal[
    "memory.recall",
    "memory.ingest",
    "evolution.observe",
    "experience.record_skill_trace",
    "experience.record_item",
    "evolution.get_active_policy",
]
type EIMemoryRPCRequest = dict[str, Any]
type EIMemoryRPCResponse = dict[str, Any]


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


@dataclass(frozen=True)
class BridgeSource:
    source_id: str
    source_type: str
    channel: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "channel": self.channel,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BridgeSource":
        return cls(
            source_id=str(data.get("source_id", "")),
            source_type=str(data.get("source_type", "")),
            channel=data.get("channel"),
            metadata=_mapping(data.get("metadata")),
        )


@dataclass(frozen=True)
class BridgeTarget:
    agent_id: str | None = None
    capability: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "capability": self.capability,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BridgeTarget":
        return cls(
            agent_id=data.get("agent_id"),
            capability=data.get("capability"),
            metadata=_mapping(data.get("metadata")),
        )


@dataclass(frozen=True)
class BridgeCommand:
    command_id: str
    source: BridgeSource
    target: BridgeTarget
    intent: str
    params: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    created_at_ts: float = field(default_factory=time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "source": self.source.to_dict(),
            "target": self.target.to_dict(),
            "intent": self.intent,
            "params": dict(self.params),
            "policy": dict(self.policy),
            "created_at_ts": self.created_at_ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BridgeCommand":
        return cls(
            command_id=str(data.get("command_id", "")),
            source=BridgeSource.from_dict(_mapping(data.get("source"))),
            target=BridgeTarget.from_dict(_mapping(data.get("target"))),
            intent=str(data.get("intent", "")),
            params=_mapping(data.get("params")),
            policy=_mapping(data.get("policy")),
            created_at_ts=float(data.get("created_at_ts", time())),
        )


@dataclass(frozen=True)
class BridgeResult:
    ok: bool
    command_id: str
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "command_id": self.command_id,
            "summary": self.summary,
            "payload": dict(self.payload),
            "error": self.error,
            "audit": dict(self.audit),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BridgeResult":
        return cls(
            ok=bool(data.get("ok", False)),
            command_id=str(data.get("command_id", "")),
            summary=str(data.get("summary", "")),
            payload=_mapping(data.get("payload")),
            error=data.get("error"),
            audit=_mapping(data.get("audit")),
        )


@dataclass(frozen=True)
class BridgeEvent:
    event_id: str
    agent_id: str
    event_type: str
    summary: str
    importance: float = 0.0
    should_notify: bool = False
    memory_ref: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "agent_id": self.agent_id,
            "event_type": self.event_type,
            "summary": self.summary,
            "importance": self.importance,
            "should_notify": self.should_notify,
            "memory_ref": dict(self.memory_ref) if self.memory_ref is not None else None,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BridgeEvent":
        memory_ref = data.get("memory_ref")
        return cls(
            event_id=str(data.get("event_id", "")),
            agent_id=str(data.get("agent_id", "")),
            event_type=str(data.get("event_type", "")),
            summary=str(data.get("summary", "")),
            importance=float(data.get("importance", 0.0)),
            should_notify=bool(data.get("should_notify", False)),
            memory_ref=_mapping(memory_ref) if memory_ref is not None else None,
            payload=_mapping(data.get("payload")),
        )
