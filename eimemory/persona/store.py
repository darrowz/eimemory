from __future__ import annotations

import json
from pathlib import Path
from dataclasses import asdict
from hashlib import sha256
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.persona.schema import PersonaCorrectionEvent, PersonaState, PersonaTraceEvent
from eimemory.persona.state import default_persona_state, enforce_hard_boundaries
from eimemory.storage.atomic_file import atomic_write_json


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
        last_good = self._latest_snapshot_path()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            if last_good is not None:
                try:
                    payload = json.loads(last_good.read_text(encoding="utf-8"))
                    return enforce_hard_boundaries(
                        PersonaState.from_dict(payload if isinstance(payload, dict) else {})
                    )
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    pass
            raise ValueError(f"corrupt persona state at {self.state_path}") from exc
        try:
            return enforce_hard_boundaries(PersonaState.from_dict(payload if isinstance(payload, dict) else {}))
        except (TypeError, ValueError) as exc:
            if last_good is not None:
                try:
                    payload = json.loads(last_good.read_text(encoding="utf-8"))
                    return enforce_hard_boundaries(
                        PersonaState.from_dict(payload if isinstance(payload, dict) else {})
                    )
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    pass
            raise ValueError(f"invalid persona state at {self.state_path}") from exc

    def save_state(self, state: PersonaState, *, scope: dict[str, Any] | None = None) -> RecordEnvelope:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        payload = state.to_dict()
        atomic_write_json(self.state_path, payload)
        snapshot_path = self.snapshot_dir / f"persona_state_{_safe_ts(state.updated_at)}.json"
        atomic_write_json(snapshot_path, payload)
        self._prune_snapshots(keep=32)
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
        try:
            stored = self._append(record)
        except Exception as exc:
            # BC-08: file write succeeded but audit append failed — surface gap and retry once.
            gap_path = self.state_dir / "persona_audit_gap.json"
            try:
                atomic_write_json(
                    gap_path,
                    {
                        "schema": "eimemory.persona_audit_gap.v1",
                        "error": f"{type(exc).__name__}:{exc}",
                        "snapshot_path": str(snapshot_path),
                        "state_path": str(self.state_path),
                    },
                )
            except Exception:
                pass
            try:
                stored = self._append(record)
            except Exception as retry_exc:
                raise RuntimeError(
                    f"persona_audit_gap: state file updated but audit append failed ({type(retry_exc).__name__})"
                ) from retry_exc
            try:
                if gap_path.exists():
                    gap_path.unlink()
            except OSError:
                pass
            return stored
        gap_path = self.state_dir / "persona_audit_gap.json"
        if gap_path.exists():
            try:
                gap_path.unlink()
            except OSError:
                pass
        return stored

    def record_correction(
        self,
        correction: PersonaCorrectionEvent,
        *,
        scope: dict[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> RecordEnvelope:
        payload = correction.to_dict()
        scope_ref = ScopeRef.from_dict(scope or {})
        record = RecordEnvelope.create(
            kind="feedback",
            title=f"Persona correction: {correction.category}",
            summary=correction.rule_candidate,
            detail=correction.raw_text,
            content=payload,
            tags=["persona", "correction", correction.category],
            source="persona.correction",
            scope=scope_ref,
            meta={"category": correction.category, "severity": correction.severity},
        )
        if idempotency_key:
            record.record_id = "personacorr_" + _stable_hash(asdict(scope_ref), idempotency_key)[:24]
            record.meta["idempotency_key"] = idempotency_key
            record.content["idempotency_key"] = idempotency_key
            if self.record_store is not None:
                existing = self.record_store.get_by_id(record.record_id, scope=scope_ref)
                if existing is not None:
                    return existing
        return self._append(record)

    def record_trace(
        self,
        trace: PersonaTraceEvent,
        *,
        scope: dict[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> RecordEnvelope:
        payload = trace.to_dict()
        scope_ref = ScopeRef.from_dict(scope or {})
        record = RecordEnvelope.create(
            kind="reflection",
            title="Persona trace",
            summary=(
                f"Persona {'enabled' if trace.enabled else 'disabled'} "
                f"scene={trace.scene or 'none'} guidance={trace.guidance_length} "
                f"injection={trace.injection_latency_ms:.3f}ms"
            ),
            detail="Runtime trace for OpenClaw persona guidance injection.",
            content=payload,
            tags=["persona", "trace"],
            source="persona.trace",
            scope=scope_ref,
            meta={
                "report_type": "persona.trace",
                "scene": trace.scene,
                "enabled": trace.enabled,
                "guidance_length": trace.guidance_length,
                "guidance_latency_ms": trace.guidance_latency_ms,
                "injection_latency_ms": trace.injection_latency_ms,
            },
        )
        if idempotency_key:
            record.record_id = "personatrace_" + _stable_hash(asdict(scope_ref), idempotency_key)[:24]
            record.meta["idempotency_key"] = idempotency_key
            record.content["idempotency_key"] = idempotency_key
            if self.record_store is not None:
                existing = self.record_store.get_by_id(record.record_id, scope=scope_ref)
                if existing is not None:
                    return existing
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
        pass_rate = _float_or_default(report.get("pass_rate"), default=0.0)
        record = RecordEnvelope.create(
            kind="replay_result",
            title="Persona eval result",
            summary=f"Persona eval pass rate {pass_rate:.3f}",
            detail="Deterministic persona guidance replay result.",
            content={"event_type": "persona.eval_result", **dict(report)},
            tags=["persona", "eval"],
            source="persona.eval_result",
            scope=ScopeRef.from_dict(scope or {}),
            meta={"capability": "persona.layer", "report_type": "persona.eval_result"},
        )
        return self._append(record)

    def _prune_snapshots(self, *, keep: int = 32) -> None:
        """Retain only the newest snapshots (EXT-15)."""
        if not self.snapshot_dir.exists():
            return
        snapshots = sorted(
            (path for path in self.snapshot_dir.glob("persona_state_*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in snapshots[max(1, int(keep)):]:
            try:
                path.unlink()
            except OSError:
                pass

    def _latest_snapshot_path(self) -> Path | None:
        if not self.snapshot_dir.exists():
            return None
        snapshots = sorted(
            (path for path in self.snapshot_dir.glob("persona_state_*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return snapshots[0] if snapshots else None

    def _append(self, record: RecordEnvelope) -> RecordEnvelope:
        if self.record_store is None:
            return record
        return self.record_store.append(record)


def _safe_ts(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in str(value or ""))[:48] or "snapshot"


def _stable_hash(*values: Any) -> str:
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(raw.encode("utf-8")).hexdigest()


def _float_or_default(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
