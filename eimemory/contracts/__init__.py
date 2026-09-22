"""Cross-plane shared contracts with no upward Data/Control deps.

ARCH-01: storage and models should import from here instead of capabilities /
governance / scoring / retrieval when only shared types/helpers are needed.
"""

from eimemory.contracts.outcome_evidence import outcome_evidence
from eimemory.contracts.receipts import MAX_ELIGIBLE_RECEIPTS_PER_RUN
from eimemory.contracts.release_identity import (
    ReleaseIdentity,
    release_authority_key,
    release_identity_payload,
    same_release_authority,
)

__all__ = [
    "MAX_ELIGIBLE_RECEIPTS_PER_RUN",
    "outcome_evidence",
    "ReleaseIdentity",
    "release_authority_key",
    "release_identity_payload",
    "same_release_authority",
]
