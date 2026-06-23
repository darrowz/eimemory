"""Tests for harness-patch v2 enforcement helpers in candidate_search.

See ``docs/superpowers/plans/2026-06-23-eimemory-1.6.0-harness-patch.md`` §Task 4.
"""
from __future__ import annotations

import pytest

from eimemory.governance.candidate_search import (
    MAX_DIFF_LINES,
    MAX_DIFF_TOKENS,
    MIN_DIVERSE_CANDIDATES,
    enforce_diff_size,
    enforce_diversity,
    enforce_one_active_per_surface,
)


def test_enforce_diff_size_rejects_oversized() -> None:
    with pytest.raises(ValueError, match="diff_lines"):
        enforce_diff_size(diff_lines=MAX_DIFF_LINES + 1, diff_tokens=100)


def test_enforce_diff_size_rejects_oversized_tokens() -> None:
    with pytest.raises(ValueError, match="diff_tokens"):
        enforce_diff_size(diff_lines=10, diff_tokens=MAX_DIFF_TOKENS + 1)


def test_enforce_diversity_rejects_single_cluster() -> None:
    candidates = [
        {"source_key": "k1", "diff_lines": 10},
        {"source_key": "k1", "diff_lines": 12},
    ]
    with pytest.raises(ValueError, match="diverse"):
        enforce_diversity(candidates, min_count=MIN_DIVERSE_CANDIDATES)


def test_enforce_one_active_per_surface_rejects_duplicate() -> None:
    active = [{"target_surface": "INSTRUCTION", "id": "a"}]
    with pytest.raises(ValueError, match="already active"):
        enforce_one_active_per_surface(
            new_surface="INSTRUCTION",
            active_surfaces=active,
        )