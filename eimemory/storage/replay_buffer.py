from __future__ import annotations

from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef


class ReplayBuffer:
    """Persistent scoped replay buffer backed by normal eimemory records."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def add_transition(
        self,
        *,
        state: dict[str, Any],
        action: dict[str, Any],
        reward: dict[str, Any],
        next_state: dict[str, Any],
        scope: dict[str, Any] | ScopeRef | None,
        source_record_id: str = "",
    ) -> RecordEnvelope:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        reward_score = _float(reward.get("reward") if isinstance(reward, dict) else reward)
        action_key = action_identity(action)
        record = RecordEnvelope.create(
            kind="rl_transition",
            title=f"RL transition: {action_key}",
            summary=f"reward {reward_score} for {action_key}",
            content={
                "state": dict(state or {}),
                "action": dict(action or {}),
                "reward": dict(reward or {}),
                "next_state": dict(next_state or {}),
                "source_record_id": str(source_record_id or ""),
            },
            tags=["rl", "transition", str(action.get("type") or "")],
            source="eimemory.rl.replay_buffer",
            scope=scope_ref,
            provenance={
                "report_type": "rl_transition",
                "source_record_id": str(source_record_id or ""),
            },
            meta={
                "report_type": "rl_transition",
                "action_key": action_key,
                "action_type": str(action.get("type") or ""),
                "reward": reward_score,
                "source_record_id": str(source_record_id or ""),
            },
        )
        return self.store.append(record)

    def sample(self, *, scope: dict[str, Any] | ScopeRef | None, k: int = 32) -> list[RecordEnvelope]:
        return list(self.store.list_records(kinds=["rl_transition"], scope=scope, limit=max(1, int(k))))


def action_identity(action: dict[str, Any] | None) -> str:
    payload = dict(action or {})
    action_type = str(payload.get("type") or "action").strip() or "action"
    action_id = str(payload.get("id") or payload.get("name") or payload.get("action") or action_type).strip()
    return f"{action_type}:{action_id}"


def _float(value: Any) -> float:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0
