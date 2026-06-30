from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from eimemory.persona.schema import PersonaCorrectionEvent, PersonaState
from eimemory.persona.state import apply_trait_delta, enforce_hard_boundaries


@dataclass(slots=True)
class PersonaEvolutionResult:
    state: PersonaState
    applied_categories: list[str] = field(default_factory=list)
    rule_candidates: list[str] = field(default_factory=list)
    dry_run: bool = True
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.to_dict()
        return payload


def evolve_persona(
    state: PersonaState,
    corrections: list[PersonaCorrectionEvent],
    *,
    store: Any | None = None,
    scope: dict[str, Any] | None = None,
    dry_run: bool = True,
) -> PersonaEvolutionResult:
    grouped: dict[str, list[PersonaCorrectionEvent]] = defaultdict(list)
    for correction in corrections:
        grouped[correction.category].append(correction)
    updated = PersonaState.from_dict(state.to_dict())
    applied: list[str] = []
    rules: list[str] = []
    for category, events in grouped.items():
        if len(events) < 3 and max(event.severity for event in events) < 0.8:
            continue
        delta = _merged_delta(events)
        updated = apply_trait_delta(updated, delta)
        applied.append(category)
        rules.extend(event.rule_candidate for event in events if event.rule_candidate)
    updated = enforce_hard_boundaries(updated)
    result = PersonaEvolutionResult(state=updated, applied_categories=applied, rule_candidates=_dedupe(rules), dry_run=dry_run)
    if store is not None and not dry_run:
        store.record_evolution(result, scope=scope)
        store.save_state(updated, scope=scope)
    return result


def _merged_delta(events: list[PersonaCorrectionEvent]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for event in events:
        for key, value in event.trait_delta.items():
            totals[key] = totals.get(key, 0.0) + float(value)
    count = max(1, len(events))
    return {key: max(-0.2, min(0.2, value / count)) for key, value in totals.items()}


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
