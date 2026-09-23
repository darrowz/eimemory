"""Small evaluation exception hierarchy (fail-closed helpers).

Keep this shallow: most call sites still raise ``ValueError`` with stable
reason codes. Use these types when a typed catch/boundary is useful.
"""
from __future__ import annotations


class EvaluationError(Exception):
    """Base for evaluation-module failures."""

    def __init__(self, code: str = "evaluation_error", *args: object) -> None:
        self.code = str(code or "evaluation_error")
        super().__init__(self.code, *args)


class EvaluationDatasetError(EvaluationError, ValueError):
    """Dataset schema/bounds/authority failures."""


class EvaluationAuthorityError(EvaluationError, ValueError):
    """Label / dataset / catalog authority failures."""


class EvaluationCatalogError(EvaluationError, RuntimeError):
    """Capability catalog resolution / seal failures."""
