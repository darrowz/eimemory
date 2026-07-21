from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import re
from threading import BoundedSemaphore, RLock, Thread
from typing import Any, Iterable, Mapping

from eimemory.adapters.runtime.channel import base_scope_from_channel, normalize_runtime_channel, resolve_channel_scope
from eimemory.adapters.runtime.redaction import bounded_redacted_text
from eimemory.governance.evidence_contract import current_release_identity, release_identity_payload
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.models.source_partitions import normalize_source_ids


PROACTIVE_POLICY_VERSION = "proactive-recall.v1"
PROACTIVE_CONFIDENCE_THRESHOLD = 0.70
DEFAULT_MAX_CONTEXT_CHARS = 3_600
DEFAULT_MAX_SESSIONS = 128
DEFAULT_MAX_DECISIONS = 512
DEFAULT_MAX_CACHE_ENTRIES = 128
DEFAULT_MAX_BYPASS_DIAGNOSTICS = 64
DEFAULT_RECALL_TIMEOUT_SECONDS = 0.8
_MAX_TURNS_PER_SESSION = 4
_MAX_TURN_SUMMARY_CHARS = 1_000
_MAX_QUERY_CHARS = 8_000
_MAX_VOLUNTEERED_ITEMS = 3
_MAX_RECALL_WORKERS = 2
_TERMINAL_STATES = frozenset({"used", "not_used", "rejected"})
_TRANSITIONS = {
    "volunteered": frozenset({"injected", "not_used", "rejected"}),
    "injected": frozenset({"used", "not_used", "rejected"}),
    "used": frozenset({"rejected"}),
    "not_used": frozenset({"rejected"}),
    "rejected": frozenset(),
}
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{2,63}|[\u3400-\u9fff]{2,16}")
_STOP_TERMS = frozenset(
    {
        "the", "and", "for", "with", "from", "that", "this", "what", "when", "where",
        "which", "please", "should", "could", "would", "user", "assistant", "ignore", "previous",
        "instructions", "system", "prompt", "请问", "一下", "这个", "那个", "我们", "你们", "他们",
    }
)


def _bounded_text(value: object, limit: int) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split())
    return normalized[: max(0, int(limit))]


def _scope_tuple(scope: Mapping[str, Any]) -> tuple[str, str, str, str]:
    ref = ScopeRef.from_dict(dict(scope))
    return (ref.tenant_id, ref.agent_id, ref.workspace_id, ref.user_id)


def _source_key(source_ids: Iterable[str] | None) -> tuple[str, ...]:
    normalized = normalize_source_ids(source_ids)
    return ("*",) if normalized is None else normalized


@dataclass(slots=True)
class _SessionState:
    turns: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=_MAX_TURNS_PER_SESSION))
    volunteered_refs: set[tuple[str, str]] = field(default_factory=set)


@dataclass(slots=True)
class _DecisionItem:
    record_id: str
    source_id: str
    citation: str
    confidence: float
    state: str = "volunteered"
    mandatory: bool = False


@dataclass(slots=True)
class _DecisionState:
    decision_id: str
    query_id: str
    query: str
    channel: str
    scope: dict[str, str]
    source_ids: tuple[str, ...]
    session_id: str
    turn_id: str
    policy_version: str
    release_identity: dict[str, str]
    control_cohort: bool
    pair_id: str
    release_bound: bool
    items: dict[str, _DecisionItem]


@dataclass(slots=True)
class _CachedRecall:
    records: tuple[RecordEnvelope, ...]
    explanation: dict[str, Any]
    confidence: float


