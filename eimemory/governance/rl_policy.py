from __future__ import annotations

from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.replay_buffer import action_identity


class RLPolicy:
    """Small value-table policy over named actions."""

    def __init__(self, store: Any, *, alpha: float = 0.1) -> None:
        self.store = store
        self.alpha = float(alpha)

    def select_action(self, state: dict[str, Any], *, scope: dict[str, Any] | ScopeRef | None = None) -> dict[str, Any]:
        candidates = [dict(item) for item in (state.get("possible_actions") or []) if isinstance(item, dict)]
        if not candidates:
            return {}
        best = max(
            candidates,
            key=lambda item: (
                _float(item.get("value")) + self.value_for(action_identity(item), scope=scope),
                str(item.get("id") or item.get("name") or ""),
            ),
        )
        selected = dict(best)
        selected["action_key"] = action_identity(best)
        selected["policy_value"] = self.value_for(selected["action_key"], scope=scope)
        selected["selected_by"] = "rl_policy.value_table"
        return selected

    def update(
        self,
        *,
        state: dict[str, Any],
        action: dict[str, Any],
        reward: dict[str, Any],
        scope: dict[str, Any] | ScopeRef | None = None,
    ) -> dict[str, Any]:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        action_key = action_identity(action)
        previous = self.value_for(action_key, scope=scope_ref)
        reward_value = _float(reward.get("reward") if isinstance(reward, dict) else reward)
        value = round(previous + self.alpha * reward_value, 3)
        record = RecordEnvelope.create(
            kind="rl_policy_value",
            title=f"RL policy value: {action_key}",
            summary=f"{action_key} value {value}",
            content={
                "state": dict(state or {}),
                "action": dict(action or {}),
                "reward": dict(reward or {}),
                "previous_value": previous,
                "value": value,
                "alpha": self.alpha,
            },
            tags=["rl", "policy", str(action.get("type") or "")],
            source="eimemory.rl.policy",
            scope=scope_ref,
            provenance={"report_type": "rl_policy_value", "action_key": action_key},
            meta={
                "report_type": "rl_policy_value",
                "action_key": action_key,
                "action_type": str(action.get("type") or ""),
                "previous_value": previous,
                "value": value,
                "reward": reward_value,
                "alpha": self.alpha,
            },
        )
        stored = self.store.append(record)
        return {
            "ok": True,
            "action_key": action_key,
            "previous_value": previous,
            "value": value,
            "reward": reward_value,
            "record_id": stored.record_id,
        }

    def value_for(self, action_key: str, *, scope: dict[str, Any] | ScopeRef | None = None) -> float:
        try:
            records = self.store.list_records(kinds=["rl_policy_value"], scope=scope, limit=200)
        except Exception:
            return 0.0
        for record in records:
            if str(record.meta.get("action_key") or "") == action_key:
                return _float(record.meta.get("value"))
        return 0.0


def _float(value: Any) -> float:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0
