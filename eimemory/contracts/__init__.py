"""Cross-plane shared contracts with no upward Data/Control deps.

ARCH-01: storage and models should import from here instead of capabilities /
governance / scoring when only constants or pure helpers are needed.
"""

from eimemory.contracts.receipts import MAX_ELIGIBLE_RECEIPTS_PER_RUN
from eimemory.contracts.outcome_evidence import outcome_evidence

__all__ = [
    "MAX_ELIGIBLE_RECEIPTS_PER_RUN",
    "outcome_evidence",
]
