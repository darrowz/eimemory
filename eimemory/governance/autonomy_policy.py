from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class AutonomyPolicy:
    rollout_radius: str = "honxin_single_scope"
    max_daily_goals: int = 3
    max_auto_promotions: int = 3
    max_auto_rollbacks: int = 5
    min_replay_pass_rate_for_auto: float = 0.8
    post_promotion_hit_window: int = 3
    timeout_seconds: int = 900

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_autonomy_policy(value: dict[str, Any] | AutonomyPolicy | None = None) -> AutonomyPolicy:
    if isinstance(value, AutonomyPolicy):
        return value
    raw = dict(value or {})
    return AutonomyPolicy(
        rollout_radius=str(raw.get("rollout_radius") or "honxin_single_scope"),
        max_daily_goals=_bounded_int(raw.get("max_daily_goals"), default=3, minimum=1, maximum=20),
        max_auto_promotions=_bounded_int(raw.get("max_auto_promotions"), default=3, minimum=0, maximum=20),
        max_auto_rollbacks=_bounded_int(raw.get("max_auto_rollbacks"), default=5, minimum=0, maximum=50),
        min_replay_pass_rate_for_auto=_bounded_float(raw.get("min_replay_pass_rate_for_auto"), default=0.8, minimum=0.0, maximum=1.0),
        post_promotion_hit_window=_bounded_int(raw.get("post_promotion_hit_window"), default=3, minimum=1, maximum=20),
        timeout_seconds=_bounded_int(raw.get("timeout_seconds"), default=900, minimum=30, maximum=7200),
    )


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return round(max(float(minimum), min(float(maximum), parsed)), 3)
