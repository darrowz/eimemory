from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.persona.schema import PersonaCorrectionEvent, PersonaState
from eimemory.persona.state import default_persona_state, enforce_hard_boundaries


class PersonaStore:
    def __init__(self, store_or_root: Any) -> None:
        self.record_store = store_or_root if hasattr(store_or_root, "append") else None
        root = getattr(store_or_root, "root", store_or_root)
        self.root = Path(root)
        self.state_dir = self.root / "state"
        self.state_path = self.state_dir / "persona_state.json"
        self.snapshot_dir = self.state_dir / "persona_snapshots"

    def load_state(self) -> PersonaState:
        if not self.state_path.exists():
            return default_persona_state()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default_persona_state()
        return enforce_hard_boundaries(PersonaState.from_dict(payload if isinstance(payload, dict) else {}))

    def save_state(self, state: PersonaState, *, scope: dict[str, Any] | None = None) -> RecordEnvelope:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        payload = state.to_dict()
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        snapshot_path = self.snapshot_dir / f"persona_state_{_safe_ts(state.updated_at)}.json"
        snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        record = RecordEnvelope.create(
            kind="reflection",
            title="Persona state snapshot",
            summary="Functional persona state snapshot updated.",
            detail="Snapshot of persona traits, relationship, runtime state, and immutable boundaries.",
            content={"event_type": "persona.state_snapshot", "state": payload, "snapshot_path": str(snapshot_path)},
            tags=["persona", "state_snapshot"],
            source="persona.state_snapshot",
            scope=ScopeRef.from_dict(scope or {}),
            meta={"report_type": "persona.state_snapshot"},
        )
        return self._append(record)

    def record_correction(self, correction: PersonaCorrectionEvent, *, scope: dict[str, Any] | None = None) -> RecordEnvelope:
        payload = correction.to_dict()
        record = RecordEnvelope.create(
            kind="feedback",
            title=f"Persona correction: {correction.category}",
            summary=correction.rule_candidate,
            detail=correction.raw_text,
            content=payload,
            tags=["persona", "correction", correction.category],
            source="persona.correction",
            scope=ScopeRef.from_dict(scope or {}),
            meta={"category": correction.category, "severity": correction.severity},
        )
        return self._append(record)

    def list_corrections(self, *, scope: dict[str, Any] | None = None, limit: int = 50) -> list[PersonaCorrectionEvent]:
        if self.record_store is None:
            return []
        records = self.record_store.list_records(kinds=["feedback"], scope=ScopeRef.from_dict(scope or {}), limit=limit)
        corrections: list[PersonaCorrectionEvent] = []
        for record in records:
            if record.source != "persona.correction":
                continue
            content = record.content if isinstance(record.content, dict) else {}
            try:
                corrections.append(
                    PersonaCorrectionEvent(
                        raw_text=str(content.get("raw_text") or ""),
                        category=str(content.get("category") or "tone"),
                        severity=float(content.get("severity") or 0.0),
                        trait_delta={str(k): float(v) for k, v in dict(content.get("trait_delta") or {}).items()},
                        rule_candidate=str(content.get("rule_candidate") or ""),
                        source=str(content.get("source") or "user_message"),
                        event_type=str(content.get("event_type") or "persona.correction"),
                        created_at=str(content.get("created_at") or record.time.created_at),
                    )
                )
            except (TypeError, ValueError):
                continue
        return corrections

    def record_evolution(self, result: Any, *, scope: dict[str, Any] | None = None) -> RecordEnvelope:
        payload = result.to_dict() if hasattr(result, "to_dict") else dict(result or {})
        record = RecordEnvelope.create(
            kind="reflection",
            title="Persona evolution",
            summary=f"Applied persona categories: {', '.join(payload.get('applied_categories') or []) or 'none'}",
            detail="Persona correction loop produced an evolution result.",
            content={"event_type": "persona.evolution", **payload},
            tags=["persona", "evolution"],
            source="persona.evolution",
            scope=ScopeRef.from_dict(scope or {}),
            meta={"report_type": "persona.evolution"},
        )
        return self._append(record)

    def record_eval_result(self, report: dict[str, Any], *, scope: dict[str, Any] | None = None) -> RecordEnvelope:
        record = RecordEnvelope.create(
            kind="replay_result",
            title="Persona eval result",
            summary=f"Persona eval pass rate {report.get('pass_rate', 0):.3f}",
            detail="Deterministic persona guidance replay result.",
            content={"event_type": "persona.eval_result", **dict(report)},
            tags=["persona", "eval"],
            source="persona.eval_result",
            scope=ScopeRef.from_dict(scope or {}),
            meta={"capability": "persona.layer", "report_type": "persona.eval_result"},
        )
        return self._append(record)

    def _append(self, record: RecordEnvelope) -> RecordEnvelope:
        if self.record_store is None:
            return record
        return self.record_store.append(record)


def _safe_ts(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in str(value or ""))[:48] or "snapshot"
