from __future__ import annotations

import pytest

from eimemory.contracts.capability_validators import (
    CapabilityContractError,
    MAX_INT_DIGITS,
    _normalize_json_value,
)
from eimemory.evaluation.reward import RewardEngine


def test_normalize_json_rejects_oversized_int() -> None:
    with pytest.raises(CapabilityContractError, match="integer exceeds"):
        _normalize_json_value(
            10 ** (MAX_INT_DIGITS + 2),
            field="payload",
            reject_executable=True,
            depth=0,
        )


def test_reward_uses_hit_metrics_and_completed_status() -> None:
    engine = RewardEngine()
    result = engine.compute(
        {},
        {"hit_at_5": 1.0, "mrr": 0.5, "ok": True},
        {"status": "completed"},
    )
    assert result["components"]["recall_quality"] == 1.0
    assert result["components"]["task_success"] == 2.0
    assert result["reward"] > 0


def test_reward_treats_succeeded_as_success() -> None:
    engine = RewardEngine()
    result = engine.compute({}, {}, {"status": "succeeded"})
    assert result["components"]["task_success"] == 2.0
