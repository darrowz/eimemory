from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Mapping, Sequence

from eimemory.models.records import RecordEnvelope


FUSION_POLICY_VERSION = "governed-rrf.v1"
DEFAULT_RRF_K = 60
MAX_RRF_K = 1000
MAX_FUSION_LIMIT = 1000
MAX_COMPONENT_ITEMS = 5000

SUPPORTED_COMPONENTS = frozenset(
    {
        "exact_title",
        "exact_alias",
        "keyword",
        "vector",
        "graph",
        "living",
        "usage",
    }
)
DEFAULT_COMPONENT_WEIGHTS: Mapping[str, float] = {
    "exact_title": 4.0,
    "exact_alias": 4.0,
    "keyword": 2.0,
    "vector": 1.5,
    "graph": 1.0,
    "living": 0.75,
    "usage": 0.5,
}


@dataclass(frozen=True, slots=True)
class FusionItem:
    record_id: str
    score: float
    ranks: dict[str, int]
    contributions: dict[str, float]


@dataclass(frozen=True, slots=True)
class FusionResult:
    policy_version: str
    rrf_k: int
    limit: int
    weights: dict[str, float]
    items: tuple[FusionItem, ...]


def fuse_ranked_components(
    components: Iterable[tuple[str, Sequence[str]]],
    *,
    weights: Mapping[str, object] | None = None,
    rrf_k: object = DEFAULT_RRF_K,
    limit: object = MAX_FUSION_LIMIT,
) -> FusionResult:
    """Fuse bounded ordered ID lists with deterministic weighted RRF."""

    normalized_k = _bounded_int(rrf_k, default=DEFAULT_RRF_K, minimum=1, maximum=MAX_RRF_K)
    normalized_limit = _bounded_int(limit, default=MAX_FUSION_LIMIT, minimum=0, maximum=MAX_FUSION_LIMIT)
    configured_weights = dict(DEFAULT_COMPONENT_WEIGHTS)
    configured_input = weights if isinstance(weights, Mapping) else {}
    for name, value in configured_input.items():
        component = str(name or "").strip()
        if component not in SUPPORTED_COMPONENTS:
            continue
        configured_weights[component] = _bounded_weight(value, default=configured_weights[component])

    ranks_by_id: dict[str, dict[str, int]] = {}
    contributions_by_id: dict[str, dict[str, float]] = {}
    seen_components: set[str] = set()
    for raw_name, ordered_ids in components:
        component = str(raw_name or "").strip()
        if component not in SUPPORTED_COMPONENTS:
            raise ValueError(f"unsupported fusion component: {component}")
        if component in seen_components:
            raise ValueError(f"duplicate fusion component: {component}")
        seen_components.add(component)
        weight = configured_weights[component]
        if weight <= 0:
            continue
        seen_ids: set[str] = set()
        rank = 0
        for raw_record_id in ordered_ids[:MAX_COMPONENT_ITEMS]:
            record_id = str(raw_record_id or "").strip()[:256]
            if not record_id or record_id in seen_ids:
                continue
            seen_ids.add(record_id)
            rank += 1
            ranks_by_id.setdefault(record_id, {})[component] = rank
            contributions_by_id.setdefault(record_id, {})[component] = weight / (normalized_k + rank)

    items = [
        FusionItem(
            record_id=record_id,
            score=sum(contributions.values()),
            ranks=dict(sorted(ranks_by_id[record_id].items())),
            contributions=dict(sorted(contributions.items())),
        )
        for record_id, contributions in contributions_by_id.items()
    ]
    items.sort(key=lambda item: (-item.score, item.record_id))
    return FusionResult(
        policy_version=FUSION_POLICY_VERSION,
        rrf_k=normalized_k,
        limit=normalized_limit,
        weights={name: configured_weights[name] for name in sorted(seen_components)},
        items=tuple(items[:normalized_limit]),
    )


def page_pool_key(record: RecordEnvelope) -> str:
    scope = record.scope
    namespace = "\x1f".join(
        (
            str(scope.tenant_id or "default")[:256],
            str(scope.agent_id or "")[:256],
            str(scope.workspace_id or "")[:256],
            str(scope.user_id or "")[:256],
            str(record.source_id or "default")[:256],
        )
    )
    content = record.content if isinstance(record.content, dict) else {}
    meta = record.meta if isinstance(record.meta, dict) else {}
    for label, keys in (
        ("page", ("page_id",)),
        ("parent", ("parent_record_id",)),
        ("document", ("source_document_id", "document_id", "doc_id")),
    ):
        value = _first_identifier(content, meta, keys=keys)
        if value:
            return f"{namespace}\x1f{label}:{value}"
    session_id = _first_identifier(content, meta, keys=("session_id",))
    source_event_id = _first_identifier(content, meta, keys=("source_event_id",))
    if session_id and source_event_id:
        return f"{namespace}\x1fraw:{session_id}:{source_event_id}"
    return f"{namespace}\x1frecord:{str(record.record_id or '')[:256]}"


def _first_identifier(content: Mapping[str, object], meta: Mapping[str, object], *, keys: tuple[str, ...]) -> str:
    for container in (content, meta):
        for key in keys:
            value = " ".join(str(container.get(key) or "").split())[:256]
            if value:
                return value
    return ""


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bounded_weight(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if not isfinite(parsed):
        parsed = default
    return max(0.0, min(10.0, parsed))
