from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from threading import BoundedSemaphore, RLock, Thread
from time import monotonic
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
DEFAULT_CLOSE_TIMEOUT_SECONDS = 5.0
DEFAULT_STALE_DECISION_SECONDS = 900
DEFAULT_INJECTED_STALE_DECISION_SECONDS = 86_400
_MAX_TURNS_PER_SESSION = 4
_MAX_TURN_SUMMARY_CHARS = 1_000
_MAX_QUERY_CHARS = 8_000
_MAX_VOLUNTEERED_ITEMS = 3
_MAX_RECALL_WORKERS = 2
_TERMINAL_STATES = frozenset({"used", "not_used", "suppressed", "rejected"})
_TRANSITIONS = {
    "volunteered": frozenset({"injected", "not_used", "suppressed", "rejected"}),
    "injected": frozenset({"used", "not_used", "rejected"}),
    "used": frozenset({"rejected"}),
    "not_used": frozenset({"rejected"}),
    "suppressed": frozenset(),
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
    query_digest: str
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
        stale_decision_seconds: float = DEFAULT_STALE_DECISION_SECONDS,
        injected_stale_decision_seconds: float = DEFAULT_INJECTED_STALE_DECISION_SECONDS,
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
        self.stale_decision_seconds = max(60.0, min(86_400.0, float(stale_decision_seconds)))
        self.injected_stale_decision_seconds = max(
            self.stale_decision_seconds,
            min(604_800.0, max(900.0, float(injected_stale_decision_seconds))),
        )
        self._sessions: OrderedDict[tuple[Any, ...], _SessionState] = OrderedDict()
        self._decisions: OrderedDict[str, _DecisionState] = OrderedDict()
        self._candidate_cache: OrderedDict[str, _CachedRecall | tuple[RecordEnvelope, ...]] = OrderedDict()
        self._bypasses: deque[dict[str, str]] = deque(maxlen=max(1, min(512, int(max_bypass_diagnostics))))
        self._recall_slots = BoundedSemaphore(_MAX_RECALL_WORKERS)
        self._workers: set[Thread] = set()
        self._closing = False
        self._on_drained_called = False
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
        self.runtime.store.append_proactive_turn(
            {
                "channel": channel_id,
                "scope": exact_scope,
                "source_key": self._source_digest(sources),
                "session_id": normalized_session,
                "turn_id": normalized_turn,
                "turn_digest": sha256(safe_summary.encode("utf-8", errors="replace")).hexdigest(),
                "entity_digests": [
                    sha256(entity.encode("utf-8", errors="replace")).hexdigest()
                    for entity in entities
                ],
            },
            max_session_turns=_MAX_TURNS_PER_SESSION,
            max_global_turns=self.max_sessions * _MAX_TURNS_PER_SESSION,
        )
        key = self._session_key(channel_id, exact_scope, sources, normalized_session)
        with self._lock:
            state = self._session(key)
            retained = [item for item in state.turns if item[0] != normalized_turn]
            state.turns.clear()
            state.turns.extend(retained)
            state.turns.append((normalized_turn, " ".join(entities)))
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
        normalized_task_type = _bounded_text(task_type or "proactive.recall", 200)
        if not normalized_query or not normalized_query_id:
            raise ValueError("query and query_id are required")
        release = self._current_release(exact_scope, channel=channel_id)
        policy_version = self._policy_version()
        query_digest = sha256(normalized_query.encode("utf-8", errors="replace")).hexdigest()
        cache_key = self._cache_key(
            channel_id, exact_scope, sources,
            sha256(f"{query_digest}\x1f{normalized_task_type}".encode("utf-8")).hexdigest(),
            policy_version, release,
        )
        if not all(str(release.get(key) or "") for key in release):
            self._record_bypass(
                channel=channel_id,
                session_id=normalized_session,
                query_digest=query_digest,
                reason="release_identity_unavailable",
            )
            if recall_bundle is not None:
                return self.mandatory_fallback(
                    channel=channel_id, scope=exact_scope, source_ids=sources,
                    records=[*recall_bundle.items, *recall_bundle.rules],
                    query_id=normalized_query_id,
                    cache_key=cache_key, release=release,
                )
            return self._empty_decision(
                query_id=normalized_query_id,
                cache_key=cache_key,
                release=release,
                bypassed=True,
            )
        exact_existing = self.runtime.store.find_proactive_decision(
            {
                "channel": channel_id,
                "scope": exact_scope,
                "source_key": self._source_digest(sources),
                "session_id": normalized_session,
                "turn_id": normalized_query_id,
                "release_identity": release,
            }
        )
        if exact_existing is not None:
            stored_effective_digest = str(
                exact_existing.get("effective_query_digest") or query_digest
            )
            return self._persisted_decision_response(
                exact_existing,
                cache_key=self._cache_key(
                    channel_id, exact_scope, sources, stored_effective_digest,
                    policy_version, release,
                ),
                expected={
                    "channel": channel_id,
                    "scope": exact_scope,
                    "source_key": self._source_digest(sources),
                    "session_id": normalized_session,
                    "turn_id": normalized_query_id,
                    "query_id": normalized_query_id,
                    "query_digest": query_digest,
                    "task_type": normalized_task_type,
                    "policy_version": policy_version,
                    "release_identity": release,
                },
            )
        self.reconcile_stale(
            channel=channel_id,
            scope=exact_scope,
            source_ids=sources,
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
        rehydrated_turn_context = self._rehydrate_turn_context(
            channel=channel_id,
            exact_scope=exact_scope,
            source_ids=sources,
            session_id=normalized_session,
            persisted_turns=persisted_turns,
        )
        with self._lock:
            session = self._session(session_key)
            persisted_turn_ids = {str(item["turn_id"]) for item in persisted_turns}
            retained_turns = [item for item in session.turns if item[0] in persisted_turn_ids]
            session.turns.clear()
            session.turns.extend(retained_turns)
            turn_summaries = [summary for _turn_id, summary in session.turns]
        for context_terms in rehydrated_turn_context:
            if context_terms not in turn_summaries:
                turn_summaries.append(context_terms)
        turn_summaries = turn_summaries[-_MAX_TURNS_PER_SESSION:]
        if persisted_turns and not turn_summaries:
            self._record_bypass(
                channel=channel_id,
                session_id=normalized_session,
                query_digest=query_digest,
                reason="turn_context_unavailable_after_restart",
            )
        recall_query = self._recall_query(normalized_query, turn_summaries)
        effective_query_digest = sha256(
            f"{normalized_task_type}\x1f{recall_query}".encode("utf-8", errors="replace")
        ).hexdigest()
        cache_key = self._cache_key(
            channel_id, exact_scope, sources, effective_query_digest, policy_version, release
        )
        decision_id = "pd:" + sha256(
            "\x1f".join(
                [channel_id, *_scope_tuple(exact_scope), *sources, normalized_session,
                 normalized_query_id, query_digest, effective_query_digest,
                 normalized_task_type, policy_version,
                 *(str(release.get(key) or "") for key in sorted(release))]
            ).encode("utf-8")
        ).hexdigest()[:32]
        pair_id = "pp:" + sha256(
            "\x1f".join(
                [channel_id, *_scope_tuple(exact_scope), *sources,
                 effective_query_digest, normalized_task_type, policy_version,
                 *(str(release.get(key) or "") for key in sorted(release))]
            ).encode("utf-8")
        ).hexdigest()[:32]
        existing = self.runtime.store.load_proactive_decision(decision_id)
        if existing is not None:
            return self._persisted_decision_response(
                existing,
                cache_key=cache_key,
                expected={
                    "channel": channel_id,
                    "scope": exact_scope,
                    "source_key": self._source_digest(sources),
                    "session_id": normalized_session,
                    "turn_id": normalized_query_id,
                    "query_id": normalized_query_id,
                    "query_digest": query_digest,
                    "task_type": normalized_task_type,
                    "effective_query_digest": effective_query_digest,
                    "policy_version": policy_version,
                    "release_identity": release,
                    "pair_id": pair_id,
                },
            )
        with self._lock:
            cached = self._candidate_cache.get(cache_key)
            if cached is not None:
                self._candidate_cache.move_to_end(cache_key)
        cache_hit = cached is not None
        if recall_bundle is not None:
            # OpenClaw has already applied its authoritative policy/evidence
            # gates to this exact turn. Never replace that bundle with a cache.
            cached = None
            cache_hit = False
        if cached is None:
            try:
                bundle = recall_bundle or self._recall_with_timeout(
                    query=recall_query, scope=exact_scope,
                    source_ids=sources, task_type=normalized_task_type,
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
            unique_records: dict[tuple[str, str], RecordEnvelope] = {}
            for record in [*bundle.items, *bundle.rules]:
                unique_records.setdefault((record.record_id, record.source_id), record)
            cached = _CachedRecall(
                tuple(unique_records.values()), dict(bundle.explanation), float(bundle.confidence)
            )
            with self._lock:
                self._candidate_cache[cache_key] = cached
                self._candidate_cache.move_to_end(cache_key)
                while len(self._candidate_cache) > self.max_cache_entries:
                    self._candidate_cache.popitem(last=False)
        records, explanation, bundle_confidence = self._cached_parts(cached)
        if cache_hit:
            records = self._revalidate_cached_records(
                records,
                exact_scope=exact_scope,
                source_ids=sources,
            )
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
            limit=self.max_decisions * _MAX_VOLUNTEERED_ITEMS,
        )
        with self._lock:
            session = self._session(session_key)
            dedupe_refs = persisted_refs | session.volunteered_refs
            voluntary_candidates = [
                detail for detail in details
                if (detail[0].record_id, detail[0].source_id) not in dedupe_refs
                and (detail[0].record_id, detail[0].source_id) not in mandatory_refs
            ]
        mandatory_details = [(record, 1.0) for record in mandatory_records[:_MAX_VOLUNTEERED_ITEMS]]
        voluntary_details = voluntary_candidates[
            : max(0, _MAX_VOLUNTEERED_ITEMS - len(mandatory_details))
        ]
        control = self._is_control(
            channel=channel_id, scope=exact_scope, session_id=normalized_session,
            query_digest=query_digest, policy_version=policy_version,
        )
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
        mandatory_items = [item for item in public_items if item["mandatory"]]
        voluntary_items = [item for item in public_items if not item["mandatory"]]
        proposed_delivery = mandatory_items if control else public_items
        context, delivered_items = self._render_context_with_items(proposed_delivery)
        persisted_items = [*delivered_items, *(voluntary_items if control else [])]
        persisted_citations = {str(item["citation"]) for item in persisted_items}
        decision_items = {
            citation: item for citation, item in decision_items.items()
            if citation in persisted_citations
        }
        release_bound = all(str(release.get(key) or "") for key in release)
        state = _DecisionState(
            decision_id=decision_id,
            query_id=normalized_query_id,
            query=normalized_query,
            query_digest=query_digest,
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
        decision_payload = self._decision_payload(
            state, query_digest=query_digest, context=context,
            task_type=normalized_task_type,
            effective_query_digest=effective_query_digest,
        )
        public_by_citation = {str(item["citation"]): item for item in public_items}
        item_payloads = [
            {
                "citation": item.citation,
                "record_id": item.record_id,
                "source_id": item.source_id,
                "confidence": item.confidence,
                "state": item.state,
                "mandatory": item.mandatory,
                "order": index,
                "render_digest": self._render_snapshot_digest(
                    str(public_by_citation[item.citation].get("title") or ""),
                    str(public_by_citation[item.citation].get("text") or ""),
                ),
            }
            for index, item in enumerate(decision_items.values(), start=1)
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
            if recall_bundle is not None:
                return self.mandatory_fallback(
                    channel=channel_id,
                    scope=exact_scope,
                    source_ids=sources,
                    records=[*recall_bundle.items, *recall_bundle.rules],
                    query_id=normalized_query_id,
                    cache_key=cache_key,
                    release=release,
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
        injected_citations: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        state = self._decision(query_id=query_id, decision_id=decision_id)
        if not self._transition_namespace_matches(
            state, channel=channel, scope=scope, source_ids=source_ids, session_id=session_id,
            turn_id=turn_id, release_identity=release_identity,
        ):
            return {"ok": False, "error": "proactive_namespace_mismatch", "decision_id": state.decision_id, "changed": 0}
        requested = {str(value) for value in (injected_citations or ())}
        injectable = {
            citation for citation, item in state.items.items()
            if not state.control_cohort or item.mandatory
        }
        if not requested or not requested.issubset(injectable):
            return {
                "ok": False, "error": "proactive_injection_set_mismatch",
                "decision_id": state.decision_id, "changed": 0,
            }
        targets = {citation: "injected" for citation in requested}
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
        terminal_outcome: Mapping[str, Any] | None = None,
        _stale_lease_guard: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        del assistant_text  # Similarity is never evidence of use.
        state = self._decision(query_id=query_id, decision_id=decision_id)
        if not self._transition_namespace_matches(
            state, channel=channel, scope=scope, source_ids=source_ids, session_id=session_id,
            turn_id=turn_id, release_identity=release_identity,
        ):
            return {"ok": False, "error": "proactive_namespace_mismatch", "decision_id": state.decision_id, "changed": 0}
        outcome = (
            self._validated_terminal_outcome(terminal_outcome)
            if terminal_outcome
            else None
        )
        changed = self._transition_targets(
            state,
            {
                citation: (
                    "suppressed"
                    if state.control_cohort and not item.mandatory
                    else "not_used"
                )
                for citation, item in state.items.items()
                if item.state not in _TERMINAL_STATES
            },
            stale_lease_guard=_stale_lease_guard,
        )
        if changed == -2:
            return {
                "ok": True,
                "bypassed": False,
                "decision_id": state.decision_id,
                "changed": 0,
                "outcome_recorded": False,
                "reason": "stale_lease_renewed",
            }
        if changed < 0:
            return {
                "ok": False, "bypassed": True, "decision_id": state.decision_id,
                "changed": 0, "outcome_recorded": False,
            }
        outcome_recorded = False
        if outcome is not None:
            try:
                self.runtime.store.record_proactive_outcome(
                    state.decision_id,
                    outcome,
                    expected={
                        "channel": state.channel,
                        "scope": state.scope,
                        "source_key": self._source_digest(state.source_ids),
                        "session_id": state.session_id,
                        "policy_version": state.policy_version,
                        "release_identity": state.release_identity,
                    },
                )
                outcome_recorded = True
            except Exception as exc:  # noqa: BLE001 - telemetry remains fail-open
                self._record_bypass(
                    channel=state.channel,
                    session_id=state.session_id,
                    query_digest=state.query_digest,
                    reason=f"outcome_{type(exc).__name__}",
                )
                return {
                    "ok": False, "bypassed": True, "decision_id": state.decision_id,
                    "changed": max(0, changed), "outcome_recorded": False,
                }
        return {
            "ok": changed >= 0, "bypassed": changed < 0,
            "decision_id": state.decision_id, "changed": max(0, changed),
            "outcome_recorded": outcome_recorded,
        }

    def reconcile_stale(
        self,
        *,
        channel: str,
        scope: Mapping[str, Any],
        source_ids: Iterable[str] | None,
        limit: int = 64,
    ) -> dict[str, Any]:
        """Close expired authoritative decisions after a client-side crash.

        The SQLite decision ledger remains the only authority.  Client retry
        queues merely accelerate delivery; after the lease, a later service
        call deterministically converts every unfinished item to not-used (or
        control-suppressed) through the normal transition/feedback contract.
        """

        channel_id = normalize_runtime_channel(channel)
        exact_scope = resolve_channel_scope(channel_id, dict(scope))
        raw_sources = None if source_ids is None else tuple(source_ids)
        sources = ("*",) if raw_sources == ("*",) else _source_key(raw_sources)
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=self.stale_decision_seconds)
        ).isoformat()
        injected_cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=self.injected_stale_decision_seconds)
        ).isoformat()
        stale = self.runtime.store.list_stale_proactive_decisions(
            {
                "channel": channel_id,
                "scope": exact_scope,
                "source_key": self._source_digest(sources),
            },
            before_created_at=cutoff,
            before_injected_updated_at=injected_cutoff,
            limit=max(1, min(512, int(limit))),
        )
        closed = 0
        failed = 0
        skipped = 0
        for decision in stale:
            try:
                decision_sources = tuple(decision.get("source_ids") or sources)
                result = self.mark_terminal(
                    decision_id=str(decision.get("decision_id") or ""),
                    channel=str(decision.get("channel") or channel_id),
                    scope=dict(decision.get("scope") or exact_scope),
                    source_ids=None if decision_sources == ("*",) else list(decision_sources),
                    session_id=str(decision.get("session_id") or ""),
                    turn_id=str(decision.get("turn_id") or ""),
                    release_identity=dict(decision.get("release_identity") or {}),
                    terminal_outcome=None,
                    _stale_lease_guard={
                        "before_created_at": cutoff,
                        "before_injected_updated_at": injected_cutoff,
                    },
                )
                if result.get("reason") == "stale_lease_renewed":
                    skipped += 1
                elif result.get("ok") is True:
                    closed += 1
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001 - reconciliation is fail-open
                failed += 1
                self._record_bypass(
                    channel=channel_id,
                    session_id=str(decision.get("session_id") or "reconcile"),
                    query_digest=str(decision.get("query_digest") or ""),
                    reason=f"reconcile_{type(exc).__name__}",
                )
        return {
            "ok": failed == 0,
            "examined": len(stale),
            "closed": closed,
            "skipped": skipped,
            "failed": failed,
        }

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
                str(item.get("turn_digest") or "")
                for item in persisted
            ],
        }

    def _persisted_decision_response(
        self,
        payload: Mapping[str, Any],
        *,
        cache_key: str,
        expected: Mapping[str, Any],
    ) -> dict[str, Any]:
        for key, value in expected.items():
            if payload.get(key) != value:
                raise ValueError("persisted proactive decision identity conflict")
        public_items = self._rehydrate_persisted_items(payload, cache_key=cache_key)
        control = bool(payload.get("control_cohort"))
        proposed = [item for item in public_items if item["mandatory"]] if control else public_items
        context, delivered = self._render_context_with_items(proposed)
        suppressed = [item for item in public_items if not item["mandatory"]] if control else []
        return {
            "ok": True,
            "bypassed": False,
            "decision_id": str(payload.get("decision_id") or ""),
            "query_id": str(payload.get("query_id") or ""),
            "cache_key": str(cache_key),
            "control_cohort": control,
            "release_identity": dict(payload.get("release_identity") or {}),
            "release_bound": bool(payload.get("release_bound")),
            "policy_version": str(payload.get("policy_version") or ""),
            "pair_id": str(payload.get("pair_id") or ""),
            "items": delivered,
            "suppressed_items": suppressed,
            "context": context,
            "idempotent": True,
        }

    def _rehydrate_persisted_items(
        self,
        payload: Mapping[str, Any],
        *,
        cache_key: str,
    ) -> list[dict[str, Any]]:
        """Hydrate a bounded snapshot only from the exact authority namespace."""

        del cache_key  # SQLite exact refs are authority; cached bodies never resurrect deletion.
        exact_scope = dict(payload.get("scope") or {})
        allowed_sources = tuple(str(item) for item in (payload.get("source_ids") or []))
        hydrated: list[dict[str, Any]] = []
        for raw in list(payload.get("items") or [])[:_MAX_VOLUNTEERED_ITEMS]:
            if not isinstance(raw, Mapping):
                continue
            record_id = str(raw.get("record_id") or "")
            source_id = str(raw.get("source_id") or "default")
            if source_id not in allowed_sources and allowed_sources != ("*",):
                continue
            record = self.runtime.store.get_by_exact_ref(
                record_id,
                scope=exact_scope,
                source_id=source_id,
            )
            if record is None or not self._authorized(
                record,
                exact_scope=exact_scope,
                source_ids=allowed_sources,
            ):
                continue
            title = _bounded_text(record.title, 240)
            text = self._record_text(record)
            expected_digest = str(raw.get("render_digest") or "")
            if not expected_digest or self._render_snapshot_digest(title, text) != expected_digest:
                continue
            hydrated.append(
                {
                    "record_id": record_id,
                    "source_id": source_id,
                    "citation": str(raw.get("citation") or ""),
                    "confidence": float(raw.get("confidence") or 0.0),
                    "title": title,
                    "text": text,
                    "mandatory": bool(raw.get("mandatory")),
                }
            )
        return hydrated

    def _rehydrate_turn_context(
        self,
        *,
        channel: str,
        exact_scope: Mapping[str, Any],
        source_ids: tuple[str, ...],
        session_id: str,
        persisted_turns: list[Mapping[str, Any]],
    ) -> list[str]:
        """Recover terms from exact record refs, never from persisted raw turns."""

        entity_digests = {
            str(digest)
            for turn in persisted_turns
            for digest in (turn.get("entity_digests") or [])
            if str(digest)
        }
        if not entity_digests:
            return []
        refs = self.runtime.store.list_proactive_session_item_refs(
            {
                "channel": channel,
                "scope": dict(exact_scope),
                "source_key": self._source_digest(source_ids),
                "session_id": session_id,
            },
            limit=_MAX_TURNS_PER_SESSION * _MAX_VOLUNTEERED_ITEMS,
        )
        context: list[str] = []
        seen: set[tuple[str, str]] = set()
        for record_id, source_id in refs:
            ref = (record_id, source_id)
            if ref in seen:
                continue
            seen.add(ref)
            record = self.runtime.store.get_by_exact_ref(
                record_id,
                scope=dict(exact_scope),
                source_id=source_id,
            )
            if record is None or not self._authorized(
                record,
                exact_scope=exact_scope,
                source_ids=source_ids,
            ):
                continue
            terms = self._entities(f"{record.title} {self._record_text(record)}")
            if not any(
                sha256(term.encode("utf-8", errors="replace")).hexdigest() in entity_digests
                for term in terms
            ):
                continue
            context.append(" ".join(terms))
            if len(context) >= _MAX_TURNS_PER_SESSION:
                break
        return list(reversed(context))

    @staticmethod
    def _render_snapshot_digest(title: str, text: str) -> str:
        return sha256(
            json.dumps(
                {"text": str(text), "title": str(title)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8", errors="replace")
        ).hexdigest()

    def paired_metrics(
        self,
        *,
        scope: Mapping[str, Any],
        channel: str = "codex",
        source_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Report paired task effects only from explicit verified outcomes.

        Item-use telemetry remains diagnostic.  It is never used as a proxy for
        task success because control suppression would make that delta tautological.
        """

        if source_ids is None:
            return self._unavailable_paired_metrics("source_ids_required")
        channel_id = normalize_runtime_channel(channel)
        exact_scope = resolve_channel_scope(channel_id, dict(scope))
        sources = _source_key(source_ids)
        release = self._current_release(exact_scope, channel=channel_id)
        if not all(str(release.get(key) or "") for key in release):
            return self._unavailable_paired_metrics("release_identity_unavailable")
        decisions = self.runtime.store.list_proactive_outcomes(
            {
                "channel": channel_id,
                "scope": exact_scope,
                "source_key": self._source_digest(sources),
                "policy_version": self._policy_version(),
                **release,
            },
            limit=min(5_000, self.max_decisions),
        )
        usage = {
            "control": {state: 0 for state in ("used", "not_used", "suppressed", "rejected")},
            "treatment": {state: 0 for state in ("used", "not_used", "suppressed", "rejected")},
        }
        volunteered_count = 0
        injected_count = 0
        arms: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for decision in decisions:
            arm = "control" if bool(decision.get("control_cohort")) else "treatment"
            for item in decision.get("items") or []:
                volunteered_count += 1
                injected_count += int(bool(item.get("ever_injected")))
                state_name = str(item.get("state") or "")
                if state_name in usage[arm]:
                    usage[arm][state_name] += 1
            if (
                decision.get("outcome_verified") is True
                and isinstance(decision.get("outcome_success"), bool)
                and str(decision.get("pair_id") or "")
            ):
                arms.setdefault(str(decision["pair_id"]), {}).setdefault(arm, []).append(decision)
        pairs: list[dict[str, Any]] = []
        for pair_id, pair_arms in sorted(arms.items()):
            if not {"control", "treatment"}.issubset(pair_arms):
                continue
            control_samples = sorted(
                pair_arms["control"],
                key=lambda item: (str(item.get("created_at") or ""), str(item.get("decision_id") or "")),
            )
            treatment_samples = sorted(
                pair_arms["treatment"],
                key=lambda item: (str(item.get("created_at") or ""), str(item.get("decision_id") or "")),
            )
            for sample_index, (control, treatment) in enumerate(
                zip(control_samples, treatment_samples), start=1
            ):
                pairs.append(
                    {
                        "pair_id": pair_id,
                        "sample_index": sample_index,
                        "control_decision_id": str(control.get("decision_id") or ""),
                        "treatment_decision_id": str(treatment.get("decision_id") or ""),
                        "control_success": bool(control["outcome_success"]),
                        "treatment_success": bool(treatment["outcome_success"]),
                        "control_quality": control.get("outcome_quality"),
                        "treatment_quality": treatment.get("outcome_quality"),
                        "control_latency_ms": control.get("outcome_latency_ms"),
                        "treatment_latency_ms": treatment.get("outcome_latency_ms"),
                    }
                )
        pair_count = len(pairs)
        result: dict[str, Any] = {
            **usage,
            "volunteered_count": volunteered_count,
            "injected_count": injected_count,
            "effect_available": bool(pair_count),
            "reason": "" if pair_count else "verified_paired_outcomes_unavailable",
            "pair_count": pair_count,
            "pairs": pairs[:100],
            "used_rate_delta": None,
            "success_rate_delta": None,
            "quality_delta": None,
            "latency_ms_delta": None,
        }
        if pair_count:
            result["success_rate_delta"] = round(
                (
                    sum(item["treatment_success"] for item in pairs)
                    - sum(item["control_success"] for item in pairs)
                ) / pair_count,
                4,
            )
            quality_pairs = [
                item for item in pairs
                if isinstance(item["control_quality"], (int, float))
                and isinstance(item["treatment_quality"], (int, float))
            ]
            latency_pairs = [
                item for item in pairs
                if isinstance(item["control_latency_ms"], (int, float))
                and isinstance(item["treatment_latency_ms"], (int, float))
            ]
            if quality_pairs:
                result["quality_delta"] = round(
                    sum(item["treatment_quality"] - item["control_quality"] for item in quality_pairs)
                    / len(quality_pairs),
                    4,
                )
            if latency_pairs:
                result["latency_ms_delta"] = round(
                    sum(item["treatment_latency_ms"] - item["control_latency_ms"] for item in latency_pairs)
                    / len(latency_pairs),
                    4,
                )
        return result

    def bypass_diagnostics(self) -> list[dict[str, str]]:
        try:
            return self.runtime.store.list_proactive_bypasses(limit=self._bypasses.maxlen or 64)
        except Exception:
            with self._lock:
                return [dict(item) for item in self._bypasses]

    @staticmethod
    def _unavailable_paired_metrics(reason: str) -> dict[str, Any]:
        empty = {state: 0 for state in ("used", "not_used", "suppressed", "rejected")}
        return {
            "control": dict(empty), "treatment": dict(empty),
            "volunteered_count": 0, "injected_count": 0,
            "effect_available": False, "reason": str(reason), "pair_count": 0,
            "pairs": [], "used_rate_delta": None, "success_rate_delta": None,
            "quality_delta": None, "latency_ms_delta": None,
        }

    @staticmethod
    def _validated_terminal_outcome(value: Mapping[str, Any]) -> dict[str, Any]:
        if value.get("verified") is not True:
            raise ValueError("proactive task outcome must be explicitly verified")
        if not isinstance(value.get("success"), bool):
            raise ValueError("verified proactive task outcome success must be boolean")
        normalized: dict[str, Any] = {
            "verified": True,
            "success": bool(value["success"]),
        }
        quality = value.get("quality")
        if quality is not None:
            if isinstance(quality, bool) or not isinstance(quality, (int, float)):
                raise ValueError("proactive task outcome quality must be numeric")
            quality_value = float(quality)
            if not 0.0 <= quality_value <= 1.0:
                raise ValueError("proactive task outcome quality must be within 0..1")
            normalized["quality"] = quality_value
        latency = value.get("latency_ms")
        if latency is not None:
            if isinstance(latency, bool) or not isinstance(latency, (int, float)):
                raise ValueError("proactive task outcome latency_ms must be numeric")
            latency_value = float(latency)
            if latency_value < 0.0:
                raise ValueError("proactive task outcome latency_ms must be non-negative")
            normalized["latency_ms"] = latency_value
        return normalized

    def close(
        self,
        *,
        on_drained: Any | None = None,
        timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        """Stop admission and close only after workers drain within a bounded wait."""

        with self._lock:
            self._closing = True
            workers = tuple(self._workers)
        deadline = monotonic() + max(0.0, min(60.0, float(timeout_seconds)))
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - monotonic()))
        with self._lock:
            alive = tuple(worker for worker in self._workers if worker.is_alive())
            should_close = on_drained is not None and not self._on_drained_called and not alive
            if should_close:
                self._on_drained_called = True
        if alive:
            raise TimeoutError("proactive recall workers did not drain before shutdown timeout")
        if should_close:
            on_drained()

    def mandatory_fallback(
        self,
        *,
        channel: str,
        scope: Mapping[str, Any],
        source_ids: Iterable[str] | None,
        records: Iterable[RecordEnvelope],
        query_id: str,
        cache_key: str = "",
        release: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Render only hard policy on advisory failure, without telemetry."""

        channel_id = normalize_runtime_channel(channel)
        exact_scope = resolve_channel_scope(channel_id, dict(scope))
        sources = _source_key(source_ids)
        transient_id = "pf:" + sha256(
            "\x1f".join([channel_id, *_scope_tuple(exact_scope), *sources, str(query_id)]).encode("utf-8")
        ).hexdigest()[:32]
        items: list[dict[str, Any]] = []
        for rank, record in enumerate(records, start=1):
            if len(items) >= _MAX_VOLUNTEERED_ITEMS:
                break
            if not self._authorized(record, exact_scope=exact_scope, source_ids=sources):
                continue
            if not self._is_hard_policy(record):
                continue
            items.append(
                {
                    "record_id": record.record_id,
                    "source_id": record.source_id,
                    "citation": self._citation(transient_id, record, rank),
                    "confidence": 1.0,
                    "title": _bounded_text(record.title, 240),
                    "text": self._record_text(record),
                    "mandatory": True,
                }
            )
        context, rendered_items = self._render_context_with_items(items)
        return {
            "ok": True,
            "bypassed": True,
            "decision_id": "",
            "query_id": str(query_id),
            "cache_key": str(cache_key),
            "control_cohort": False,
            "release_identity": dict(release or {}),
            "release_bound": False,
            "policy_version": self._policy_version(),
            "items": rendered_items,
            "suppressed_items": [],
            "context": context,
            "reason": "mandatory_policy_fallback",
        }

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
        with self._lock:
            if self._closing:
                raise RuntimeError("proactive recall is closing")
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
            if self._closing:
                self._recall_slots.release()
                raise RuntimeError("proactive recall is closing")
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

    def _revalidate_cached_records(
        self,
        records: tuple[RecordEnvelope, ...],
        *,
        exact_scope: Mapping[str, Any],
        source_ids: tuple[str, ...],
    ) -> tuple[RecordEnvelope, ...]:
        """Resolve every cache hit through the current authority namespace.

        Candidate objects are only latency hints.  Deletion, revocation and
        content replacement in SQLite take effect before another session can
        consume a cached result.
        """

        current: list[RecordEnvelope] = []
        for candidate in records[:8]:
            record = self.runtime.store.get_by_exact_ref(
                candidate.record_id,
                scope=dict(exact_scope),
                source_id=candidate.source_id,
            )
            if record is not None and self._authorized(
                record,
                exact_scope=exact_scope,
                source_ids=source_ids,
            ):
                current.append(record)
        return tuple(current)

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
        context, _rendered = self._render_context_with_items(items)
        return context

    def _render_context_with_items(
        self, items: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        if not items:
            return "", []
        header = '<eimemory_proactive_context trust="untrusted-data">\n'
        footer = "</eimemory_proactive_context>"
        lines = [header]
        rendered: list[dict[str, Any]] = []
        remaining = self.max_context_chars - len(header) - len(footer)
        for item in items:
            payload = {
                "citation": item["citation"], "source_id": item["source_id"],
                "title": _bounded_text(item["title"], 80), "text": item["text"],
            }
            line = self._safe_json(payload) + "\n"
            if len(line) > remaining:
                original_text = str(payload["text"])
                payload["text"] = ""
                line = self._safe_json(payload) + "\n"
                if len(line) > remaining:
                    payload["title"] = ""
                    line = self._safe_json(payload) + "\n"
                if len(line) <= remaining:
                    low, high = 0, len(original_text)
                    best = line
                    while low <= high:
                        middle = (low + high) // 2
                        payload["text"] = original_text[:middle]
                        candidate = self._safe_json(payload) + "\n"
                        if len(candidate) <= remaining:
                            best = candidate
                            low = middle + 1
                        else:
                            high = middle - 1
                    line = best
            if len(line) > remaining:
                break
            lines.append(line)
            rendered.append(item)
            remaining -= len(line)
        if len(lines) == 1:
            return "", []
        lines.append(footer)
        return "".join(lines)[: self.max_context_chars], rendered

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
        if not normalized_decision:
            raise ValueError("decision_id is required for proactive state transitions")
        with self._lock:
            state = self._decisions.get(normalized_decision)
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

    def _transition_targets(
        self,
        state: _DecisionState,
        targets: dict[str, str],
        *,
        stale_lease_guard: Mapping[str, str] | None = None,
    ) -> int:
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
                stale_lease_guard=(
                    None if stale_lease_guard is None else dict(stale_lease_guard)
                ),
            )
        except Exception as exc:  # noqa: BLE001 - telemetry is advisory to host execution
            self._record_bypass(
                channel=state.channel,
                session_id=state.session_id,
                query_digest=state.query_digest,
                reason=f"transition_{type(exc).__name__}",
            )
            return -1
        if changed is None:
            return -2
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
            query="",
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
                "query_digest": state.query_digest,
            },
            persist=False,
        )
        record.record_id = "fb_" + transition_id[:12]
        return record

    @staticmethod
    def _decision_payload(
        state: _DecisionState,
        *,
        query_digest: str,
        context: str,
        task_type: str,
        effective_query_digest: str,
    ) -> dict[str, Any]:
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
            "task_type": str(task_type),
            "effective_query_digest": str(effective_query_digest),
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
            query_digest=str(payload.get("query_digest") or ""),
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
