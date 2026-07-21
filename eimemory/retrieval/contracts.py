from __future__ import annotations

from dataclasses import dataclass, field
from itertools import islice
from typing import Any, Mapping, Protocol, runtime_checkable

from eimemory.models.records import RecallBundle, ScopeRef
from eimemory.models.source_partitions import normalize_source_id, normalize_source_ids


@dataclass(frozen=True, slots=True)
class RecallPipelineSnapshot:
    search_limit: int
    raw_hybrid: bool
    recall_profile: str
    recall_profile_source: str
    recall_intent_name: str
    graph_depth: int
    query_scope_count: int
    report_query: bool
    operational_recall_allowed: bool


@dataclass(frozen=True, slots=True)
class _FrozenMapping:
    items: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _FrozenSequence:
    items: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _FrozenSet:
    items: tuple[Any, ...]


def freeze_value(value: Any) -> Any:
    if isinstance(value, (_FrozenMapping, _FrozenSequence, _FrozenSet)):
        return value
    if isinstance(value, Mapping):
        return _FrozenMapping(tuple((str(key), freeze_value(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return _FrozenSequence(tuple(freeze_value(item) for item in value))
    if isinstance(value, set):
        return _FrozenSet(tuple(sorted((freeze_value(item) for item in value), key=repr)))
    return value


def thaw_value(value: Any) -> Any:
    if isinstance(value, _FrozenMapping):
        return {key: thaw_value(item) for key, item in value.items}
    if isinstance(value, _FrozenSequence):
        return [thaw_value(item) for item in value.items]
    if isinstance(value, _FrozenSet):
        return {thaw_value(item) for item in value.items}
    if isinstance(value, tuple):
        return {key: thaw_value(item) for key, item in value}
    return value


@dataclass(frozen=True, slots=True)
class ExactScope:
    tenant_id: str = "default"
    agent_id: str = ""
    workspace_id: str = ""
    user_id: str = ""

    @classmethod
    def from_scope(cls, scope: ScopeRef | Mapping[str, Any] | None) -> "ExactScope":
        ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope or {}))
        return cls(
            tenant_id=str(ref.tenant_id or "default"),
            agent_id=str(ref.agent_id or ""),
            workspace_id=str(ref.workspace_id or ""),
            user_id=str(ref.user_id or ""),
        )

    def to_scope_ref(self) -> ScopeRef:
        return ScopeRef(
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            workspace_id=self.workspace_id,
            user_id=self.user_id,
        )


@dataclass(frozen=True, slots=True)
class CandidateRequest:
    query: str
    scope: ExactScope
    kinds: tuple[str, ...] = ()
    source_ids: tuple[str, ...] | None = None
    limit: int = 8
    budget: int = 24
    recall_filters: tuple[tuple[str, Any], ...] = ()
    task_context: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", str(self.query or "").strip())
        object.__setattr__(self, "kinds", tuple(str(kind).strip() for kind in self.kinds if str(kind).strip()))
        normalized_sources = normalize_source_ids(self.source_ids)
        object.__setattr__(self, "source_ids", normalized_sources)
        bounded_limit = max(0, min(1000, int(self.limit)))
        object.__setattr__(self, "limit", bounded_limit)
        object.__setattr__(self, "budget", max(bounded_limit, min(5000, int(self.budget))))
        object.__setattr__(self, "recall_filters", _freeze_pairs(self.recall_filters))
        object.__setattr__(self, "task_context", _freeze_pairs(self.task_context))

    @classmethod
    def create(
        cls,
        *,
        query: str,
        scope: ScopeRef | Mapping[str, Any] | None,
        kinds: tuple[str, ...] | list[str] = (),
        source_ids: tuple[str, ...] | list[str] | None = None,
        limit: int = 8,
        budget: int | None = None,
        recall_filters: Mapping[str, Any] | None = None,
        task_context: Mapping[str, Any] | None = None,
    ) -> "CandidateRequest":
        bounded_limit = max(0, min(1000, int(limit)))
        return cls(
            query=query,
            scope=ExactScope.from_scope(scope),
            kinds=tuple(kinds or ()),
            source_ids=None if source_ids is None else tuple(source_ids),
            limit=bounded_limit,
            budget=max(bounded_limit, bounded_limit * 3) if budget is None else budget,
            recall_filters=freeze_value(dict(recall_filters or {})),
            task_context=freeze_value(dict(task_context or {})),
        )

    def recall_filter_dict(self) -> dict[str, Any]:
        return dict(thaw_value(self.recall_filters))

    def task_context_dict(self) -> dict[str, Any]:
        return dict(thaw_value(self.task_context))


@dataclass(frozen=True, slots=True)
class CandidateRef:
    record_id: str
    scope: ExactScope
    source_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_id", str(self.record_id or "").strip())
        object.__setattr__(self, "source_id", normalize_source_id(self.source_id))


@dataclass(frozen=True, slots=True)
class CandidateHit:
    ref: CandidateRef
    source_rank: int
    source_score: float
    component_hints: tuple[tuple[str, Any], ...] = ()
    evidence_hints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_rank", max(1, int(self.source_rank)))
        object.__setattr__(self, "source_score", float(self.source_score))
        object.__setattr__(self, "component_hints", _freeze_bounded_pairs(self.component_hints, max_items=32))
        object.__setattr__(
            self,
            "evidence_hints",
            tuple(
                normalized
                for item in islice(iter(self.evidence_hints), 32)
                if (normalized := str(item).strip()[:128])
            ),
        )

    def component_dict(self) -> dict[str, Any]:
        return dict(thaw_value(self.component_hints))


@dataclass(frozen=True, slots=True)
class CandidateBatch:
    hits: tuple[CandidateHit, ...] = ()
    diagnostics: tuple[tuple[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "hits", tuple(islice(iter(self.hits), 5000)))
        object.__setattr__(self, "diagnostics", _freeze_bounded_pairs(self.diagnostics, max_items=12))

    def diagnostic_dict(self) -> dict[str, Any]:
        return dict(thaw_value(self.diagnostics))


@runtime_checkable
class CandidateSource(Protocol):
    name: str

    def search(self, request: CandidateRequest) -> CandidateBatch: ...


@runtime_checkable
class RecallEngine(Protocol):
    def recall(self, request: CandidateRequest) -> RecallBundle: ...


def _freeze_pairs(value: Any, *, max_items: int | None = None) -> tuple[tuple[str, Any], ...]:
    if isinstance(value, _FrozenMapping):
        pairs = list(value.items)
    elif isinstance(value, Mapping):
        pairs = list(value.items())
    else:
        pairs = list(value or ())
    if max_items is not None:
        pairs = pairs[:max_items]
    return tuple((str(key), freeze_value(item)) for key, item in pairs)


def _freeze_bounded_pairs(value: Any, *, max_items: int) -> tuple[tuple[str, Any], ...]:
    if isinstance(value, _FrozenMapping):
        pairs = islice(value.items, max_items)
    elif isinstance(value, Mapping):
        pairs = islice(value.items(), max_items)
    else:
        pairs = islice(iter(value or ()), max_items)
    return tuple((str(key)[:80], _freeze_bounded(item, depth=0)) for key, item in pairs)


def _freeze_bounded(value: Any, *, depth: int) -> Any:
    if depth >= 4:
        return "<truncated>"
    if isinstance(value, _FrozenMapping):
        value = dict(islice(value.items, 16))
    elif isinstance(value, (_FrozenSequence, _FrozenSet)):
        value = list(islice(value.items, 32))
    if isinstance(value, Mapping):
        return _FrozenMapping(
            tuple(
                (str(key)[:80], _freeze_bounded(item, depth=depth + 1))
                for key, item in islice(value.items(), 16)
            )
        )
    if isinstance(value, (list, tuple)):
        return _FrozenSequence(tuple(_freeze_bounded(item, depth=depth + 1) for item in value[:32]))
    if isinstance(value, set):
        bounded = list(islice(iter(value), 32))
        bounded.sort(key=lambda item: (type(item).__name__, str(item)[:80]))
        return _FrozenSet(tuple(_freeze_bounded(item, depth=depth + 1) for item in bounded))
    if isinstance(value, str):
        return value[:512]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"
