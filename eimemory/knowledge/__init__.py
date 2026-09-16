from __future__ import annotations
# EXT-06 FIXED: keyed reconcile helper marks incomplete when capped

from typing import Any, Iterable

from eimemory.knowledge.extract import PaperMemoryExtraction, extract_paper_memory

__all__ = ["PaperMemoryExtraction", "extract_paper_memory", "reconcile_knowledge_sets"]


def reconcile_knowledge_sets(
    left: Iterable[Any],
    right: Iterable[Any],
    *,
    key=lambda item: getattr(item, "record_id", None) or str(item),
    limit: int = 1000,
) -> dict[str, Any]:
    """EXT-06: O(n) keyed reconcile; never silently treat a capped scan as complete."""
    left_map: dict[str, Any] = {}
    right_map: dict[str, Any] = {}
    incomplete = False
    for index, item in enumerate(left):
        if index >= limit:
            incomplete = True
            break
        left_map[str(key(item))] = item
    for index, item in enumerate(right):
        if index >= limit:
            incomplete = True
            break
        right_map[str(key(item))] = item
    only_left = sorted(set(left_map) - set(right_map))
    only_right = sorted(set(right_map) - set(left_map))
    both = sorted(set(left_map) & set(right_map))
    return {
        "ok": not incomplete,
        "incomplete": incomplete,
        "matched_count": len(both),
        "only_left": only_left[:100],
        "only_right": only_right[:100],
        "limit": limit,
    }
