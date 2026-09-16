from __future__ import annotations
# EXT-12: outcome fallback must be scoped, not full-table

from .bridge import record_experience_item, record_skill_trace
from .outcome import record_outcome_trace

__all__ = ["record_experience_item", "record_outcome_trace", "record_skill_trace"]
