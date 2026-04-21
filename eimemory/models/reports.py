from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PolicyReport:
    retrieval_policy: dict[str, Any] = field(default_factory=dict)
    response_policy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieval_policy": dict(self.retrieval_policy),
            "response_policy": dict(self.response_policy),
        }
