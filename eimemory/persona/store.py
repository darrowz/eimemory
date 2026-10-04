from __future__ import annotations

import json
from pathlib import Path
from dataclasses import asdict
from hashlib import sha256
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.persona.schema import PersonaCorrectionEvent, PersonaState, PersonaTraceEvent
from eimemory.persona.feedback_safety import (
    PersonaCorrectionRejected, safe_correction_payload, validate_idempotency_key,
)
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
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
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
        # Validate before allocating a record ID or touching any storage.
        original = correction.to_dict()
        payload = safe_correction_payload(original)
        idempotency_key = validate_idempotency_key(idempotency_key)
        scope_ref = ScopeRef.from_dict(scope or {})
        if self.record_store is None:
            raise PersonaCorrectionRejected("record_store_unavailable")
        input_digest = _stable_hash({key: value for key, value in original.items() if key != "created_at"})
        identity_digest = _stable_hash(asdict(scope_ref), idempotency_key) if idempotency_key else ""
        record_id = "personacorr_" + identity_digest[:24] if identity_digest else ""
        if record_id:
            # Preserve the old scoped ID algorithm, with no text scan or legacy rewrite.
            try:
                existing = self.record_store.get_by_id(record_id, scope=scope_ref, exact_scope=True)
            except RuntimeError as exc:
                # This native exact-read failure means no usable legacy envelope.
                # Do not normalize unrelated infrastructure or uncertain write errors.
                if type(exc) is RuntimeError and exc.args == ("exact_scope_record_unavailable_or_mismatched",):
                    raise PersonaCorrectionRejected("invalid_existing_correction") from None
                raise
            if existing is not None:
                return self._safe_correction_receipt(existing, scope_ref, input_digest=input_digest)
        record = RecordEnvelope.create(
            kind="feedback",
            title=f"Persona correction: {payload['category']}",
            summary=payload["rule_candidate"],
            detail=payload["raw_text"],
            content={**payload, "input_digest": input_digest},
            tags=["persona", "correction", payload["category"]],
            source="persona.correction",
            scope=scope_ref,
            meta={"category": payload["category"], "severity": payload["severity"],
                  "source_validation": "format_only", "input_digest": input_digest},
        )
        if record_id:
            record.record_id = record_id
            record.meta["idempotency_digest"] = identity_digest
            record.content["idempotency_digest"] = identity_digest
            # Native Store's transactional insert-once check closes the lookup/append race.
            # Unsupported Store adapters fail closed; do not fall back to an overwrite.
            def existing_matches(existing: RecordEnvelope) -> bool:
                self._safe_correction_receipt(existing, scope_ref, input_digest=input_digest)
                return True
            stored = self.record_store.append(record, existing_match=existing_matches)
        else:
            stored = self._append(record)
        return self._safe_correction_receipt(stored, scope_ref, input_digest=input_digest)

    def _safe_correction_receipt(
        self, record: RecordEnvelope, scope: ScopeRef, *, input_digest: str,
    ) -> RecordEnvelope:
        if record.scope != scope or record.kind != "feedback" or record.source != "persona.correction":
            raise PersonaCorrectionRejected("invalid_existing_correction")
        if record.status != "active":
            raise PersonaCorrectionRejected("invalid_existing_correction")
        content = record.content if isinstance(record.content, dict) else {}
        try:
            payload = safe_correction_payload(content)
        except PersonaCorrectionRejected as exc:
            code = "legacy_sensitive_payload_withheld" if exc.code == "sensitive_feedback" else "invalid_existing_correction"
            raise PersonaCorrectionRejected(code) from None
        prior_digest = content.get("input_digest")
        if not prior_digest:
            # Legacy payloads are compared only after the exact scoped identity lookup.
            fields = PersonaCorrectionEvent.__dataclass_fields__
            prior_digest = _stable_hash({key: content[key] for key in fields if key != "created_at"})
        if prior_digest != input_digest:
            raise PersonaCorrectionRejected("idempotency_conflict")
        # Build an allowlisted response, never return arbitrary legacy detail/meta/links.
        safe = RecordEnvelope.create(
            kind="feedback", title=f"Persona correction: {payload['category']}",
            summary=payload["rule_candidate"], detail=payload["raw_text"], content=payload,
            tags=["persona", "correction", payload["category"]], source="persona.correction",
            scope=scope, meta={"category": payload["category"], "severity": payload["severity"],
                               "persisted": True, "source_validation": "format_only"},
        )
        safe.record_id = record.record_id  # A real stored identity, not a rejection ID.
        return safe

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

    def list_corrections(
        self, *, scope: dict[str, Any] | None = None, limit: int = 50, max_scan: int = 1000,
    ) -> list[PersonaCorrectionEvent]:
        """Return a bounded top-limit window within one native read snapshot.

        Physical count and hydrated pages must share a read transaction. A short
        hydrated page is not exhaustion: missing/corrupt payloads raise incomplete.
        Source/type consistency checks do not authenticate the claimed origin.
        """
        if type(limit) is not int or type(max_scan) is not int or not 0 <= limit <= max_scan <= 10_000:
            raise PersonaCorrectionRejected("invalid_scan_limit")
        if limit == 0:
            return []
        read_consistent = getattr(self.record_store, "read_consistent", None)
        if not callable(read_consistent):
            raise PersonaCorrectionRejected("correction_scan_incomplete")
        scope_ref = ScopeRef.from_dict(scope or {})

        def read_window(reader: Any) -> list[PersonaCorrectionEvent]:
            count_records = getattr(reader, "count_records", None)
            list_records = getattr(reader, "list_records", None)
            if not callable(count_records) or not callable(list_records):
                raise PersonaCorrectionRejected("correction_scan_incomplete")
            # Identical native SQL filters; COUNT sees physical rows, not hydration.
            filters = {"kinds": ["feedback"], "scope": scope_ref, "status": "active", "source_ids": None}
            total = count_records(**filters)
            if type(total) is not int or total < 0:
                raise PersonaCorrectionRejected("correction_scan_incomplete")
            corrections: list[PersonaCorrectionEvent] = []
            offset = 0
            while offset < total and offset < max_scan:
                expected = min(100, total - offset, max_scan - offset)
                records = list_records(**filters, limit=expected, offset=offset)
                if not isinstance(records, list) or len(records) != expected:
                    raise PersonaCorrectionRejected("correction_scan_incomplete")
                for record in records:
                    if record.status != "active" or record.kind != "feedback" or record.source != "persona.correction":
                        continue
                    if record.scope != scope_ref:
                        continue
                    try:
                        payload = safe_correction_payload(record.content)
                    except PersonaCorrectionRejected:
                        continue
                    corrections.append(PersonaCorrectionEvent(**payload))
                    if len(corrections) >= limit:
                        # An explicit top-limit result is not a claim of full history.
                        return corrections
                offset += expected
            if offset < total:
                raise PersonaCorrectionRejected("correction_scan_incomplete")
            return corrections

        return read_consistent(read_window)

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
