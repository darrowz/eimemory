"""Separate replay evidence from verified production outcomes.

Implementation lives in ``eimemory.contracts.outcome_evidence`` so the Data plane
can import it without depending on governance (ARCH-01).
"""
from eimemory.contracts.outcome_evidence import (  # noqa: F401
    outcome_evidence,
    _verified_failure,
    _unexecuted_verification_state,
)

__all__ = ["outcome_evidence"]