class ProactiveRecallService:
    """Deterministic proactive recall and explicit use-feedback coordinator.

    Confidence is a weighted mean: intent 25%, fusion evidence 30%, rank 20%,
    and stored quality 25%.  A record volunteers only at an inclusive 0.70.
    Memory text is always encoded as untrusted JSON data, never prompt syntax.
    """

    def __init__(
        self,
        runtime: Any,
        *,
        release_identity: Mapping[str, Any] | None = None,
        release_identity_provider: Any | None = None,
        control_percent: int = 10,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_decisions: int = DEFAULT_MAX_DECISIONS,
        max_cache_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
        max_bypass_diagnostics: int = DEFAULT_MAX_BYPASS_DIAGNOSTICS,
        recall_timeout_seconds: float = DEFAULT_RECALL_TIMEOUT_SECONDS,
    ) -> None:
        self.runtime = runtime
        self._release_override = self._normalize_release(release_identity or {})
        self._release_provider = release_identity_provider
        self.control_percent = max(0, min(100, int(control_percent)))
        self.max_context_chars = max(256, min(16_000, int(max_context_chars)))
        self.max_sessions = max(1, min(2_048, int(max_sessions)))
        self.max_decisions = max(1, min(8_192, int(max_decisions)))
        self.max_cache_entries = max(1, min(2_048, int(max_cache_entries)))
        self.recall_timeout_seconds = max(0.01, min(10.0, float(recall_timeout_seconds)))
        self._sessions: OrderedDict[tuple[Any, ...], _SessionState] = OrderedDict()
        self._decisions: OrderedDict[str, _DecisionState] = OrderedDict()
        self._candidate_cache: OrderedDict[str, _CachedRecall | tuple[RecordEnvelope, ...]] = OrderedDict()
        self._bypasses: deque[dict[str, str]] = deque(maxlen=max(1, min(512, int(max_bypass_diagnostics))))
        self._recall_slots = BoundedSemaphore(_MAX_RECALL_WORKERS)
        self._workers: set[Thread] = set()
        self._lock = RLock()

    @staticmethod
    def score_confidence(
        *, intent_strength: float, evidence_strength: float, rank_strength: float, quality: float
    ) -> float:
        values = (
            max(0.0, min(1.0, float(intent_strength))),
            max(0.0, min(1.0, float(evidence_strength))),
            max(0.0, min(1.0, float(rank_strength))),
            max(0.0, min(1.0, float(quality))),
        )
        return round(values[0] * 0.25 + values[1] * 0.30 + values[2] * 0.20 + values[3] * 0.25, 2)

    @staticmethod
    def eligible(confidence: float) -> bool:
        return float(confidence) >= PROACTIVE_CONFIDENCE_THRESHOLD

    def complete_turn(
        self,
        *,
        channel: str,
        scope: Mapping[str, Any],
        source_ids: Iterable[str] | None,
        session_id: str,
        turn_id: str,
        user_summary: str,
        assistant_summary: str,
    ) -> None:
        channel_id, exact_scope, sources, normalized_session = self._namespace(
            channel=channel, scope=scope, source_ids=source_ids, session_id=session_id
        )
        normalized_turn = _bounded_text(turn_id, 500)
        if not normalized_turn:
            raise ValueError("turn_id is required")
        summary = _bounded_text(
            f"User: {_bounded_text(user_summary, _MAX_TURN_SUMMARY_CHARS // 2)} "
            f"Assistant: {_bounded_text(assistant_summary, _MAX_TURN_SUMMARY_CHARS // 2)}",
            _MAX_TURN_SUMMARY_CHARS,
        )
        safe_summary = self._redact_sensitive_summary(summary)
        entities = self._entities(safe_summary)
        persisted = self.runtime.store.append_proactive_turn(
            {
                "channel": channel_id,
                "scope": exact_scope,
                "source_key": self._source_digest(sources),
                "session_id": normalized_session,
                "turn_id": normalized_turn,
                "summary": safe_summary,
                "entities": entities,
            },
            max_session_turns=_MAX_TURNS_PER_SESSION,
            max_global_turns=self.max_sessions * _MAX_TURNS_PER_SESSION,
        )
        key = self._session_key(channel_id, exact_scope, sources, normalized_session)
        with self._lock:
            state = self._session(key)
            state.turns.clear()
            state.turns.extend(
                (str(item["turn_id"]), " ".join(item.get("entities") or []))
                for item in persisted
            )
            self._sessions.move_to_end(key)

    def decide(
        self,
        *,
        channel: str,
        scope: Mapping[str, Any],
        source_ids: Iterable[str] | None,
        session_id: str,
        query_id: str,
        query: str,
        task_type: str = "",
        recall_bundle: RecallBundle | None = None,
    ) -> dict[str, Any]:
        channel_id, exact_scope, sources, normalized_session = self._namespace(
            channel=channel, scope=scope, source_ids=source_ids, session_id=session_id
        )
        normalized_query = _bounded_text(query, _MAX_QUERY_CHARS)
        normalized_query_id = _bounded_text(query_id, 500)
        if not normalized_query or not normalized_query_id:
            raise ValueError("query and query_id are required")
        release = self._current_release(exact_scope, channel=channel_id)
        policy_version = self._policy_version()
        query_digest = sha256(normalized_query.encode("utf-8", errors="replace")).hexdigest()
        cache_key = self._cache_key(
            channel_id, exact_scope, sources, query_digest, policy_version, release
        )
        if not all(str(release.get(key) or "") for key in release):
            self._record_bypass(
                channel=channel_id,
                session_id=normalized_session,
                query_digest=query_digest,
                reason="release_identity_unavailable",
            )
            return self._empty_decision(
                query_id=normalized_query_id,
                cache_key=cache_key,
                release=release,
                bypassed=True,
            )
        session_key = self._session_key(channel_id, exact_scope, sources, normalized_session)
        persisted_turns = self.runtime.store.load_proactive_turns(
            {
                "channel": channel_id,
                "scope": exact_scope,
                "source_key": self._source_digest(sources),
                "session_id": normalized_session,
            },
            limit=_MAX_TURNS_PER_SESSION,
        )
        with self._lock:
            session = self._session(session_key)
            session.turns.clear()
            session.turns.extend(
                (str(item["turn_id"]), " ".join(item.get("entities") or []))
                for item in persisted_turns
            )
            turn_summaries = [summary for _turn_id, summary in session.turns]
            cached = self._candidate_cache.get(cache_key)
            if cached is not None:
                self._candidate_cache.move_to_end(cache_key)
        if recall_bundle is not None:
            # OpenClaw has already applied its authoritative policy/evidence
            # gates to this exact turn. Never replace that bundle with a cache.
            cached = None
        if cached is None:
            recall_query = self._recall_query(normalized_query, turn_summaries)
            try:
                bundle = recall_bundle or self._recall_with_timeout(
                    query=recall_query, scope=exact_scope,
                    source_ids=sources, task_type=task_type,
                )
            except Exception as exc:  # noqa: BLE001 - proactive recall is advisory
                self._record_bypass(
                    channel=channel_id,
                    session_id=normalized_session,
                    query_digest=query_digest,
                    reason=type(exc).__name__,
                )
                return self._empty_decision(
                    query_id=normalized_query_id,
                    cache_key=cache_key,
                    release=release,
                    bypassed=True,
                )
            cached = _CachedRecall(tuple(bundle.items), dict(bundle.explanation), float(bundle.confidence))
            with self._lock:
                self._candidate_cache[cache_key] = cached
                self._candidate_cache.move_to_end(cache_key)
                while len(self._candidate_cache) > self.max_cache_entries:
                    self._candidate_cache.popitem(last=False)
        records, explanation, bundle_confidence = self._cached_parts(cached)
        authorized = [
            record for record in records
            if self._authorized(record, exact_scope=exact_scope, source_ids=sources)
        ]
        intent_strength = self._intent_strength(normalized_query, turn_summaries)
        details = self._candidate_details(
            authorized, explanation=explanation, intent_strength=intent_strength,
            bundle_confidence=bundle_confidence,
        )
        mandatory_records = [record for record in authorized if self._is_hard_policy(record)]
        mandatory_refs = {(record.record_id, record.source_id) for record in mandatory_records}
        persisted_refs = self.runtime.store.proactive_session_refs(
            {
                "channel": channel_id,
                "scope": exact_scope,
                "source_key": self._source_digest(sources),
                "session_id": normalized_session,
            },
            limit=self.max_decisions,
        )
        with self._lock:
            session = self._session(session_key)
            dedupe_refs = persisted_refs | session.volunteered_refs
            voluntary_details = [
                detail for detail in details
                if (detail[0].record_id, detail[0].source_id) not in dedupe_refs
                and (detail[0].record_id, detail[0].source_id) not in mandatory_refs
            ][:_MAX_VOLUNTEERED_ITEMS]
        mandatory_details = [(record, 1.0) for record in mandatory_records[:_MAX_VOLUNTEERED_ITEMS]]
        control = self._is_control(
            channel=channel_id, scope=exact_scope, session_id=normalized_session,
            query_digest=query_digest, policy_version=policy_version,
        )
        decision_id = "pd:" + sha256(
            "\x1f".join(
                [channel_id, *(_scope_tuple(exact_scope)), *sources, normalized_session,
                 normalized_query_id, query_digest, policy_version,
                 *(str(release.get(key) or "") for key in sorted(release))]
            ).encode("utf-8")
        ).hexdigest()[:32]
        pair_id = "pp:" + sha256(
            "\x1f".join(
                [channel_id, *_scope_tuple(exact_scope), *sources, query_digest, policy_version,
                 *(str(release.get(key) or "") for key in sorted(release))]
            ).encode("utf-8")
        ).hexdigest()[:32]
        decision_items: dict[str, _DecisionItem] = {}
        public_items: list[dict[str, Any]] = []
        combined_details = [
            *((record, confidence, True) for record, confidence in mandatory_details),
            *((record, confidence, False) for record, confidence in voluntary_details),
        ]
        for rank, (record, confidence, mandatory) in enumerate(combined_details, start=1):
            citation = self._citation(decision_id, record, rank)
            item = _DecisionItem(record.record_id, record.source_id, citation, confidence, mandatory=mandatory)
            decision_items[citation] = item
            public_items.append(
                {
                    "record_id": record.record_id,
                    "source_id": record.source_id,
                    "citation": citation,
                    "confidence": confidence,
                    "title": _bounded_text(record.title, 240),
                    "text": self._record_text(record),
                    "mandatory": mandatory,
                }
            )
        release_bound = all(str(release.get(key) or "") for key in release)
        state = _DecisionState(
            decision_id=decision_id,
            query_id=normalized_query_id,
            query=normalized_query,
            channel=channel_id,
            scope=exact_scope,
            source_ids=sources,
            session_id=normalized_session,
            turn_id=normalized_query_id,
            policy_version=policy_version,
            release_identity=release,
            control_cohort=control,
            pair_id=pair_id,
            release_bound=release_bound,
            items=decision_items,
        )
        decision_payload = self._decision_payload(state, query_digest=query_digest)
        item_payloads = [
            {
                "citation": item.citation,
                "record_id": item.record_id,
                "source_id": item.source_id,
                "confidence": item.confidence,
                "state": item.state,
                "mandatory": item.mandatory,
            }
            for item in decision_items.values()
        ]
        volunteered_feedback = [
            self._feedback_record(
                state, item, "volunteered",
                control_suppressed=control and not item.mandatory,
            )
            for item in decision_items.values()
        ]
        try:
            stored_decision, _idempotent = self.runtime.store.record_proactive_decision(
                decision_payload, item_payloads, volunteered_feedback,
                max_global_decisions=self.max_decisions,
            )
        except Exception as exc:  # noqa: BLE001 - host injection remains fail-open
            self._record_bypass(
                channel=channel_id,
                session_id=normalized_session,
                query_digest=query_digest,
                reason=f"decision_{type(exc).__name__}",
            )
            return self._empty_decision(
                query_id=normalized_query_id,
                cache_key=cache_key,
                release=release,
                bypassed=True,
            )
        state = self._state_from_payload(stored_decision)
        with self._lock:
            self._decisions[decision_id] = state
            self._decisions.move_to_end(decision_id)
            while len(self._decisions) > self.max_decisions:
                self._decisions.popitem(last=False)
            session = self._session(session_key)
            session.volunteered_refs.update(
                (item.record_id, item.source_id) for item in state.items.values() if not item.mandatory
            )
        mandatory_items = [item for item in public_items if item["mandatory"]]
        voluntary_items = [item for item in public_items if not item["mandatory"]]
        delivered_items = mandatory_items if control else public_items
        context = self._render_context(delivered_items)
        return {
            "ok": True,
            "bypassed": False,
            "decision_id": decision_id,
            "query_id": normalized_query_id,
            "cache_key": cache_key,
            "control_cohort": control,
            "release_identity": dict(release),
            "release_bound": release_bound,
            "policy_version": policy_version,
            "pair_id": pair_id,
            "items": delivered_items,
            "suppressed_items": voluntary_items if control else [],
            "context": context,
        }

    def mark_injected(
        self, *, query_id: str = "", decision_id: str = "", channel: str = "",
        scope: Mapping[str, Any] | None = None, source_ids: Iterable[str] | None = None,
        session_id: str = "", turn_id: str = "", release_identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self._decision(query_id=query_id, decision_id=decision_id)
        if not self._transition_namespace_matches(
            state, channel=channel, scope=scope, source_ids=source_ids, session_id=session_id,
            turn_id=turn_id, release_identity=release_identity,
        ):
            return {"ok": False, "error": "proactive_namespace_mismatch", "decision_id": state.decision_id, "changed": 0}
        targets = {
            citation: "injected"
            for citation, item in state.items.items()
            if not state.control_cohort or item.mandatory
        }
        changed = self._transition_targets(state, targets)
        return {"ok": changed >= 0, "bypassed": changed < 0, "decision_id": state.decision_id, "changed": max(0, changed)}

    def record_feedback(
        self,
        *,
        query_id: str = "",
        decision_id: str = "",
        used_citations: Iterable[str] | None = None,
        rejected_citations: Iterable[str] | None = None,
        channel: str = "",
        scope: Mapping[str, Any] | None = None,
        source_ids: Iterable[str] | None = None,
        session_id: str = "",
        turn_id: str = "",
        release_identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self._decision(query_id=query_id, decision_id=decision_id)
        if not self._transition_namespace_matches(
            state, channel=channel, scope=scope, source_ids=source_ids, session_id=session_id,
            turn_id=turn_id, release_identity=release_identity,
        ):
            return {"ok": False, "error": "proactive_namespace_mismatch", "decision_id": state.decision_id, "changed": 0}
        used = {str(value) for value in (used_citations or ())}
        rejected = {str(value) for value in (rejected_citations or ())}
        requested = used | rejected
        if not requested.issubset(state.items):
            raise ValueError("feedback citation does not belong to the exact proactive decision")
        targets = {
            citation: ("rejected" if citation in rejected else "used")
            for citation in requested
        }
        changed = self._transition_targets(state, targets)
        return {"ok": changed >= 0, "bypassed": changed < 0, "decision_id": state.decision_id, "changed": max(0, changed)}

    def mark_terminal(
        self,
        *,
        query_id: str = "",
        decision_id: str = "",
        assistant_text: str = "",
        channel: str = "",
        scope: Mapping[str, Any] | None = None,
        source_ids: Iterable[str] | None = None,
        session_id: str = "",
        turn_id: str = "",
        release_identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        del assistant_text  # Similarity is never evidence of use.
        state = self._decision(query_id=query_id, decision_id=decision_id)
        if not self._transition_namespace_matches(
            state, channel=channel, scope=scope, source_ids=source_ids, session_id=session_id,
            turn_id=turn_id, release_identity=release_identity,
        ):
            return {"ok": False, "error": "proactive_namespace_mismatch", "decision_id": state.decision_id, "changed": 0}
        changed = self._transition_targets(
            state,
            {
                citation: "not_used"
                for citation, item in state.items.items()
                if item.state not in _TERMINAL_STATES
            },
        )
        return {"ok": changed >= 0, "bypassed": changed < 0, "decision_id": state.decision_id, "changed": max(0, changed)}

    def switch_session(
        self,
        *,
        channel: str,
        scope: Mapping[str, Any],
        source_ids: Iterable[str] | None,
        session_id: str,
    ) -> None:
        channel_id, exact_scope, sources, normalized_session = self._namespace(
            channel=channel, scope=scope, source_ids=source_ids, session_id=session_id
        )
        keep = self._session_key(channel_id, exact_scope, sources, normalized_session)
        with self._lock:
            stale = [key for key in self._sessions if key[:6] == keep[:6] and key != keep]
            for key in stale:
                self._sessions.pop(key, None)
            self._candidate_cache.clear()

    def session_status(
        self, *, channel: str, scope: Mapping[str, Any], source_ids: Iterable[str] | None, session_id: str
    ) -> dict[str, Any]:
        channel_id, exact_scope, sources, normalized_session = self._namespace(
            channel=channel, scope=scope, source_ids=source_ids, session_id=session_id
        )
        persisted = self.runtime.store.load_proactive_turns(
            {
                "channel": channel_id, "scope": exact_scope,
                "source_key": self._source_digest(sources), "session_id": normalized_session,
            },
            limit=_MAX_TURNS_PER_SESSION,
        )
        return {
            "turn_count": len(persisted),
            "turn_digests": [
                sha256(" ".join(item.get("entities") or []).encode("utf-8")).hexdigest()
                for item in persisted
            ],
        }

    def paired_metrics(self, *, scope: Mapping[str, Any], channel: str = "codex") -> dict[str, Any]:
        exact_scope = resolve_channel_scope(channel, dict(scope))
        records = self.runtime.store.list_records_by_meta_value(
            kinds=["feedback"], scope=ScopeRef.from_dict(exact_scope),
            meta_key="schema_version", meta_value="memory_usage_telemetry.v2", limit=500,
        ) or []
        latest: dict[tuple[str, str, str], tuple[str, bool]] = {}
        state_order = {"volunteered": 1, "injected": 2, "used": 3, "not_used": 3, "rejected": 4}
        for record in records:
            pair_id = str(record.meta.get("pair_id") or "")
            decision_id = str(record.meta.get("decision_id") or "")
            citation = str(record.meta.get("citation") or "")
            if pair_id and decision_id and citation:
                candidate = str(record.meta.get("proactive_state") or "")
                key = (pair_id, decision_id, citation)
                prior = latest.get(key)
                if prior is None or state_order.get(candidate, 0) > state_order.get(prior[0], 0):
                    latest[key] = (
                        candidate,
                        bool(record.meta.get("control_cohort")),
                    )
        result: dict[str, Any] = {
            "control": {"used": 0, "not_used": 0, "rejected": 0},
            "treatment": {"used": 0, "not_used": 0, "rejected": 0},
        }
        decision_outcomes: dict[tuple[str, str, bool], list[str]] = {}
        for (pair_id, decision_id, _citation), (state_name, control) in latest.items():
            if state_name in _TERMINAL_STATES:
                arm = "control" if control else "treatment"
                result[arm][state_name] += 1
                decision_outcomes.setdefault((pair_id, decision_id, control), []).append(state_name)
        paired: dict[str, dict[str, str]] = {}
        for (pair_id, _decision_id, control), outcomes in decision_outcomes.items():
            outcome = "rejected" if "rejected" in outcomes else ("used" if "used" in outcomes else "not_used")
            paired.setdefault(pair_id, {})["control_outcome" if control else "treatment_outcome"] = outcome
        pairs = [
            {"pair_id": pair_id, **arms}
            for pair_id, arms in sorted(paired.items())
            if {"control_outcome", "treatment_outcome"}.issubset(arms)
        ]
        control_used = sum(item["control_outcome"] == "used" for item in pairs)
        treatment_used = sum(item["treatment_outcome"] == "used" for item in pairs)
        pair_count = len(pairs)
        result.update(
            {
                "pair_count": pair_count,
                "pairs": pairs[:100],
                "used_rate_delta": round(
                    ((treatment_used - control_used) / pair_count) if pair_count else 0.0,
                    4,
                ),
            }
        )
        return result

    def bypass_diagnostics(self) -> list[dict[str, str]]:
        try:
            return self.runtime.store.list_proactive_bypasses(limit=self._bypasses.maxlen or 64)
        except Exception:
            with self._lock:
                return [dict(item) for item in self._bypasses]

    def close(self) -> None:
        """Bound shutdown so no timed-out recall keeps using a closed store."""

        with self._lock:
            workers = tuple(self._workers)
        for worker in workers:
            worker.join(timeout=max(1.0, self.recall_timeout_seconds))

    def current_release(self, *, channel: str, scope: Mapping[str, Any]) -> dict[str, str]:
        channel_id = normalize_runtime_channel(channel)
        exact_scope = resolve_channel_scope(channel_id, dict(scope))
        return self._current_release(exact_scope, channel=channel_id)

    def _namespace(
        self, *, channel: str, scope: Mapping[str, Any], source_ids: Iterable[str] | None, session_id: str
    ) -> tuple[str, dict[str, str], tuple[str, ...], str]:
        channel_id = normalize_runtime_channel(channel)
        exact_scope = resolve_channel_scope(channel_id, dict(scope))
        sources = _source_key(source_ids)
        normalized_session = _bounded_text(session_id, 500)
        if not normalized_session:
            raise ValueError("session_id is required")
        return channel_id, exact_scope, sources, normalized_session

    def _transition_namespace_matches(
        self,
        state: _DecisionState,
        *,
        channel: str,
        scope: Mapping[str, Any] | None,
        source_ids: Iterable[str] | None,
        session_id: str,
        turn_id: str,
        release_identity: Mapping[str, Any] | None,
    ) -> bool:
        supplied = bool(channel or scope is not None or session_id or turn_id or release_identity is not None)
        if not supplied:
            return True
        if not channel or scope is None or not session_id or not turn_id or release_identity is None:
            return False
        try:
            channel_id, exact_scope, sources, normalized_session = self._namespace(
                channel=channel, scope=scope, source_ids=source_ids, session_id=session_id
            )
        except (TypeError, ValueError):
            return False
        return bool(
            channel_id == state.channel
            and exact_scope == state.scope
            and sources == state.source_ids
            and normalized_session == state.session_id
            and _bounded_text(turn_id, 500) == state.turn_id
            and self._normalize_release(release_identity) == state.release_identity
        )

    def _session(self, key: tuple[Any, ...]) -> _SessionState:
        state = self._sessions.get(key)
        if state is None:
            state = _SessionState()
            self._sessions[key] = state
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
        return state

    @staticmethod
    def _session_key(
        channel: str, scope: Mapping[str, Any], source_ids: tuple[str, ...], session_id: str
    ) -> tuple[Any, ...]:
        return (channel, *_scope_tuple(scope), source_ids, session_id)

    def _current_release(self, scope: Mapping[str, Any], *, channel: str) -> dict[str, str]:
        if self._release_provider is not None:
            return self._normalize_release(self._release_provider(self.runtime, dict(scope)) or {})
        if self._release_override.get("release_commit"):
            return dict(self._release_override)
        identity = current_release_identity(self.runtime, ScopeRef.from_dict(dict(scope)))
        if identity is None and channel in {"codex", "hermes"}:
            identity = current_release_identity(
                self.runtime,
                ScopeRef.from_dict(base_scope_from_channel(channel, dict(scope))),
            )
        return release_identity_payload(identity) if identity is not None else self._normalize_release({})

    @staticmethod
    def _source_digest(source_ids: tuple[str, ...]) -> str:
        return sha256(json.dumps(list(source_ids), ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_release(value: Mapping[str, Any]) -> dict[str, str]:
        return {
            key: str(value.get(key) or "")
            for key in ("release_commit", "release_version", "deployment_receipt_id", "release_session_id")
        }

    def _policy_version(self) -> str:
        engine = getattr(self.runtime.memory, "recall_engine", None)
        engine_version = str(getattr(engine, "policy_version", "governed-recall.unknown"))
        return f"{PROACTIVE_POLICY_VERSION}+{engine_version}"

    @staticmethod
    def _cache_key(
        channel: str, scope: Mapping[str, Any], source_ids: tuple[str, ...], query_digest: str,
        policy_version: str, release: Mapping[str, str],
    ) -> str:
        payload = [channel, *_scope_tuple(scope), *source_ids, query_digest, policy_version]
        payload.extend(str(release.get(key) or "") for key in sorted(release))
        return "pc:" + sha256("\x1f".join(payload).encode("utf-8")).hexdigest()

    def _recall_with_timeout(
        self, *, query: str, scope: Mapping[str, Any], source_ids: tuple[str, ...], task_type: str
    ) -> RecallBundle:
        if not self._recall_slots.acquire(blocking=False):
            raise TimeoutError("proactive recall worker capacity exhausted")
        result: list[RecallBundle] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                result.append(
                    self.runtime.memory.recall(
                        query=query,
                        scope=dict(scope),
                        task_context={
                            "source_ids": [] if source_ids == () else (None if source_ids == ("*",) else list(source_ids)),
                            "runtime_channel": "proactive",
                            "exact_scope_only": True,
                            "task_type": str(task_type or "proactive.recall"),
                        },
                        limit=8,
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - passed to caller thread
                errors.append(exc)
            finally:
                self._recall_slots.release()
                with self._lock:
                    self._workers.discard(worker)

        worker = Thread(target=run, name="eimemory-proactive-recall", daemon=True)
        with self._lock:
            self._workers.add(worker)
        worker.start()
        worker.join(timeout=self.recall_timeout_seconds)
        if worker.is_alive():
            raise TimeoutError("proactive recall timed out")
        if errors:
            raise errors[0]
        if not result:
            raise RuntimeError("proactive recall produced no result")
        return result[0]

    @staticmethod
    def _cached_parts(
        cached: _CachedRecall | tuple[RecordEnvelope, ...]
    ) -> tuple[tuple[RecordEnvelope, ...], dict[str, Any], float]:
        if isinstance(cached, _CachedRecall):
            return cached.records, dict(cached.explanation), cached.confidence
        return tuple(cached), {}, 0.81

    @staticmethod
    def _authorized(
        record: RecordEnvelope, *, exact_scope: Mapping[str, Any], source_ids: tuple[str, ...]
    ) -> bool:
        if record.status != "active" or _scope_tuple(asdict(record.scope)) != _scope_tuple(exact_scope):
            return False
        return source_ids == ("*",) or record.source_id in source_ids

    def _candidate_details(
        self, records: list[RecordEnvelope], *, explanation: Mapping[str, Any],
        intent_strength: float, bundle_confidence: float,
    ) -> list[tuple[RecordEnvelope, float]]:
        fusion = explanation.get("fusion") if isinstance(explanation.get("fusion"), Mapping) else {}
        selected = fusion.get("selected") if isinstance(fusion, Mapping) else []
        detail_by_ref = {
            (str(item.get("record_id") or ""), str(item.get("source_id") or "default")): item
            for item in (selected or []) if isinstance(item, Mapping)
        }
        scoring = explanation.get("scoring") if isinstance(explanation.get("scoring"), list) else []
        quality_by_ref = {
            (str(item.get("record_id") or ""), str(item.get("source_id") or "default")):
                self._bounded_float(item.get("quality_score"), 0.5)
            for item in scoring if isinstance(item, Mapping)
        }
        ranked: list[tuple[RecordEnvelope, float]] = []
        for rank, record in enumerate(records, start=1):
            ref = (record.record_id, record.source_id)
            detail = detail_by_ref.get(ref) or {}
            evidence_strength = self._evidence_strength(detail.get("evidence"))
            if not detail and bundle_confidence:
                evidence_strength = max(evidence_strength, self._bounded_float(bundle_confidence, 0.0))
            rank_strength = max(0.4, 1.0 - ((rank - 1) * 0.15))
            quality = quality_by_ref.get(ref, self._record_quality(record))
            confidence = self.score_confidence(
                intent_strength=intent_strength,
                evidence_strength=evidence_strength,
                rank_strength=rank_strength,
                quality=quality,
            )
            if self.eligible(confidence):
                ranked.append((record, confidence))
        return ranked

    @staticmethod
    def _evidence_strength(values: object) -> float:
        strengths = {
            "exact_title": 1.0, "alias_hit": 0.95, "keyword_exact": 0.85,
            "graph_path": 0.78, "vector_match": 0.70,
        }
        if not isinstance(values, (list, tuple, set)):
            return 0.4
        return max((strengths.get(str(value), 0.4) for value in values), default=0.4)

    @staticmethod
    def _record_quality(record: RecordEnvelope) -> float:
        quality = record.meta.get("quality") if isinstance(record.meta, Mapping) else {}
        if not isinstance(quality, Mapping):
            return 0.5
        return ProactiveRecallService._bounded_float(
            quality.get("salience_score", quality.get("importance", 0.5)), 0.5
        )

    @staticmethod
    def _bounded_float(value: object, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        return max(0.0, min(1.0, parsed))

    @staticmethod
    def _intent_strength(query: str, turns: list[str]) -> float:
        lowered = query.casefold()
        explicit = ("recall", "remember", "memory", "previous", "preference", "记忆", "回忆", "之前", "偏好")
        action = ("fix", "implement", "compare", "verify", "audit", "research", "修复", "实现", "比较", "确认", "审计")
        if any(marker in lowered for marker in explicit):
            base = 0.90
        elif any(marker in lowered for marker in action) or "?" in query or "？" in query:
            base = 0.78
        elif len(query) >= 24:
            base = 0.70
        else:
            base = 0.45
        entities = ProactiveRecallService._entities(" ".join([query, *turns]))
        return min(1.0, base + min(0.10, len(entities) * 0.02))

    @staticmethod
    def _entities(text: str) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for match in _TOKEN_RE.finditer(_bounded_text(text, 6_000)):
            token = match.group(0).casefold()[:64]
            if token in _STOP_TERMS or token in seen:
                continue
            seen.add(token)
            result.append(token)
            if len(result) >= 24:
                break
        return result

    @staticmethod
    def _redact_sensitive_summary(text: str) -> str:
        return bounded_redacted_text(text, max_chars=_MAX_TURN_SUMMARY_CHARS)

    @classmethod
    def _recall_query(cls, query: str, turns: list[str]) -> str:
        entities = cls._entities(" ".join(turns))
        if not entities:
            return query
        return _bounded_text(f"{query}\nContext entities: {' '.join(entities)}", _MAX_QUERY_CHARS)

    def _is_control(
        self, *, channel: str, scope: Mapping[str, Any], session_id: str,
        query_digest: str, policy_version: str,
    ) -> bool:
        if self.control_percent <= 0:
            return False
        digest = sha256(
            "\x1f".join([channel, *_scope_tuple(scope), session_id, query_digest, policy_version]).encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:2], "big") % 100 < self.control_percent

    @staticmethod
    def _citation(decision_id: str, record: RecordEnvelope, rank: int) -> str:
        digest = sha256(
            f"{decision_id}\x1f{record.record_id}\x1f{record.source_id}\x1f{rank}".encode("utf-8")
        ).hexdigest()[:20]
        return f"pm:{digest}"

    @staticmethod
    def _record_text(record: RecordEnvelope) -> str:
        return _bounded_text(record.content.get("text") or record.summary or record.detail or record.title, 1_200)

    @staticmethod
    def _is_hard_policy(record: RecordEnvelope) -> bool:
        if record.kind == "rule":
            return True
        markers = {str(tag or "").strip().casefold() for tag in record.tags}
        meta = record.meta if isinstance(record.meta, Mapping) else {}
        return bool(
            markers & {"safety", "security", "hard_policy", "mandatory"}
            or meta.get("hard_policy") is True
            or meta.get("mandatory_context") is True
        )

    def _render_context(self, items: list[dict[str, Any]]) -> str:
        if not items:
            return ""
        header = '<eimemory_proactive_context trust="untrusted-data">\n'
        footer = "</eimemory_proactive_context>"
        lines = [header]
        remaining = self.max_context_chars - len(header) - len(footer)
        for item in items:
            payload = {
                "citation": item["citation"], "source_id": item["source_id"],
                "title": item["title"], "text": item["text"],
            }
            line = self._safe_json(payload) + "\n"
            if len(line) > remaining:
                payload["text"] = _bounded_text(payload["text"], max(0, remaining - 180))
                line = self._safe_json(payload) + "\n"
            if len(line) > remaining:
                break
            lines.append(line)
            remaining -= len(line)
        if len(lines) == 1:
            return ""
        lines.append(footer)
        return "".join(lines)[: self.max_context_chars]

    @staticmethod
    def _safe_json(value: Mapping[str, Any]) -> str:
        return (
            json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
        )

    def _decision(self, *, query_id: str, decision_id: str) -> _DecisionState:
        normalized_decision = str(decision_id or "").strip()
        with self._lock:
            if normalized_decision:
                state = self._decisions.get(normalized_decision)
            else:
                matches = [state for state in self._decisions.values() if state.query_id == str(query_id or "").strip()]
                state = matches[0] if len(matches) == 1 else None
        if state is None and normalized_decision:
            payload = self.runtime.store.load_proactive_decision(normalized_decision)
            if payload is not None:
                state = self._state_from_payload(payload)
                with self._lock:
                    self._decisions[state.decision_id] = state
                    self._decisions.move_to_end(state.decision_id)
                    while len(self._decisions) > self.max_decisions:
                        self._decisions.popitem(last=False)
        if state is None:
            raise ValueError("exact proactive decision is required")
        return state

    def _transition_targets(self, state: _DecisionState, targets: dict[str, str]) -> int:
        if not targets:
            return 0
        feedback: dict[tuple[str, str], RecordEnvelope] = {}
        for citation, target in targets.items():
            item = state.items.get(citation)
            if item is None:
                raise ValueError("proactive citation does not belong to the exact decision")
            feedback[(citation, target)] = self._feedback_record(
                state,
                item,
                target,
                control_suppressed=state.control_cohort and not item.mandatory,
            )
        try:
            changed = self.runtime.store.transition_proactive_decision(
                state.decision_id,
                targets,
                feedback,
                expected={
                    "channel": state.channel,
                    "scope": state.scope,
                    "source_key": self._source_digest(state.source_ids),
                    "session_id": state.session_id,
                    "policy_version": state.policy_version,
                    "release_identity": state.release_identity,
                },
            )
        except Exception as exc:  # noqa: BLE001 - telemetry is advisory to host execution
            self._record_bypass(
                channel=state.channel,
                session_id=state.session_id,
                query_digest=sha256(state.query.encode("utf-8")).hexdigest(),
                reason=f"transition_{type(exc).__name__}",
            )
            return -1
        with self._lock:
            for item in changed:
                current = state.items.get(str(item["citation"]))
                if current is not None:
                    current.state = str(item["state"])
        return len(changed)

    def _feedback_record(
        self,
        state: _DecisionState,
        item: _DecisionItem,
        target: str,
        *,
        control_suppressed: bool,
    ) -> RecordEnvelope:
        transition_id = sha256(
            f"{state.decision_id}\x1f{item.citation}\x1f{item.record_id}\x1f{item.source_id}\x1f{target}"
            .encode("utf-8")
        ).hexdigest()
        record = self.runtime.record_memory_usage(
            query_id=state.query_id,
            scope=state.scope,
            used_record_ids=[item.record_id] if target == "used" else [],
            rejected_record_ids=[item.record_id] if target == "rejected" else [],
            query=state.query,
            source=f"{state.channel}.proactive_recall",
            source_id=item.source_id,
            proactive_state=target,
            session_id=state.session_id,
            transition_id=transition_id,
            policy_version=state.policy_version,
            release_identity=state.release_identity,
            control_cohort=state.control_cohort,
            control_suppressed=control_suppressed,
            citation=item.citation,
            decision_id=state.decision_id,
            record_id=item.record_id,
            pair_id=state.pair_id,
            meta={
                "confidence": item.confidence,
                "runtime_channel": state.channel,
                "source_allowlist": list(state.source_ids),
                "mandatory": item.mandatory,
                "release_bound": state.release_bound,
            },
            persist=False,
        )
        record.record_id = "fb_" + transition_id[:12]
        return record

    @staticmethod
    def _decision_payload(state: _DecisionState, *, query_digest: str) -> dict[str, Any]:
        return {
            "decision_id": state.decision_id,
            "channel": state.channel,
            "scope": dict(state.scope),
            "source_key": ProactiveRecallService._source_digest(state.source_ids),
            "source_ids": list(state.source_ids),
            "session_id": state.session_id,
            "turn_id": state.turn_id,
            "query_id": state.query_id,
            "query_digest": query_digest,
            "query": state.query,
            "policy_version": state.policy_version,
            "release_identity": dict(state.release_identity),
            "release_bound": state.release_bound,
            "control_cohort": state.control_cohort,
            "pair_id": state.pair_id,
        }

    @staticmethod
    def _state_from_payload(payload: Mapping[str, Any]) -> _DecisionState:
        items: dict[str, _DecisionItem] = {}
        for raw in payload.get("items") or []:
            if not isinstance(raw, Mapping):
                continue
            item = _DecisionItem(
                record_id=str(raw.get("record_id") or ""),
                source_id=str(raw.get("source_id") or "default"),
                citation=str(raw.get("citation") or ""),
                confidence=float(raw.get("confidence") or 0.0),
                state=str(raw.get("state") or "volunteered"),
                mandatory=bool(raw.get("mandatory")),
            )
            items[item.citation] = item
        return _DecisionState(
            decision_id=str(payload.get("decision_id") or ""),
            query_id=str(payload.get("query_id") or ""),
            query=str(payload.get("query") or ""),
            channel=str(payload.get("channel") or ""),
            scope=dict(payload.get("scope") or {}),
            source_ids=tuple(str(item) for item in (payload.get("source_ids") or [])),
            session_id=str(payload.get("session_id") or ""),
            turn_id=str(payload.get("turn_id") or payload.get("query_id") or ""),
            policy_version=str(payload.get("policy_version") or ""),
            release_identity={key: str(value or "") for key, value in dict(payload.get("release_identity") or {}).items()},
            control_cohort=bool(payload.get("control_cohort")),
            pair_id=str(payload.get("pair_id") or ""),
            release_bound=bool(payload.get("release_bound")),
            items=items,
        )

    def _record_bypass(self, *, channel: str, session_id: str, query_digest: str, reason: str) -> None:
        payload = {
            "channel": _bounded_text(channel, 40),
            "session_digest": sha256(str(session_id).encode("utf-8")).hexdigest()[:16],
            "query_digest": str(query_digest)[:64],
            "reason": _bounded_text(reason, 80),
        }
        with self._lock:
            self._bypasses.append(payload)
        try:
            self.runtime.store.append_proactive_bypass(
                payload, max_entries=self._bypasses.maxlen or DEFAULT_MAX_BYPASS_DIAGNOSTICS
            )
        except Exception:
            return

    @staticmethod
    def _empty_decision(
        *, query_id: str, cache_key: str, release: Mapping[str, str], bypassed: bool
    ) -> dict[str, Any]:
        return {
            "ok": True, "bypassed": bypassed, "decision_id": "", "query_id": query_id,
            "cache_key": cache_key, "control_cohort": False,
            "release_identity": dict(release), "policy_version": PROACTIVE_POLICY_VERSION,
            "items": [], "suppressed_items": [], "context": "",
        }
