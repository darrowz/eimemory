from __future__ import annotations

import json
import math
import os
import re
from hashlib import sha256
from time import perf_counter
from typing import Any

from eimemory.api.runtime import Runtime
from eimemory.identity import hongtu_identity_meta, hongtu_scope
from eimemory.governance.evidence_contract import current_release_identity, release_identity_payload
from eimemory.metadata import business_metadata
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.persona.correction import persona_feedback_from_user_text
from eimemory.persona.prompt import build_persona_guidance, disabled_persona_guidance, persona_enabled
from eimemory.persona.schema import PersonaTraceEvent
from eimemory.persona.store import PersonaStore
from eimemory.ops import openclaw_loop


DEFAULT_RECALL_MODE = "fast"
DEFAULT_RECALL_BUDGET_MS = 800
DEFAULT_FAST_CANDIDATE_LIMIT = 24
DEFAULT_FAST_QUERY_SCOPE_LIMIT = 8
DEFAULT_INJECTION_TOKEN_BUDGET = 1800


def _is_unexecuted_verification_state(value: Any) -> bool:
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).split())
    prefixes = ("not run", "not executed", "skipped", "skip", "unavailable", "unknown", "missing", "uncertain")
    return any(normalized == prefix or normalized.startswith(prefix + " ") for prefix in prefixes)


def _int_env(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, "") or "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


class OpenClawMemoryHooks:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def on_message_received(self, event: dict) -> dict:
        message = dict(event.get("message") or {})
        if str(message.get("role") or "").lower() != "user":
            return {"stored": None, "persona_feedback": None}
        text = self._clean_user_memory_text(str(message.get("content") or "").strip())
        persona_feedback = self._record_persona_feedback(event=event, text=text)
        if self._should_capture_message(text=text, event=event):
            scope = self._scope_from_event(event)
            idempotency_key = self._message_idempotency_key(event=event, text=text)
            if idempotency_key:
                existing = self.runtime.store.get_by_idempotency_key(
                    kinds=["memory"],
                    scope=scope,
                    idempotency_key=idempotency_key,
                )
                if existing is not None:
                    return {"stored": existing.to_dict(), "persona_feedback": persona_feedback}
            meta = self._identity_meta(event, organ="cognition", modality="text")
            if idempotency_key:
                meta["idempotency_key"] = idempotency_key
            stored = self.runtime.memory.ingest(
                text=text,
                memory_type="conversation",
                title="OpenClaw user message",
                scope=scope,
                source="openclaw.message_received",
                force_capture=self._force_capture_requested(event),
                meta=meta,
            )
            if stored.status == "rejected":
                return {"stored": None, "rejected": stored.to_dict(), "persona_feedback": persona_feedback}
            return {"stored": stored.to_dict(), "persona_feedback": persona_feedback}
        return {"stored": None, "persona_feedback": persona_feedback}

    def _message_idempotency_key(self, *, event: dict, text: str) -> str:
        explicit = self._first_text(
            event.get("idempotency_key"),
            event.get("idempotencyKey"),
            event.get("message_id"),
            event.get("messageId"),
            event.get("event_id"),
            event.get("eventId"),
            (event.get("message") or {}).get("id") if isinstance(event.get("message"), dict) else "",
            (event.get("message") or {}).get("messageId") if isinstance(event.get("message"), dict) else "",
        )
        if explicit:
            return "openclaw.message_received:" + self._stable_hash(
                {
                    "session_id": self._session_id_from_event(event),
                    "message_id": explicit,
                }
            )[:24]
        if not text:
            return ""
        return ""

    def before_prompt_build(self, event: dict) -> dict:
        start = perf_counter()
        query = self._clean_prompt_query(str(event.get("query") or event.get("raw_query") or "").strip())
        recall_context = self._resolve_recall_context(event)
        loop_task = self._openclaw_loop_start(event=event, query=query)
        if loop_task:
            recall_context["openclaw_loop_task_id"] = loop_task.get("task_id", "")
        trace_context = self._trace_context_from_event(event, task_context=recall_context, query=query)
        recall_context["trace_context"] = trace_context
        event = dict(event)
        event["task_context"] = recall_context
        persona_guidance = self._build_persona_guidance_safely(event=event, query=query, task_context=recall_context)
        if persona_guidance.get("enabled"):
            recall_context["persona_guidance"] = persona_guidance
        if not query:
            recall_context["policy_suggestion_ids"] = []
            recall_context["policy_sources"] = []
            recall_context["matched_event_type"] = ""
            recall_context["selected_records"] = []
            policy_attribution = self._normalize_policy_attribution(recall_context)
            recall_context["policy_attribution"] = policy_attribution
            bundle = self._empty_bundle({"task_context": recall_context})
            injection_plan = self._build_injection_plan(bundle=bundle, task_context=recall_context)
            recall_context["injection_plan"] = injection_plan
            bundle.explanation["injection_plan"] = injection_plan
            bundle.explanation["persona_guidance"] = persona_guidance
            latency_ms = round((perf_counter() - start) * 1000.0, 3)
            recall_context["latency_ms"] = latency_ms
            bundle.explanation["latency_ms"] = latency_ms
            self._audit_prompt_recall(event=event, bundle=bundle, injected=False)
            persona_trace = self._record_persona_trace(
                event=event,
                persona_guidance=persona_guidance,
                injection_latency_ms=latency_ms,
            )
            return {
                "memory_bundle": bundle.to_dict(),
                "injection_plan": injection_plan,
                "usage_telemetry": self._usage_telemetry(bundle),
                "trace_context": trace_context,
                "task_context": recall_context,
                "policy_attribution": policy_attribution,
                "persona_guidance": persona_guidance,
                "persona_trace": persona_trace,
            }
        scope = self._scope_from_event(event)
        policy_search = self._run_policy_search_safely(
            query=query,
            scope=scope,
            context=recall_context,
            limit=5,
        )
        bundle = self._run_recall_safely(
            query=query,
            scope=scope,
            task_context=recall_context,
            limit=8,
        )
        self._merge_policy_search(bundle=bundle, policy_search=policy_search)
        self._apply_pre_answer_gates(bundle=bundle, query=query, scope=scope, task_context=recall_context)
        recall_context["policy_suggestion_ids"] = self._coerce_string_list(
            bundle.explanation.get("policy_suggestion_ids")
        )
        recall_context["policy_sources"] = self._coerce_string_list(bundle.explanation.get("policy_sources"))
        recall_context["matched_event_type"] = str(bundle.explanation.get("matched_event_type") or "")
        recall_context["selected_records"] = self._selected_records(bundle)
        policy_attribution = self._normalize_policy_attribution(recall_context)
        recall_context["policy_attribution"] = policy_attribution
        injection_plan = self._build_injection_plan(bundle=bundle, task_context=recall_context)
        recall_context["injection_plan"] = injection_plan
        bundle.explanation["injection_plan"] = injection_plan
        bundle.explanation["persona_guidance"] = persona_guidance
        latency_ms = round((perf_counter() - start) * 1000.0, 3)
        recall_context["latency_ms"] = latency_ms
        bundle.explanation["latency_ms"] = latency_ms
        self._audit_prompt_recall(event=event, bundle=bundle, injected=bool(bundle.items))
        persona_trace = self._record_persona_trace(
            event=event,
            persona_guidance=persona_guidance,
            injection_latency_ms=latency_ms,
        )
        return {
            "memory_bundle": bundle.to_dict(),
            "injection_plan": injection_plan,
            "usage_telemetry": self._usage_telemetry(bundle),
            "trace_context": trace_context,
            "task_context": recall_context,
            "policy_attribution": policy_attribution,
            "persona_guidance": persona_guidance,
            "persona_trace": persona_trace,
        }

    def on_agent_end(self, event: dict) -> dict:
        assistant_messages = event.get("assistant_messages") or self._assistant_messages_from_event(event)
        text = ""
        if assistant_messages:
            text = self._clean_agent_text(str(assistant_messages[-1].get("content") or "").strip())
        if not text:
            text = self._clean_agent_text(str((event.get("outcome") or {}).get("notes") or "").strip())
        scope = self._scope_from_event(event)
        outcome = dict(event.get("outcome") or {})
        incident = None
        if outcome.get("success") is False:
            incident = self.runtime.evolution.observe(
                signal_type="incident",
                payload={
                    "incident_type": "agent_end_failure",
                    "title": "OpenClaw agent failure",
                    "summary": str(outcome.get("notes") or "agent execution failed"),
                    "session_id": event.get("session_id", ""),
                },
                scope=scope,
            )
        stored = None
        if text and self._is_salient_agent_text(text):
            stored = self.runtime.memory.ingest(
                text=text,
                memory_type="conversation",
                title="OpenClaw agent outcome",
                scope=scope,
                source="openclaw.agent_end",
                meta=self._identity_meta(event, organ="cognition", modality="text"),
            )
        return {
            "stored": stored.to_dict() if stored else None,
            "incident": incident.to_dict() if incident else None,
            **self._record_terminal_memory(event, end_kind="agent_end", assistant_text=text),
        }

    def on_task_end(self, event: dict) -> dict:
        return self._record_terminal_memory(event, end_kind="task_end")

    def on_session_end(self, event: dict) -> dict:
        return self._record_terminal_memory(event, end_kind="session_end")

    def _should_capture_message(self, *, text: str, event: dict) -> bool:
        if not text:
            return False
        normalized = "".join(ch for ch in text if ch.isalnum() or ch.isspace()).strip().lower()
        if not normalized:
            return False
        if self._looks_like_prompt_injection(normalized):
            return False
        if self._force_capture_requested(event):
            return True
        durable_markers = (
            "remember",
            "prefer",
            "always",
            "never",
            "decision",
            "important",
            "rule",
            "long term memory",
            "durable",
            "capture memory",
            "记住",
            "偏好",
            "决定",
            "重要",
            "规则",
            "长期记忆",
            "eimemory",
        )
        return any(marker in normalized for marker in durable_markers)

    def _force_capture_requested(self, event: dict) -> bool:
        return bool(event.get("capture_memory") or event.get("captureMemory"))

    def _is_low_value_chatter(self, normalized: str) -> bool:
        compact = normalized.replace(" ", "")
        acknowledgements = {
            "ok",
            "okay",
            "yes",
            "no",
            "thanks",
            "thankyou",
            "done",
            "go",
            "continue",
            "好的",
            "好",
            "嗯",
            "恩",
            "行",
            "可以",
            "收到",
            "谢谢",
            "继续",
            "1",
            "2",
            "3",
        }
        if compact in acknowledgements:
            return True
        tokens = [token for token in normalized.split() if token]
        return len(compact) <= 8 and len(tokens) <= 2

    def _looks_like_prompt_injection(self, normalized: str) -> bool:
        injection_markers = (
            "ignore previous instructions",
            "ignore all previous instructions",
            "forget previous instructions",
            "forget all previous instructions",
            "disregard previous instructions",
            "reveal your system prompt",
            "show your system prompt",
            "developer message",
            "system prompt",
            "jailbreak",
            "do not obey",
            "bypass safety",
            "act as dan",
            "忽略之前",
            "忽略以上",
            "忘记之前",
            "系统提示词",
            "开发者消息",
            "越狱",
        )
        return any(marker in normalized for marker in injection_markers)

    def _clean_user_memory_text(self, text: str) -> str:
        cleaned = self._clean_prompt_query(text)
        if not cleaned:
            return ""
        return self._clean_agent_text(cleaned)

    def _scope_from_event(self, event: dict) -> dict:
        return hongtu_scope(
            {
                "tenant_id": event.get("tenant_id") or event.get("tenantId") or "default",
                "user_id": event.get("user_id") or event.get("userId") or "",
                "agent_id": event.get("agent_id") or event.get("agentId") or "main",
                "workspace_id": event.get("workspace_id") or event.get("workspaceId") or "",
            }
        )

    def _raw_scope_from_event(self, event: dict) -> dict:
        return {
            "tenant_id": str(event.get("tenant_id") or event.get("tenantId") or "default"),
            "agent_id": str(event.get("agent_id") or event.get("agentId") or "main"),
            "workspace_id": str(event.get("workspace_id") or event.get("workspaceId") or ""),
            "user_id": str(event.get("user_id") or event.get("userId") or ""),
        }

    def _identity_meta(self, event: dict, *, organ: str, modality: str) -> dict:
        return hongtu_identity_meta(
            source="openclaw.feishu",
            channel="feishu",
            hardware_node=str(event.get("hardware_node") or event.get("hardwareNode") or "honxin"),
            organ=organ,
            modality=modality,
            extra={
                "runtime_node": str(event.get("agent_id") or event.get("agentId") or "openclaw"),
                "official_channel": True,
            },
        )

    def _session_id_from_event(self, event: dict) -> str:
        return str(event.get("session_id") or event.get("sessionId") or "")

    def _resolve_recall_context(self, event: dict) -> dict:
        task_context = dict(event.get("task_context") or event.get("taskContext") or {})
        recall_mode = str(task_context.get("recall_mode") or "").strip().lower()
        if recall_mode == "deep":
            recall_mode = "raw_hybrid"
        elif recall_mode != "raw_hybrid":
            recall_mode = DEFAULT_RECALL_MODE
        task_context["recall_mode"] = recall_mode
        task_context["recall_budget_ms"] = self._coerce_recall_budget_ms(task_context.get("recall_budget_ms"))
        if recall_mode == "fast":
            task_context["candidate_limit"] = self._coerce_fast_candidate_limit(task_context.get("candidate_limit"))
            task_context["query_scope_limit"] = self._coerce_fast_query_scope_limit(
                task_context.get("query_scope_limit")
            )
        return task_context

    def _trace_context_from_event(self, event: dict, *, task_context: dict, query: str) -> dict:
        nested_trace_context = self._merged_dict(
            event.get("trace_context"),
            event.get("traceContext"),
            task_context.get("trace_context"),
            task_context.get("traceContext"),
        )
        task_type = self._first_text(
            event.get("task_type"),
            event.get("taskType"),
            task_context.get("task_type"),
            task_context.get("taskType"),
            nested_trace_context.get("task_type"),
            nested_trace_context.get("taskType"),
        )
        trace_id = self._first_text(
            event.get("trace_id"),
            event.get("traceId"),
            event.get("outcome_trace_id"),
            event.get("outcomeTraceId"),
            task_context.get("trace_id"),
            task_context.get("traceId"),
            nested_trace_context.get("trace_id"),
            nested_trace_context.get("traceId"),
        )
        idempotency_key = self._first_text(
            event.get("idempotency_key"),
            event.get("idempotencyKey"),
            task_context.get("idempotency_key"),
            task_context.get("idempotencyKey"),
            nested_trace_context.get("idempotency_key"),
            nested_trace_context.get("idempotencyKey"),
        )
        session_id = self._session_id_from_event(event)
        attempt_marker = self._first_text(
            event.get("task_id"),
            event.get("taskId"),
            event.get("turn_id"),
            event.get("turnId"),
            event.get("request_id"),
            event.get("requestId"),
            event.get("started_at"),
            event.get("startedAt"),
            task_context.get("task_id"),
            task_context.get("taskId"),
            task_context.get("turn_id"),
            task_context.get("turnId"),
            task_context.get("started_at"),
            task_context.get("startedAt"),
            nested_trace_context.get("task_id"),
            nested_trace_context.get("taskId"),
            nested_trace_context.get("turn_id"),
            nested_trace_context.get("turnId"),
            nested_trace_context.get("request_id"),
            nested_trace_context.get("requestId"),
            nested_trace_context.get("started_at"),
            nested_trace_context.get("startedAt"),
        )
        if not trace_id:
            trace_id = self._dedupe_trace_part("openclaw", session_id, task_type, attempt_marker, query)
        if not idempotency_key:
            idempotency_key = self._dedupe_trace_part("openclaw", "outcome", session_id, task_type, attempt_marker, query)
        return {
            **nested_trace_context,
            "trace_id": trace_id,
            "idempotency_key": idempotency_key,
            "task_type": task_type,
            "started_at": self._first_text(
                event.get("started_at"),
                event.get("startedAt"),
                task_context.get("started_at"),
                task_context.get("startedAt"),
                nested_trace_context.get("started_at"),
                nested_trace_context.get("startedAt"),
            ),
            "query": query,
        }

    def _dedupe_trace_part(self, *values: Any) -> str:
        parts = [str(value or "").strip() for value in values if str(value or "").strip()]
        return ":".join(parts)

    def _coerce_recall_budget_ms(self, value: object) -> int:
        try:
            budget = int(value)
        except (TypeError, ValueError):
            return DEFAULT_RECALL_BUDGET_MS
        if budget <= 0:
            return DEFAULT_RECALL_BUDGET_MS
        return budget

    def _coerce_injection_token_budget(self, value: object) -> int:
        try:
            budget = int(value)
        except (TypeError, ValueError):
            return DEFAULT_INJECTION_TOKEN_BUDGET
        if budget <= 0:
            return DEFAULT_INJECTION_TOKEN_BUDGET
        return max(8, min(8000, budget))

    def _coerce_fast_candidate_limit(self, value: object) -> int:
        try:
            candidate_limit = int(value)
        except (TypeError, ValueError):
            return DEFAULT_FAST_CANDIDATE_LIMIT
        return max(24, min(360, candidate_limit))

    def _coerce_fast_query_scope_limit(self, value: object) -> int:
        try:
            scope_limit = int(value)
        except (TypeError, ValueError):
            return DEFAULT_FAST_QUERY_SCOPE_LIMIT
        return max(3, min(32, scope_limit))

    def _run_recall_safely(self, *, query: str, scope: dict, task_context: dict, limit: int) -> RecallBundle:
        try:
            return self.runtime.memory.recall(
                query=query,
                scope=scope,
                task_context=task_context,
                limit=limit,
            )
        except Exception:
            return self._empty_bundle({"task_context": task_context, "query": query})

    def _run_policy_search_safely(self, *, query: str, scope: dict, context: dict, limit: int) -> dict:
        try:
            result = self.runtime.search_policy(
                query,
                scope=scope,
                context=context,
                limit=limit,
            )
        except Exception:
            return {"ok": False, "policy_suggestions": [], "matched_event_type": ""}
        return result if isinstance(result, dict) else {"ok": False, "policy_suggestions": [], "matched_event_type": ""}

    def _record_persona_feedback(self, *, event: dict, text: str) -> dict | None:
        normalized = "".join(ch for ch in text if ch.isalnum() or ch.isspace()).strip().lower()
        if not normalized or self._looks_like_prompt_injection(normalized):
            return None
        correction = persona_feedback_from_user_text(text)
        if correction is None:
            return None
        stored = PersonaStore(self.runtime.store).record_correction(
            correction,
            scope=self._scope_from_event(event),
            idempotency_key=self._persona_feedback_key(event=event, text=text, category=correction.category),
        )
        return {
            "stored": stored.to_dict(),
            "category": correction.category,
            "severity": correction.severity,
        }

    def _persona_feedback_key(self, *, event: dict, text: str, category: str) -> str:
        return "|".join(
            str(value or "").strip()
            for value in (
                event.get("event_id"),
                event.get("id"),
                event.get("message_id"),
                self._session_id_from_event(event),
                category,
                text,
            )
        )

    def _build_persona_guidance_safely(self, *, event: dict, query: str, task_context: dict) -> dict:
        start = perf_counter()
        if not persona_enabled():
            guidance = disabled_persona_guidance()
            guidance["duration_ms"] = round((perf_counter() - start) * 1000.0, 3)
            return guidance
        try:
            store = PersonaStore(self.runtime.store)
            state = store.load_state()
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            source_text = self._first_text(
                query,
                event.get("raw_query"),
                event.get("rawQuery"),
                event.get("prompt"),
                message.get("content"),
            )
            guidance = build_persona_guidance(
                text=source_text,
                state=state,
                recent_context=task_context,
                max_chars=_int_env("EIMEMORY_PERSONA_MAX_CHARS", 800),
            )
            payload = guidance.to_dict()
            payload["duration_ms"] = round((perf_counter() - start) * 1000.0, 3)
            return payload
        except Exception as exc:
            return {
                "enabled": False,
                "text": "",
                "scene": "",
                "risk_level": "",
                "tone": "",
                "error": str(exc),
                "duration_ms": round((perf_counter() - start) * 1000.0, 3),
            }

    def _record_persona_trace(self, *, event: dict, persona_guidance: dict, injection_latency_ms: float) -> dict:
        if not bool(persona_guidance.get("enabled")) and not persona_enabled():
            return {
                "stored": None,
                "enabled": False,
                "scene": str(persona_guidance.get("scene") or ""),
                "guidance_length": 0,
                "guidance_latency_ms": self._float_or_zero(persona_guidance.get("duration_ms")),
                "injection_latency_ms": self._float_or_zero(injection_latency_ms),
                "skipped_reason": "persona_disabled",
            }
        trace = PersonaTraceEvent(
            session_id=self._session_id_from_event(event),
            scene=str(persona_guidance.get("scene") or ""),
            guidance_length=len(str(persona_guidance.get("text") or "")),
            guidance_latency_ms=self._float_or_zero(persona_guidance.get("duration_ms")),
            injection_latency_ms=self._float_or_zero(injection_latency_ms),
            enabled=bool(persona_guidance.get("enabled")),
        )
        try:
            stored = PersonaStore(self.runtime.store).record_trace(
                trace,
                scope=self._scope_from_event(event),
                idempotency_key=self._persona_trace_idempotency_key(
                    event=event,
                    persona_guidance=persona_guidance,
                    trace=trace,
                ),
            )
            return {"stored": stored.to_dict(), **trace.to_dict()}
        except Exception as exc:
            return {"stored": None, **trace.to_dict(), "error": str(exc)}

    def _persona_trace_idempotency_key(
        self,
        *,
        event: dict,
        persona_guidance: dict,
        trace: PersonaTraceEvent,
    ) -> str:
        return self._stable_hash(
            {
                "session_id": trace.session_id,
                "query": self._clean_prompt_query(str(event.get("query") or event.get("raw_query") or "").strip()),
                "raw_query": str(event.get("raw_query") or event.get("rawQuery") or event.get("query") or "").strip(),
                "scene": trace.scene,
                "enabled": trace.enabled,
                "guidance_length": trace.guidance_length,
                "guidance_text": str(persona_guidance.get("text") or ""),
            }
        )

    def _merge_policy_search(self, *, bundle: RecallBundle, policy_search: dict) -> None:
        suggestions = policy_search.get("policy_suggestions") if isinstance(policy_search, dict) else []
        if isinstance(suggestions, list) and suggestions:
            bundle.explanation["policy_suggestions"] = list(suggestions)
            bundle.explanation["policy_first"] = True
        else:
            existing = bundle.explanation.get("policy_suggestions")
            bundle.explanation["policy_suggestions"] = list(existing) if isinstance(existing, list) else []
            bundle.explanation["policy_first"] = bool(bundle.explanation["policy_suggestions"])
        bundle.explanation["policy_suggestion_ids"] = self._extract_policy_suggestion_ids(
            bundle.explanation["policy_suggestions"]
        )
        bundle.explanation["policy_sources"] = self._extract_policy_sources(
            bundle.explanation["policy_suggestions"]
        )
        matched_event_type = str(
            policy_search.get("matched_event_type")
            or bundle.explanation.get("matched_event_type")
            or ""
        )
        bundle.explanation["matched_event_type"] = matched_event_type

    def _apply_pre_answer_gates(self, *, bundle: RecallBundle, query: str, scope: dict, task_context: dict) -> None:
        gate = self._run_ground_truth_gate_safely(query=query, scope=scope)
        if gate.get("ok"):
            self._inject_ground_truth_rules(bundle=bundle, gate=gate, scope=scope)
            bundle.explanation["ground_truth_pre_answer_gate"] = gate
            task_context["ground_truth_pre_answer_gate"] = {
                "gate_required": bool(gate.get("gate_required")),
                "matched_rule_count": self._int_or_default(gate.get("matched_rule_count"), default=0),
                "record_id": str(gate.get("record_id") or ""),
            }
        evidence_gate = self._run_answer_evidence_gate_safely(bundle=bundle, task_context=task_context)
        bundle.explanation["answer_evidence_gate"] = dict(evidence_gate.get("evidence_gate") or {})
        task_context["answer_evidence_gate"] = dict(evidence_gate.get("evidence_gate") or {})

    def _run_ground_truth_gate_safely(self, *, query: str, scope: dict) -> dict:
        try:
            result = self.runtime.build_ground_truth_pre_answer_gate(query=query, scope=scope, persist=True)
        except Exception:
            return {"ok": False, "error": "ground_truth_pre_answer_gate_failed"}
        return result if isinstance(result, dict) else {"ok": False, "error": "invalid_ground_truth_pre_answer_gate"}

    def _inject_ground_truth_rules(self, *, bundle: RecallBundle, gate: dict, scope: dict) -> None:
        seen = {record.record_id for record in [*bundle.items, *bundle.rules]}
        scope_ref = ScopeRef.from_dict(scope)
        for rule in gate.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            rule_id = str(rule.get("rule_id") or "")
            if not rule_id or rule_id in seen:
                continue
            record = self.runtime.store.get_by_id(rule_id, scope=scope_ref)
            if record is None:
                continue
            bundle.rules.append(record)
            bundle.items.insert(0, record)
            seen.add(rule_id)

    def _run_answer_evidence_gate_safely(self, *, bundle: RecallBundle, task_context: dict) -> dict:
        try:
            result = self.runtime.filter_answer_evidence(
                list(bundle.items),
                task_type=str(task_context.get("task_type") or task_context.get("intent") or ""),
            )
        except Exception as exc:
            return self._fail_closed_answer_evidence_gate(bundle=bundle, error=str(exc))
        if isinstance(result, dict) and result.get("ok") is True:
            bundle.items = [record for record in result.get("records") or [] if isinstance(record, RecordEnvelope)]
            return result
        return self._fail_closed_answer_evidence_gate(bundle=bundle, error="invalid_answer_evidence_gate")

    def _fail_closed_answer_evidence_gate(self, *, bundle: RecallBundle, error: str) -> dict:
        kept: list[RecordEnvelope] = []
        excluded: list[dict[str, Any]] = []
        for record in list(bundle.items):
            if self._record_requires_research_evidence(record):
                excluded.append(
                    {
                        "record_id": record.record_id,
                        "kind": record.kind,
                        "title": record.title,
                        "reason": "evidence_gate_unavailable",
                    }
                )
                continue
            kept.append(record)
        bundle.items = kept
        return {
            "ok": False,
            "evidence_gate": {
                "kept_count": len(kept),
                "excluded_count": len(excluded),
                "excluded": excluded,
                "error": "answer_evidence_gate_failed",
                "detail": str(error or ""),
            },
        }

    def _record_requires_research_evidence(self, record: RecordEnvelope) -> bool:
        kind = str(record.kind or "").lower()
        if kind in {"claim_card", "knowledge_candidate", "news", "paper_source", "paper_extract", "source_candidate"}:
            return True
        text = " ".join(
            str(value or "").lower()
            for value in (
                record.source,
                record.content.get("page_type") if isinstance(record.content, dict) else "",
                record.meta.get("page_type") if isinstance(record.meta, dict) else "",
                record.content.get("report_type") if isinstance(record.content, dict) else "",
                record.meta.get("report_type") if isinstance(record.meta, dict) else "",
            )
        )
        return kind == "knowledge_page" and any(marker in text for marker in ("research", "knowledge.synthesis", "daily_brief", "news", "rss", "paper"))

    def _audit_prompt_recall(self, *, event: dict, bundle: RecallBundle, injected: bool) -> RecordEnvelope:
        scope = ScopeRef.from_dict(self._scope_from_event(event))
        view = dict(bundle.explanation.get("recall_view") or {})
        all_injected_ids = [item.record_id for item in bundle.items]
        all_selected_records = self._selected_records(bundle)
        all_policy_suggestion_ids = self._coerce_string_list(bundle.explanation.get("policy_suggestion_ids"))
        all_policy_sources = self._coerce_string_list(bundle.explanation.get("policy_sources"))
        injected_ids = self._bounded_audit_string_list(all_injected_ids)
        selected_records = self._compact_selected_records_for_audit(all_selected_records)
        policy_suggestion_ids = self._bounded_audit_string_list(all_policy_suggestion_ids)
        policy_sources = self._bounded_audit_string_list(all_policy_sources)
        matched_event_type = self._bounded_audit_text(bundle.explanation.get("matched_event_type"), limit=256)
        injection_plan = self._coerce_injection_plan(bundle.explanation.get("injection_plan"))
        latency_ms = self._float_or_zero(bundle.explanation.get("latency_ms"))
        raw_query = str(event.get("raw_query") or event.get("rawQuery") or event.get("query") or "")
        task_context = dict(event.get("task_context") or event.get("taskContext") or {})
        persona_guidance = bundle.explanation.get("persona_guidance") or {}
        compact_injection_plan = self._compact_injection_plan_for_audit(injection_plan)
        compact_persona_guidance = self._compact_persona_guidance_for_audit(persona_guidance)
        source_composition = self._compact_count_mapping_for_audit(bundle.explanation.get("source_composition"))
        session_id = self._bounded_audit_text(self._session_id_from_event(event), limit=512)
        view_type = self._bounded_audit_text(view.get("view_type"), limit=256)
        raw_identity_meta = self._identity_meta(event, organ="cognition", modality="text")
        identity_meta = self._compact_identity_meta_for_audit(raw_identity_meta)
        content = {
            "session_id": session_id,
            "query": self._bounded_audit_text(
                self._clean_prompt_query(str(event.get("query") or event.get("raw_query") or "").strip())
            ),
            "raw_query": self._bounded_audit_text(raw_query),
            "raw_query_length": len(raw_query),
            "raw_query_sha256": self._stable_hash(raw_query),
            "task_context": self._compact_task_context_for_audit(task_context),
            "task_context_sha256": self._stable_hash(task_context),
            "selected_count": len(all_injected_ids),
            "injected": injected,
            "injected_record_ids": injected_ids,
            "injected_record_ids_sha256": self._stable_hash(all_injected_ids),
            "policy_suggestion_ids": policy_suggestion_ids,
            "policy_suggestion_ids_sha256": self._stable_hash(all_policy_suggestion_ids),
            "policy_sources": policy_sources,
            "policy_sources_sha256": self._stable_hash(all_policy_sources),
            "matched_event_type": matched_event_type,
            "selected_records": selected_records,
            "selected_records_sha256": self._stable_hash(all_selected_records),
            "source_composition": source_composition,
            "source_composition_sha256": self._stable_hash(bundle.explanation.get("source_composition") or {}),
            "injection_plan": compact_injection_plan,
            "injection_plan_sha256": self._stable_hash(injection_plan),
            "persona_guidance": compact_persona_guidance,
            "persona_guidance_sha256": self._stable_hash(persona_guidance),
            "injection_token_estimate": injection_plan["token_estimate"],
            "injection_lane_composition": dict(injection_plan["lane_composition"]),
            "injection_withheld_reasons": dict(compact_injection_plan["withheld_reasons"]),
            "latency_ms": latency_ms,
            "view_type": view_type,
            "confidence": bundle.confidence,
        }
        meta = {
            **identity_meta,
            "identity_meta_sha256": self._stable_hash(raw_identity_meta),
            "session_id": session_id,
            "selected_count": len(all_injected_ids),
            "selected_stored_count": len(injected_ids),
            "injected": injected,
            "policy_suggestion_ids": policy_suggestion_ids,
            "policy_sources": policy_sources,
            "matched_event_type": matched_event_type,
            "view_type": view_type,
            "source_composition": source_composition,
            "source_composition_sha256": content["source_composition_sha256"],
            "injection_token_estimate": injection_plan["token_estimate"],
            "injection_lane_composition": dict(injection_plan["lane_composition"]),
            "injection_withheld_reasons": dict(compact_injection_plan["withheld_reasons"]),
            "latency_ms": latency_ms,
            "persona_scene": str(compact_persona_guidance.get("scene") or ""),
            "persona_guidance_sha256": content["persona_guidance_sha256"],
        }
        record = RecordEnvelope.create(
            kind="recall_view",
            title="OpenClaw memory injection audit",
            summary=f"Injected {len(injected_ids)} memory records before prompt build",
            detail="Audit record for OpenClaw before_prompt_build memory recall.",
            content=content,
            tags=["openclaw", "before_prompt_build", "injection_audit"],
            source="openclaw.before_prompt_build",
            scope=scope,
            meta=meta,
        )
        record.record_id = self._prompt_audit_record_id(scope=scope, content=content)
        record.meta["idempotency_key"] = record.record_id
        record.content["idempotency_key"] = record.record_id
        existing = self.runtime.store.get_by_id(record.record_id, scope=scope)
        if existing is not None:
            stored = existing
        else:
            stored = self.runtime.store.append(record)
        raw_scope = ScopeRef.from_dict(self._raw_scope_from_event(event))
        if raw_scope != scope:
            raw_record = RecordEnvelope.create(
                kind="recall_view",
                title="OpenClaw memory injection audit",
                summary=f"Injected {len(injected_ids)} memory records before prompt build",
                detail="Audit record for OpenClaw before_prompt_build memory recall.",
                content=dict(content),
                tags=["openclaw", "before_prompt_build", "injection_audit"],
                source="openclaw.before_prompt_build",
                scope=raw_scope,
                meta=dict(meta),
            )
            raw_record.record_id = self._prompt_audit_record_id(scope=raw_scope, content=content)
            raw_record.meta["idempotency_key"] = raw_record.record_id
            raw_record.content["idempotency_key"] = raw_record.record_id
            if self.runtime.store.get_by_id(raw_record.record_id, scope=raw_scope) is None:
                self.runtime.store.append(raw_record)
        return stored

    def _prompt_audit_record_id(self, *, scope: ScopeRef, content: dict) -> str:
        selected_record_ids = [
            str(record.get("record_id") or "")
            for record in content.get("selected_records") or []
            if isinstance(record, dict)
        ]
        persona_guidance = content.get("persona_guidance") if isinstance(content.get("persona_guidance"), dict) else {}
        return "promptaudit_" + self._stable_hash(
            {
                "scope": self._scope_payload(scope),
                "session_id": str(content.get("session_id") or ""),
                "query": str(content.get("query") or ""),
                "raw_query": str(content.get("raw_query") or ""),
                "raw_query_sha256": str(content.get("raw_query_sha256") or ""),
                "task_context_sha256": str(content.get("task_context_sha256") or ""),
                "injection_plan_sha256": str(content.get("injection_plan_sha256") or ""),
                "injected": bool(content.get("injected")),
                "injected_record_ids": self._coerce_string_list(content.get("injected_record_ids")),
                "policy_suggestion_ids": self._coerce_string_list(content.get("policy_suggestion_ids")),
                "policy_sources": self._coerce_string_list(content.get("policy_sources")),
                "matched_event_type": str(content.get("matched_event_type") or ""),
                "selected_record_ids": selected_record_ids,
                "selected_records_sha256": str(content.get("selected_records_sha256") or ""),
                "view_type": str(content.get("view_type") or ""),
                "persona_enabled": bool(persona_guidance.get("enabled")),
                "persona_scene": str(persona_guidance.get("scene") or ""),
                "persona_guidance_length": len(str(persona_guidance.get("text") or "")),
                "persona_guidance_sha256": str(persona_guidance.get("text_sha256") or ""),
                "persona_guidance_full_sha256": str(content.get("persona_guidance_sha256") or ""),
            }
        )[:24]

    def _stable_hash(self, value: Any) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _bounded_audit_text(value: Any, *, limit: int = 4_096) -> str:
        return str(value or "")[: max(0, int(limit))]

    def _compact_task_context_for_audit(self, value: Any) -> dict[str, Any]:
        payload = value if isinstance(value, dict) else {}
        compact: dict[str, Any] = {}
        allowed_keys = (
            "task_type",
            "goal",
            "intent",
            "channel",
            "recall_view",
            "memory_view",
            "injection_mode",
            "injection_token_budget",
            "matched_event_type",
        )
        represented_keys: set[str] = set()
        for key in allowed_keys:
            item = payload.get(key)
            if isinstance(item, (str, int, float, bool)):
                compact[key] = self._bounded_audit_text(item)
                represented_keys.add(key)
        policy = self._normalize_policy_attribution(payload)
        if self._policy_attribution_has_values(policy):
            compact["policy_attribution"] = {
                "policy_suggestion_ids": self._coerce_string_list(policy.get("policy_suggestion_ids"))[:100],
                "policy_sources": self._coerce_string_list(policy.get("policy_sources"))[:100],
                "matched_event_type": self._bounded_audit_text(policy.get("matched_event_type"), limit=256),
                "selected_records": self._compact_selected_records_for_audit(policy.get("selected_records")),
            }
            represented_keys.add("policy_attribution")
        dropped_key_count = len(set(payload) - represented_keys)
        compact["input_key_count"] = len(payload)
        compact["dropped_key_count"] = dropped_key_count
        compact["fields_filtered"] = dropped_key_count > 0
        return compact

    def _compact_injection_plan_for_audit(self, value: dict[str, Any]) -> dict[str, Any]:
        if "entries" in value:
            entries = value.get("entries")
        else:
            entries = value.get("items")
        entries = entries if isinstance(entries, list) else []
        compact = {
            key: dict(item) if isinstance(item, dict) else item
            for key, item in value.items()
            if key not in {"entries", "items"}
        }
        compact["mode"] = self._bounded_audit_text(compact.get("mode"), limit=256)
        withheld = compact.get("withheld_reasons")
        if isinstance(withheld, dict):
            compact["withheld_reasons"] = {
                self._bounded_audit_text(key, limit=256): int(count)
                for key, count in list(withheld.items())[:100]
                if str(key).strip() and isinstance(count, (int, float))
            }
        return compact | {
            "entry_count": len(entries),
            "entries_sha256": self._stable_hash(entries),
        }

    def _compact_persona_guidance_for_audit(self, value: Any) -> dict[str, Any]:
        payload = value if isinstance(value, dict) else {}
        text = str(payload.get("text") or "")
        represented_keys = {key for key in ("enabled", "scene", "text") if key in payload}
        dropped_key_count = len(set(payload) - represented_keys)
        return {
            "enabled": bool(payload.get("enabled")),
            "scene": self._bounded_audit_text(payload.get("scene"), limit=256),
            "text": self._bounded_audit_text(text),
            "text_length": len(text),
            "text_sha256": self._stable_hash(text),
            "full_sha256": self._stable_hash(payload),
            "input_key_count": len(payload),
            "dropped_key_count": dropped_key_count,
            "fields_filtered": dropped_key_count > 0,
        }

    def _compact_selected_records_for_audit(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        keys = ("record_id", "kind", "title", "source", "recall_lane", "projection_type", "source_record_id")
        return [
            {
                key: self._bounded_audit_text(item.get(key), limit=512)
                for key in keys
                if str(item.get(key) or "").strip()
            }
            for item in value[:100]
            if isinstance(item, dict)
        ]

    def _bounded_audit_string_list(self, value: Any, *, limit: int = 100) -> list[str]:
        return [
            self._bounded_audit_text(item, limit=512)
            for item in self._coerce_string_list(value)[: max(0, int(limit))]
        ]

    def _compact_count_mapping_for_audit(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        compact: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:100]:
            key = self._bounded_audit_text(raw_key, limit=256)
            if isinstance(raw_value, dict):
                compact[key] = {
                    self._bounded_audit_text(nested_key, limit=256): nested_value
                    for nested_key, nested_value in list(raw_value.items())[:100]
                    if isinstance(nested_value, (int, float, bool))
                }
            elif isinstance(raw_value, (int, float, bool)):
                compact[key] = raw_value
        return compact

    def _compact_identity_meta_for_audit(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {
            self._bounded_audit_text(key, limit=128): (
                self._bounded_audit_text(item, limit=512) if isinstance(item, str) else item
            )
            for key, item in list(value.items())[:50]
            if isinstance(item, (str, int, float, bool))
        }

    def _scope_payload(self, scope: ScopeRef) -> dict[str, str]:
        return {
            "tenant_id": str(scope.tenant_id or "default"),
            "agent_id": str(scope.agent_id or ""),
            "workspace_id": str(scope.workspace_id or ""),
            "user_id": str(scope.user_id or ""),
        }

    def _usage_telemetry(self, bundle: RecallBundle) -> dict:
        injection_plan = self._coerce_injection_plan(bundle.explanation.get("injection_plan"))
        lane_composition = dict(injection_plan["lane_composition"])
        return {
            "selected_count": len(bundle.items),
            "confidence": bundle.confidence,
            "source_composition": dict(bundle.explanation.get("source_composition") or {}),
            "selected_records": self._selected_records(bundle),
            "latency_ms": self._float_or_zero(bundle.explanation.get("latency_ms")),
            "injection_token_estimate": injection_plan["token_estimate"],
            "injection_lane_composition": lane_composition,
            "injection_withheld_reasons": dict(injection_plan["withheld_reasons"]),
            "injection_plan": injection_plan,
            "injection": {
                "token_estimate": injection_plan["token_estimate"],
                "lane_composition": lane_composition,
                "withheld_reasons": dict(injection_plan["withheld_reasons"]),
                "full_text_count": lane_composition["full_text"],
                "summary_only_count": lane_composition["summary_only"],
                "policy_only_count": lane_composition["policy_only"],
                "withheld_count": lane_composition["withheld"],
            },
        }

    def _build_injection_plan(self, *, bundle: RecallBundle, task_context: dict) -> dict:
        mode = str(task_context.get("injection_mode") or task_context.get("injectionMode") or "strict").strip().lower()
        if mode not in {"strict", "balanced", "debug"}:
            mode = "strict"
        token_budget = self._coerce_injection_token_budget(
            self._first_present(
                task_context.get("injection_token_budget"),
                task_context.get("injectionTokenBudget"),
                task_context.get("max_injection_tokens"),
                task_context.get("maxInjectionTokens"),
                None,
            )
        )
        used_tokens = 0
        entries: list[dict[str, Any]] = []
        full_text_count = 0
        for record in self._injection_candidates(bundle):
            lane, reason = self._classify_injection_lane(record=record, full_text_count=full_text_count, mode=mode)
            token_estimate = self._injection_token_estimate(record=record, lane=lane)
            if lane != "withheld" and used_tokens + token_estimate > token_budget:
                lane = "withheld"
                reason = "context_token_budget"
                token_estimate = 0
            elif lane != "withheld":
                used_tokens += token_estimate
                if lane == "full_text":
                    full_text_count += 1
            entry = {
                "record_id": record.record_id,
                "kind": record.kind,
                "title": record.title,
                "source": record.source,
                "recall_lane": self._record_recall_lane(record),
                "lane": lane,
                "action": lane,
                "token_estimate": token_estimate,
            }
            memory_type = self._record_memory_type(record)
            if memory_type:
                entry["memory_type"] = memory_type
            if reason:
                entry["withheld_reason"] = reason
                entry["reason"] = reason
            entries.append(entry)
        lane_composition = {"full_text": 0, "summary_only": 0, "policy_only": 0, "withheld": 0}
        withheld_reasons: dict[str, int] = {}
        for entry in entries:
            lane = str(entry.get("lane") or "")
            if lane in lane_composition:
                lane_composition[lane] += 1
            reason = str(entry.get("withheld_reason") or "")
            if reason:
                withheld_reasons[reason] = withheld_reasons.get(reason, 0) + 1
        return {
            "mode": mode,
            "token_budget": token_budget,
            "token_estimate": sum(int(entry.get("token_estimate") or 0) for entry in entries),
            "lane_composition": lane_composition,
            "withheld_reasons": withheld_reasons,
            "full_text_count": lane_composition["full_text"],
            "summary_only_count": lane_composition["summary_only"],
            "policy_only_count": lane_composition["policy_only"],
            "withheld_count": lane_composition["withheld"],
            "entries": entries,
            "items": entries,
        }

    def _injection_candidates(self, bundle: RecallBundle) -> list[RecordEnvelope]:
        candidates: list[RecordEnvelope] = []
        seen: set[str] = set()
        for record in [*bundle.items, *bundle.rules, *bundle.reflections]:
            if record.record_id in seen:
                continue
            seen.add(record.record_id)
            candidates.append(record)
        return candidates

    def _classify_injection_lane(
        self,
        *,
        record: RecordEnvelope,
        full_text_count: int,
        mode: str,
    ) -> tuple[str, str]:
        operational_kinds = {
            "incident",
            "recall_view",
            "replay_result",
            "learning_loop",
            "learning_eval",
            "regression_watch",
            "feedback",
            "reflection",
            "unknown",
        }
        if record.kind in operational_kinds:
            if mode == "debug":
                return "summary_only", ""
            return "withheld", "operational_record"
        if record.kind in {"rule", "capability_candidate", "skill_candidate", "promotion_request"}:
            return "policy_only", ""
        if record.kind != "memory":
            return "summary_only", ""
        operational_memory_types = {
            "incident",
            "incident_report",
            "audit",
            "audit_record",
            "log",
            "run_log",
            "runtime_log",
            "diagnostic",
            "evolution_artifact",
        }
        if self._record_memory_type(record) in operational_memory_types:
            if mode == "debug":
                return "summary_only", ""
            return "withheld", "blocked_recall_lane"
        full_text_limit = 2 if mode == "strict" else 3
        if self._is_full_text_memory(record) and full_text_count < full_text_limit:
            return "full_text", ""
        return "summary_only", ""

    def _is_full_text_memory(self, record: RecordEnvelope) -> bool:
        memory_type = self._record_memory_type(record)
        if memory_type not in {"preference", "user_preference", "fact", "durable_fact"}:
            return False
        quality = business_metadata(record.meta).get("quality")
        quality = quality if isinstance(quality, dict) else {}
        tier = str(quality.get("quality_tier") or "").strip().lower()
        confidence = self._float_or_zero(quality.get("confidence"))
        salience = self._float_or_zero(quality.get("salience_score") or quality.get("importance"))
        return tier in {"core", "confirmed"} or confidence >= 0.85 or salience >= 0.85

    def _record_memory_type(self, record: RecordEnvelope) -> str:
        return str(
            business_metadata(record.meta).get("memory_type")
            or record.content.get("memory_type")
            or ""
        ).strip().lower()

    def _record_recall_lane(self, record: RecordEnvelope) -> str:
        memory_type = self._record_memory_type(record)
        aliases = {
            "preference": "user_preference",
            "user_preference": "user_preference",
            "rule": "system_rule",
            "system_rule": "system_rule",
            "fact": "durable_fact",
            "durable_fact": "durable_fact",
            "incident": "incident_report",
            "incident_report": "incident_report",
            "audit": "audit_record",
            "audit_record": "audit_record",
            "log": "run_log",
            "run_log": "run_log",
            "runtime_log": "run_log",
            "evolution_artifact": "evolution_artifact",
            "knowledge": "external_knowledge",
            "external_knowledge": "external_knowledge",
            "conversation": "task_context",
            "context": "task_context",
            "task_context": "task_context",
        }
        if memory_type in aliases:
            return aliases[memory_type]
        if record.kind == "rule":
            return "system_rule"
        if record.kind == "reflection":
            return self._reflection_recall_lane(record)
        if record.kind in {"incident"}:
            return "incident_report"
        if record.kind in {"recall_view", "feedback"}:
            return "audit_record"
        if record.kind in {"replay_result", "learning_eval", "capability_candidate", "promotion_request", "skill_candidate"}:
            return "evolution_artifact"
        if record.kind in {"knowledge_page", "claim_card", "paper_source", "paper_extract", "knowledge_unit"}:
            return "external_knowledge"
        if record.kind == "memory":
            return "durable_fact"
        return record.kind

    @staticmethod
    def _reflection_recall_lane(record: RecordEnvelope) -> str:
        meta = business_metadata(record.meta)
        content = record.content if isinstance(record.content, dict) else {}
        report_type = str(meta.get("report_type") or record.provenance.get("report_type") or content.get("report_type") or "").strip().lower()
        haystack = " ".join([report_type, str(record.source or ""), str(record.title or "")]).lower()
        if any(marker in haystack for marker in ("audit", "before_prompt_build", "injection")):
            return "audit_record"
        if "incident" in haystack:
            return "incident_report"
        if "outcome_trace" in haystack or "run_log" in haystack:
            return "run_log"
        if report_type:
            return "evolution_artifact"
        return "audit_record"

    def _injection_token_estimate(self, *, record: RecordEnvelope, lane: str) -> int:
        if lane == "withheld":
            return 0
        if lane == "full_text":
            text = self._first_text(record.content.get("text"), record.detail, record.summary, record.title)
        elif lane == "policy_only":
            text = self._first_text(
                " ".join(self._coerce_string_list(record.content.get("execution_policy"))),
                record.summary,
                record.title,
            )
        else:
            text = self._first_text(record.summary, record.title)
        return max(1, (len(text) + 3) // 4) if text else 0

    def _coerce_injection_plan(self, value: Any) -> dict:
        if isinstance(value, dict):
            lane_raw = value.get("lane_composition") or {}
            lane_composition = lane_raw if isinstance(lane_raw, dict) else {}
            entries_raw = value.get("entries") if "entries" in value else value.get("items", [])
            entries_raw = entries_raw if isinstance(entries_raw, list) else []
            entries = [dict(entry) for entry in entries_raw if isinstance(entry, dict)]
            token_budget = self._int_or_default(value.get("token_budget"), default=DEFAULT_INJECTION_TOKEN_BUDGET)
            if token_budget <= 0:
                token_budget = DEFAULT_INJECTION_TOKEN_BUDGET
            withheld_raw = value.get("withheld_reasons") or {}
            withheld_items = withheld_raw.items() if isinstance(withheld_raw, dict) else []
            withheld_reasons = {
                str(key): self._int_or_default(count, default=0)
                for key, count in withheld_items
                if str(key).strip()
            }
            return {
                "mode": str(value.get("mode") or "strict"),
                "token_budget": token_budget,
                "token_estimate": self._int_or_default(value.get("token_estimate"), default=0),
                "lane_composition": {
                    "full_text": self._int_or_default(lane_composition.get("full_text"), default=0),
                    "summary_only": self._int_or_default(lane_composition.get("summary_only"), default=0),
                    "policy_only": self._int_or_default(lane_composition.get("policy_only"), default=0),
                    "withheld": self._int_or_default(lane_composition.get("withheld"), default=0),
                },
                "withheld_reasons": withheld_reasons,
                "full_text_count": self._int_or_default(
                    self._first_present(value.get("full_text_count"), lane_composition.get("full_text"), 0),
                    default=0,
                ),
                "summary_only_count": self._int_or_default(
                    self._first_present(value.get("summary_only_count"), lane_composition.get("summary_only"), 0),
                    default=0,
                ),
                "policy_only_count": self._int_or_default(
                    self._first_present(value.get("policy_only_count"), lane_composition.get("policy_only"), 0),
                    default=0,
                ),
                "withheld_count": self._int_or_default(
                    self._first_present(value.get("withheld_count"), lane_composition.get("withheld"), 0),
                    default=0,
                ),
                "entries": entries,
                "items": entries,
            }
        return {
            "mode": "strict",
            "token_budget": DEFAULT_INJECTION_TOKEN_BUDGET,
            "token_estimate": 0,
            "lane_composition": {"full_text": 0, "summary_only": 0, "policy_only": 0, "withheld": 0},
            "withheld_reasons": {},
            "full_text_count": 0,
            "summary_only_count": 0,
            "policy_only_count": 0,
            "withheld_count": 0,
            "entries": [],
            "items": [],
        }

    def _selected_records(self, bundle: RecallBundle) -> list[dict]:
        selected = bundle.explanation.get("selected_records")
        if isinstance(selected, list):
            return [
                {
                    "record_id": str(item.get("record_id") or ""),
                    "kind": str(item.get("kind") or ""),
                    "title": str(item.get("title") or ""),
                    "source": str(item.get("source") or ""),
                    "recall_lane": str(item.get("recall_lane") or ""),
                    "projection_type": str(item.get("projection_type") or ""),
                    "source_record_id": str(item.get("source_record_id") or ""),
                }
                for item in selected
                if isinstance(item, dict)
            ]
        return [
            {
                "record_id": item.record_id,
                "kind": item.kind,
                "title": item.title,
                "source": item.source,
                "recall_lane": self._record_recall_lane(item),
                "projection_type": str(item.meta.get("projection_type") or ""),
                "source_record_id": str(
                    item.meta.get("source_record_id")
                    or item.provenance.get("source_record_id")
                    or item.content.get("source_record_id")
                    or ""
                ),
            }
            for item in bundle.items
        ]

    def _openclaw_loop_start(self, *, event: dict, query: str) -> dict:
        if os.environ.get("EIMEMORY_OPENCLAW_LOOP_DISABLED") == "1":
            return {}
        title = self._first_text(event.get("title"), event.get("task_title"), query, "OpenClaw user request")
        objective = self._first_text(event.get("goal"), event.get("objective"), query, title)
        try:
            task_context = self._merged_dict(event.get("task_context"), event.get("taskContext"))
            existing_task_id = self._first_text(
                task_context.get("openclaw_loop_task_id"),
                task_context.get("loop_task_id"),
                event.get("openclaw_loop_task_id"),
                event.get("loop_task_id"),
            )
            if existing_task_id:
                try:
                    existing_task = openclaw_loop.get_task(existing_task_id)
                except KeyError:
                    existing_task = {}
                if existing_task.get("status") in openclaw_loop.ACTIVE_STATUSES:
                    task = openclaw_loop.record_heartbeat(
                        existing_task_id,
                        lease_seconds=300,
                        progress="before_prompt_build",
                        source="openclaw.before_prompt_build",
                    )
                    task["reused"] = True
                    return task
            task = openclaw_loop.create_task(
                title=title[:160],
                objective=objective[:500],
                source="openclaw.before_prompt_build",
                owner=str(event.get("agent_id") or "openclaw"),
                report_policy=str(event.get("report_policy") or event.get("reportPolicy") or "on_done"),
                dedupe_key=self._openclaw_loop_dedupe_key(event=event, query=query),
            )
            openclaw_loop.record_heartbeat(
                str(task.get("task_id") or ""),
                lease_seconds=300,
                progress="before_prompt_build",
                source="openclaw.before_prompt_build",
            )
            return task
        except Exception as exc:
            return {"error": str(exc)}

    def _openclaw_loop_close(
        self,
        *,
        event: dict,
        task_context: dict,
        outcome: dict,
        result: str,
        verification: str,
        end_kind: str,
    ) -> dict:
        if os.environ.get("EIMEMORY_OPENCLAW_LOOP_DISABLED") == "1":
            return {}
        query = self._first_text(event.get("query"), event.get("raw_query"), task_context.get("query"), result, "OpenClaw task")
        task_id = self._first_text(
            task_context.get("openclaw_loop_task_id"),
            task_context.get("loop_task_id"),
            event.get("openclaw_loop_task_id"),
            event.get("loop_task_id"),
        )
        try:
            if not task_id:
                task = self._openclaw_loop_start(event=event, query=query)
                task_id = str(task.get("task_id") or "")
            if not task_id:
                return {"error": "missing_loop_task_id"}
            success = outcome.get("success")
            verified = outcome.get("verified")
            terminal_failure = self._classify_terminal_failure(event=event, outcome=outcome, result=result)
            passed = (
                not terminal_failure
                and success is not False
                and verified is not False
                and str(result or "").lower() not in {"bad", "failed", "failure"}
            )
            failure_reason = "" if passed else self._first_text(
                outcome.get("notes"),
                outcome.get("reason"),
                outcome.get("error"),
                event.get("error"),
                outcome.get("feedback"),
                result,
                verification,
                terminal_failure["failure_class"] if terminal_failure else "",
            )
            openclaw_loop.record_verification(
                task_id,
                verifier=f"openclaw.{end_kind}",
                checks={
                    "success": success,
                    "verified": verified,
                    "result": result,
                    "verification": verification,
                    "failure_class": terminal_failure["failure_class"] if terminal_failure else "",
                },
                passed=passed,
                failure_reason=failure_reason,
                next_action="report_done" if passed else "repair",
            )
            status = "done" if passed else "failed"
            return openclaw_loop.finish_task(
                task_id,
                status=status,
                summary=self._first_text(outcome.get("notes"), outcome.get("feedback"), result, status),
            )
        except Exception as exc:
            return {"error": str(exc), "task_id": task_id}

    def _openclaw_loop_dedupe_key(self, *, event: dict, query: str) -> str:
        explicit = self._first_text(
            event.get("idempotency_key"),
            event.get("idempotencyKey"),
            event.get("run_id"),
            event.get("runId"),
            event.get("trace_id"),
            event.get("traceId"),
            event.get("request_id"),
            event.get("requestId"),
            event.get("turn_id"),
            event.get("turnId"),
            event.get("task_id"),
            event.get("taskId"),
            event.get("message_id"),
            event.get("messageId"),
            event.get("event_id"),
            event.get("eventId"),
        )
        return "openclaw:" + self._stable_hash(
            {
                "session_id": self._session_id_from_event(event),
                "explicit": explicit,
                "query": "" if explicit else query,
            }
        )[:24]

    def _record_terminal_memory(self, event: dict, *, end_kind: str, assistant_text: str = "") -> dict:
        scope = self._scope_from_event(event)
        task_context = self._task_context_from_event(event)
        outcome = self._outcome_from_event(event)
        user_messages = self._user_messages_from_event(event)
        input_quality = self._terminal_input_quality(event, user_messages=user_messages)
        terminal_event = {**event, "input_quality": input_quality}
        correction = self._correction_from_event(
            terminal_event,
            outcome=outcome,
            user_messages=user_messages,
        )
        user_phrase = self._original_user_phrase(event, user_messages=user_messages, correction=correction)
        event_type = self._terminal_event_type(
            event,
            task_context=task_context,
            user_phrase=user_phrase,
            correction=correction,
        )
        interpreted_intent = self._terminal_interpreted_intent(
            event,
            task_context=task_context,
            event_type=event_type,
            user_phrase=user_phrase,
            correction=correction,
        )
        verification = self._terminal_verification(event, task_context=task_context, outcome=outcome)
        result = self._terminal_result(event, outcome=outcome, assistant_text=assistant_text)
        policy_update = self._terminal_policy_update(
            user_phrase=user_phrase,
            correction=correction,
            event_type=event_type,
            verification=verification,
            task_context=task_context,
        )
        action_path = self._terminal_action_path(event, task_context=task_context)
        tools = self._terminal_tools(event, task_context=task_context)
        evidence = self._terminal_evidence(
            event,
            outcome=outcome,
            verification=verification,
            result=result,
            correction=correction,
        )
        policy_attribution = self._resolve_policy_attribution(event=event, task_context=task_context)
        trace_query = self._clean_prompt_query(str(event.get("query") or event.get("raw_query") or "").strip())
        trace_context = self._trace_context_from_event(
            terminal_event,
            task_context=task_context,
            query=trace_query,
        )
        event_payload = {
            "source": f"openclaw.{end_kind}",
            "session_id": self._session_id_from_event(event),
            "hook": end_kind,
            "outcome_trace_id": trace_context["trace_id"],
            "outcome_trace_task_type": trace_context["task_type"],
            "policy_attribution": policy_attribution,
            "user_phrase": user_phrase,
            "event_type": event_type,
            "interpreted_intent": interpreted_intent,
            "goal": self._first_text(
                event.get("goal"),
                task_context.get("goal"),
                outcome.get("goal"),
                interpreted_intent,
            ),
            "constraints": self._merged_list(event.get("constraints"), task_context.get("constraints")),
            "physical_conditions": self._merged_dict(
                event.get("physical_conditions"),
                event.get("physicalConditions"),
                task_context.get("physical_conditions"),
                task_context.get("physicalConditions"),
            ),
            "environment": self._merged_dict(
                event.get("environment"),
                task_context.get("environment"),
                outcome.get("environment"),
            ),
            "tools": tools,
            "action_path": action_path,
            "result": result,
            "evidence": evidence,
            "verification": verification,
            "lesson": self._first_text(event.get("lesson"), task_context.get("lesson")),
            "next_policy": self._first_text(
                event.get("next_policy"),
                event.get("nextPolicy"),
                task_context.get("next_policy"),
                task_context.get("nextPolicy"),
                policy_update,
            ),
            "confidence": self._terminal_confidence(correction=correction, verification=verification, outcome=outcome),
            "notify_policy": self._first_text(event.get("notify_policy"), task_context.get("notify_policy")),
            "input_quality": input_quality,
        }
        if not input_quality["learnable"]:
            event_payload["confidence"] = 0.2
        release = current_release_identity(self.runtime, scope)
        if release is not None:
            event_payload.update(release_identity_payload(release))
            event_payload["evidence_class"] = "verified_real_task"
        terminal_key = self._terminal_idempotency_key(event=event, end_kind=end_kind)
        if terminal_key:
            event_payload["id"] = "evt_openclaw_" + self._stable_hash(
                {
                    "scope": scope,
                    "end_kind": end_kind,
                    "terminal_key": terminal_key,
                }
            )[:24]
            event_payload["idempotency_key"] = terminal_key
            event_payload["timestamp"] = self._first_text(event.get("started_at"), event.get("startedAt")) or event_payload.get("timestamp", "")
        recorded_event = self.runtime.record_event(event_payload, scope=scope)
        outcome_payload = self._terminal_outcome_payload(
            event=terminal_event,
            outcome=outcome,
            correction=correction,
            policy_update=policy_update,
            verification=verification,
            result=result,
            end_kind=end_kind,
            policy_attribution=policy_attribution,
        )
        if self._should_record_terminal_outcome(outcome_payload, end_kind=end_kind):
            recorded_outcome = self.runtime.record_outcome(recorded_event["id"], outcome_payload, scope=scope)
            outcome_trace = self._record_outcome_trace_safely(
                event=terminal_event,
                recorded_event_id=str(recorded_event.get("id") or ""),
                scope=scope,
                task_context=task_context,
                outcome=outcome,
                correction=correction,
                verification=verification,
                result=result,
                policy_attribution=policy_attribution,
                event_type=event_type,
                action_path=action_path,
                tools=tools,
                end_kind=end_kind,
            )
        else:
            recorded_outcome = {
                **outcome_payload,
                "outcome": "not_recorded",
                "event_id": recorded_event["id"],
                "recorded": False,
            }
            outcome_trace = {}
        pattern = None
        if correction:
            pattern_payload = self._intent_pattern_from_correction(
                user_phrase=user_phrase,
                correction=correction,
                event_type=event_type,
                interpreted_intent=interpreted_intent,
                policy_update=policy_update,
                verification=verification,
            )
            if pattern_payload:
                pattern = self.runtime.upsert_intent_pattern(pattern_payload, scope=scope)
        loop_task = self._openclaw_loop_close(
            event=event,
            task_context=task_context,
            outcome=outcome,
            result=result,
            verification=verification,
            end_kind=end_kind,
        )
        return {
            "event": recorded_event,
            "outcome": recorded_outcome,
            "pattern": pattern,
            "loop_task": loop_task,
            **outcome_trace,
        }

    def _should_record_terminal_outcome(self, outcome_payload: dict, *, end_kind: str) -> bool:
        return not (
            end_kind == "agent_end"
            and str(outcome_payload.get("outcome") or "") == "uncertain"
            and str(outcome_payload.get("reason") or "")
            == "agent_end_success_without_explicit_verification"
        )

    def _terminal_idempotency_key(self, *, event: dict, end_kind: str) -> str:
        explicit = self._first_text(
            event.get("idempotency_key"),
            event.get("idempotencyKey"),
            event.get("event_id"),
            event.get("eventId"),
            event.get("trace_id"),
            event.get("traceId"),
            event.get("task_id"),
            event.get("taskId"),
            event.get("turn_id"),
            event.get("turnId"),
            event.get("request_id"),
            event.get("requestId"),
        )
        if not explicit:
            return ""
        return f"openclaw.{end_kind}:" + self._stable_hash(
            {
                "session_id": self._session_id_from_event(event),
                "terminal_id": explicit,
            }
        )[:24]

    def _task_context_from_event(self, event: dict) -> dict:
        task_context = event.get("task_context") or event.get("taskContext") or {}
        return dict(task_context) if isinstance(task_context, dict) else {}

    def _outcome_from_event(self, event: dict) -> dict:
        outcome = event.get("outcome") or {}
        data = dict(outcome) if isinstance(outcome, dict) else {}
        if "success" not in data and "success" in event:
            data["success"] = event.get("success")
        if "notes" not in data and event.get("error"):
            data["notes"] = event.get("error")
        return data

    def _user_messages_from_event(self, event: dict) -> list[str]:
        values: list[Any] = []
        for key in ("user_messages", "userMessages"):
            raw = event.get(key)
            if isinstance(raw, list):
                values.extend(
                    item
                    for item in raw
                    if not isinstance(item, dict)
                    or not str(item.get("role") or "").strip()
                    or str(item.get("role") or "").strip().lower() == "user"
                )
        messages = event.get("messages")
        if isinstance(messages, list):
            values.extend(
                message
                for message in messages
                if isinstance(message, dict) and str(message.get("role") or "").lower() == "user"
            )
        message = event.get("message")
        if isinstance(message, dict) and str(message.get("role") or "").lower() == "user":
            values.append(message)
        explicit = self._first_text(
            event.get("user_phrase"),
            event.get("userPhrase"),
            event.get("query"),
            event.get("raw_query"),
            event.get("rawQuery"),
        )
        if explicit:
            values.insert(0, explicit)
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            content = value.get("content") if isinstance(value, dict) else value
            text = self._clean_user_memory_text(str(content or ""))
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        return cleaned

    def _correction_from_event(self, event: dict, *, outcome: dict, user_messages: list[str]) -> str:
        input_quality = event.get("input_quality") or {}
        if isinstance(input_quality, dict) and input_quality.get("learnable") is False:
            return ""
        named_corrections = [
            event.get("correction_from_user"),
            event.get("correctionFromUser"),
            outcome.get("correction_from_user"),
            outcome.get("correctionFromUser"),
            outcome.get("correction"),
        ]
        for value in named_corrections:
            text = self._clean_user_memory_text(str(value or ""))
            if text:
                return text
        feedback_values = [
            event.get("feedback"),
            event.get("user_feedback"),
            event.get("userFeedback"),
            outcome.get("feedback"),
            outcome.get("user_feedback"),
            outcome.get("userFeedback"),
        ]
        for value in [*feedback_values, *reversed(user_messages)]:
            text = self._clean_user_memory_text(str(value or ""))
            if text and self._looks_like_user_correction(text):
                return text
        return ""

    def _terminal_input_quality(self, event: dict, *, user_messages: list[str]) -> dict[str, Any]:
        reasons: list[str] = []
        if any(self._looks_like_mixed_role_transcript(text) for text in user_messages):
            reasons.append("mixed_role_transcript")
        confidence, confidence_present, confidence_valid = self._terminal_asr_confidence(event)
        if confidence_present and not confidence_valid:
            reasons.append("invalid_asr_confidence")
        elif confidence is not None and confidence < 0.5:
            reasons.append("low_asr_confidence")
        elif self._terminal_is_voice_input(event) and not confidence_present:
            reasons.append("missing_asr_confidence")
        if any(self._looks_like_mojibake(text) for text in user_messages):
            reasons.append("mojibake_or_noise")
        return {
            "learnable": not reasons,
            "status": "learnable" if not reasons else "diagnostic_only",
            "reasons": sorted(set(reasons)),
        }

    def _terminal_asr_confidence(self, event: dict) -> tuple[float | None, bool, bool]:
        task_context = event.get("task_context") or event.get("taskContext") or {}
        message = event.get("message") or {}
        metadata = message.get("metadata") if isinstance(message, dict) else {}
        containers = [event, task_context, metadata]
        for container in containers:
            if not isinstance(container, dict):
                continue
            for key in ("asr_confidence", "asrConfidence", "transcript_confidence", "transcriptConfidence"):
                if key not in container:
                    continue
                try:
                    value = float(container[key])
                except (TypeError, ValueError):
                    return None, True, False
                if not math.isfinite(value) or value < 0.0 or value > 100.0:
                    return None, True, False
                return (value / 100.0 if value > 1.0 else value), True, True
        return None, False, True

    def _terminal_is_voice_input(self, event: dict) -> bool:
        task_context = event.get("task_context") or event.get("taskContext") or {}
        message = event.get("message") or {}
        metadata = message.get("metadata") if isinstance(message, dict) else {}
        markers: list[str] = []
        for container in (event, task_context, message, metadata):
            if not isinstance(container, dict):
                continue
            for key in (
                "input_type",
                "inputType",
                "media_type",
                "mediaType",
                "message_type",
                "messageType",
                "msg_type",
                "msgType",
                "type",
                "source",
            ):
                if key in container:
                    markers.append(str(container.get(key) or "").strip().lower())
        return any(any(token in marker for token in ("audio", "voice", "speech", "asr")) for marker in markers)

    def _looks_like_mixed_role_transcript(self, text: str) -> bool:
        compact = " ".join(str(text or "").lower().split())
        assistant_marker = "assistant:" in compact or "assistant：" in compact or "助手：" in compact
        user_marker = "user:" in compact or "user：" in compact or "用户：" in compact
        return assistant_marker and user_marker

    def _looks_like_mojibake(self, text: str) -> bool:
        stripped = str(text or "").strip()
        visible_count = sum(1 for char in stripped if not char.isspace())
        if visible_count == 0:
            return False
        marker_count = stripped.count("?") + stripped.count("\ufffd")
        return marker_count >= 3 and marker_count / visible_count >= 0.35

    def _original_user_phrase(self, event: dict, *, user_messages: list[str], correction: str) -> str:
        explicit = self._first_text(event.get("original_user_phrase"), event.get("originalUserPhrase"))
        if explicit:
            return explicit
        for message in user_messages:
            if correction and message == correction:
                continue
            if not self._looks_like_user_correction(message):
                return message
        for message in user_messages:
            if message:
                return message
        return ""

    def _looks_like_user_correction(self, text: str) -> bool:
        normalized = "".join(ch for ch in str(text or "").lower() if ch.isalnum() or ch.isspace())
        compact = normalized.replace(" ", "")
        markers = (
            "没听到",
            "听不到",
            "登不上",
            "登录不上",
            "不是这个意思",
            "不是这个",
            "不是让",
            "不是要",
            "不对",
            "错了",
            "理解错",
            "搞错",
            "没成功",
            "不行",
            "打不开",
            "用不了",
            "失败了",
            "wrong",
            "notwhatimeant",
            "notwhatimean",
            "notthis",
            "cannothear",
            "canthear",
            "cannotlogin",
            "cantlogin",
            "didntwork",
            "doesntwork",
        )
        return any(marker in compact for marker in markers)

    def _terminal_event_type(self, event: dict, *, task_context: dict, user_phrase: str, correction: str) -> str:
        if self._looks_like_media_playback(user_phrase, correction):
            return "media_playback"
        return self._first_text(
            event.get("event_type"),
            event.get("eventType"),
            task_context.get("event_type"),
            task_context.get("eventType"),
            task_context.get("task_type"),
            task_context.get("taskType"),
            "communication",
        )

    def _terminal_interpreted_intent(
        self,
        event: dict,
        *,
        task_context: dict,
        event_type: str,
        user_phrase: str,
        correction: str,
    ) -> str:
        if event_type == "media_playback" and correction:
            return "播放音乐给用户听"
        inferred = self._first_text(
            event.get("true_intent"),
            event.get("trueIntent"),
            task_context.get("true_intent"),
            task_context.get("trueIntent"),
            event.get("interpreted_intent"),
            event.get("interpretedIntent"),
            task_context.get("interpreted_intent"),
            task_context.get("interpretedIntent"),
            task_context.get("intent"),
            task_context.get("goal"),
        )
        if inferred:
            return inferred
        if event_type == "media_playback":
            return "播放音乐给用户听"
        return user_phrase

    def _terminal_verification(self, event: dict, *, task_context: dict, outcome: dict) -> str:
        explicit = self._first_text(
            event.get("verification"),
            event.get("verification_method"),
            event.get("verificationMethod"),
            task_context.get("verification"),
            task_context.get("verification_method"),
            task_context.get("verificationMethod"),
            outcome.get("verification"),
            outcome.get("verification_method"),
            outcome.get("verificationMethod"),
        )
        if explicit:
            return explicit
        tests = self._merged_list(event.get("tests"), outcome.get("tests"))
        if tests:
            return "; ".join(tests)
        verified = self._bool_or_none(outcome.get("verified"))
        if verified is True:
            return "verified"
        return ""

    def _terminal_result(self, event: dict, *, outcome: dict, assistant_text: str) -> str:
        if not assistant_text:
            assistant_messages = event.get("assistant_messages") or self._assistant_messages_from_event(event)
            if assistant_messages:
                assistant_text = self._clean_agent_text(str(assistant_messages[-1].get("content") or ""))
        return self._first_text(
            event.get("result"),
            outcome.get("result"),
            outcome.get("notes"),
            event.get("error"),
            assistant_text,
        )

    def _terminal_policy_update(
        self,
        *,
        user_phrase: str,
        correction: str,
        event_type: str,
        verification: str,
        task_context: dict,
    ) -> str:
        explicit = self._first_text(
            task_context.get("policy_update"),
            task_context.get("policyUpdate"),
            task_context.get("next_policy"),
            task_context.get("nextPolicy"),
        )
        if explicit:
            return explicit
        if correction and event_type == "media_playback":
            return "media_playback 类请求先确认歌曲和播放出口，确保用户能听见或能打开播放"
        if correction:
            return f"遇到类似“{user_phrase}”的请求，先复述真实意图，执行后用用户可感知方式验证"
        if not verification:
            return "任务结束前补充可复验的验证方式或说明验证缺口"
        return ""

    def _terminal_action_path(self, event: dict, *, task_context: dict) -> list[str]:
        action_path = self._merged_list(
            event.get("action_path"),
            event.get("actionPath"),
            event.get("execution_path"),
            event.get("executionPath"),
            task_context.get("action_path"),
            task_context.get("actionPath"),
            task_context.get("execution_path"),
            task_context.get("executionPath"),
        )
        if action_path:
            return action_path
        return [f"tool:{tool}" for tool in self._terminal_tools(event, task_context=task_context)]

    def _terminal_tools(self, event: dict, *, task_context: dict) -> list[str]:
        tools = self._merged_list(
            event.get("tools"),
            event.get("used_tools"),
            event.get("usedTools"),
            task_context.get("tools"),
            task_context.get("used_tools"),
            task_context.get("usedTools"),
        )
        for call in self._iter_dicts(event.get("tool_calls"), event.get("toolCalls"), task_context.get("tool_calls")):
            name = self._first_text(
                call.get("name"),
                call.get("tool"),
                call.get("tool_name"),
                call.get("toolName"),
                (call.get("function") or {}).get("name") if isinstance(call.get("function"), dict) else "",
            )
            if name:
                tools.append(name)
        return self._dedupe_strings(tools)

    def _terminal_evidence(
        self,
        event: dict,
        *,
        outcome: dict,
        verification: str,
        result: str,
        correction: str,
    ) -> list[str]:
        evidence = self._merged_list(event.get("evidence"), outcome.get("evidence"))
        for value in (verification, result, correction):
            if value:
                evidence.append(value)
        return self._dedupe_strings(evidence)

    def _terminal_confidence(self, *, correction: str, verification: str, outcome: dict) -> float:
        if correction:
            return 0.92
        if verification and self._bool_or_none(outcome.get("success")) is True:
            return 0.86
        if verification:
            return 0.74
        return 0.55

    def _terminal_outcome_payload(
        self,
        *,
        event: dict,
        outcome: dict,
        correction: str,
        policy_update: str,
        verification: str,
        result: str,
        end_kind: str,
        policy_attribution: dict,
    ) -> dict:
        terminal_failure = self._classify_terminal_failure(event=event, outcome=outcome, result=result)
        input_quality = event.get("input_quality") or {}
        diagnostic_only = isinstance(input_quality, dict) and input_quality.get("learnable") is False
        success = self._bool_or_none(outcome.get("success"))
        if success is None:
            success = self._bool_or_none(event.get("success"))
        if diagnostic_only:
            outcome_name = "uncertain"
            reason = "terminal_input_quality_gate"
        elif correction:
            outcome_name = "bad"
            reason = "user_correction_detected"
        elif terminal_failure:
            outcome_name = "bad"
            reason = self._first_text(
                outcome.get("notes"),
                outcome.get("reason"),
                event.get("error"),
                terminal_failure["failure_class"],
            )
        elif success is False:
            outcome_name = "bad"
            reason = self._first_text(outcome.get("notes"), outcome.get("reason"), event.get("error"), "execution failed")
        elif success is True and verification:
            outcome_name = "good"
            reason = self._first_text(outcome.get("reason"), outcome.get("notes"), "success verified")
        elif success is True and end_kind == "agent_end":
            outcome_name = "uncertain"
            reason = "agent_end_success_without_explicit_verification"
        elif success is True:
            outcome_name = "verification_missing"
            reason = "verification missing at terminal hook"
        else:
            outcome_name = "uncertain"
            reason = self._first_text(outcome.get("reason"), outcome.get("notes"), "terminal outcome was not explicit")
        return {
            "outcome": outcome_name,
            "reason": reason,
            "correction_from_user": correction,
            "policy_attribution": self._normalize_policy_attribution(policy_attribution),
            "policy_update": policy_update,
            "failure_class": terminal_failure["failure_class"] if terminal_failure else "",
            "source_trust": self._classify_source_trust(
                event=event,
                outcome=outcome,
                correction=correction,
                verification=verification,
                terminal_failure=terminal_failure,
            ),
            "verification": verification,
            "result": result,
            "source": f"openclaw.{end_kind}",
        }

    def _record_outcome_trace_safely(
        self,
        *,
        event: dict,
        recorded_event_id: str,
        scope: dict,
        task_context: dict,
        outcome: dict,
        correction: str,
        verification: str,
        result: str,
        policy_attribution: dict,
        event_type: str,
        action_path: list[str],
        tools: list[str],
        end_kind: str,
    ) -> dict:
        recorder = getattr(self.runtime, "record_outcome_trace", None)
        if not callable(recorder):
            return {}
        payload = self._outcome_trace_payload(
            event=event,
            recorded_event_id=recorded_event_id,
            task_context=task_context,
            outcome=outcome,
            correction=correction,
            verification=verification,
            result=result,
            policy_attribution=policy_attribution,
            event_type=event_type,
            action_path=action_path,
            tools=tools,
            end_kind=end_kind,
        )
        try:
            return {"outcome_trace": recorder(payload, scope=scope)}
        except Exception as exc:
            return {"outcome_trace_error": str(exc)}

    def _outcome_trace_payload(
        self,
        *,
        event: dict,
        recorded_event_id: str,
        task_context: dict,
        outcome: dict,
        correction: str,
        verification: str,
        result: str,
        policy_attribution: dict,
        event_type: str,
        action_path: list[str],
        tools: list[str],
        end_kind: str,
    ) -> dict:
        terminal_failure = self._classify_terminal_failure(event=event, outcome=outcome, result=result)
        query = self._clean_prompt_query(str(event.get("query") or event.get("raw_query") or "").strip())
        trace_context = self._trace_context_from_event(event, task_context=task_context, query=query)
        input_summary = self._first_text(
            event.get("input_summary"),
            event.get("inputSummary"),
            outcome.get("input_summary"),
            outcome.get("inputSummary"),
            query,
            event.get("user_phrase"),
            event.get("userPhrase"),
            task_context.get("goal"),
            task_context.get("intent"),
            result,
        )
        status = self._outcome_trace_result(
            event=event,
            outcome=outcome,
            correction=correction,
            terminal_failure=terminal_failure,
        )
        success = self._bool_or_none(outcome.get("success"))
        if success is None:
            success = self._bool_or_none(event.get("success"))
        rehearsal = self._bool_or_none(
            self._first_present(
                event.get("rehearsal"),
                outcome.get("rehearsal"),
                task_context.get("rehearsal"),
                False,
            )
        )
        if rehearsal is None:
            rehearsal = False
        explicit_verified_values = [
            container[key]
            for container in (outcome, event, task_context)
            for key in ("verified", "is_verified", "isVerified")
            if key in container
        ]
        parsed_verified_values = [self._bool_or_none(value) for value in explicit_verified_values]
        verified_unparseable = any(value is None for value in parsed_verified_values)
        if any(value is False for value in parsed_verified_values):
            verified = False
        elif any(value is True for value in parsed_verified_values):
            verified = True
        else:
            verified = None
        verification_unexecuted = any(
            _is_unexecuted_verification_state(value) for value in (verification, result, status)
        )
        passed = bool(
            status == "success"
            and success is True
            and verification
            and verified is not False
            and not verified_unparseable
            and not verification_unexecuted
        )
        payload = {
            "source": f"openclaw.{end_kind}",
            "session_id": self._session_id_from_event(event),
            "trace_id": trace_context["trace_id"],
            "idempotency_key": trace_context["idempotency_key"],
            "trace_context": trace_context,
            "task_type": self._first_text(
                event.get("task_type"),
                event.get("taskType"),
                task_context.get("task_type"),
                task_context.get("taskType"),
                event_type,
            ),
            "input_summary": input_summary,
            "selected_tools": self._dedupe_strings([*tools, *self._terminal_tools(event, task_context=task_context)]),
            "actions": self._dedupe_strings([*action_path, *self._terminal_action_path(event, task_context=task_context)]),
            "outcome": {"status": status, "success": success, "rehearsal": rehearsal},
            "verifier": {
                "passed": passed,
                "method": f"openclaw.{end_kind}",
                "evidence_refs": [recorded_event_id],
                "checks": {"verification": verification, "result": result},
            },
            "feedback": self._outcome_trace_feedback(event=event, outcome=outcome, correction=correction),
            "risk": self._first_present(
                event.get("risk"),
                outcome.get("risk"),
                task_context.get("risk"),
            )
            or "",
            "policy_attribution": self._normalize_policy_attribution(policy_attribution),
            "failure_class": terminal_failure["failure_class"] if terminal_failure else "",
        }
        for key in ("world_state", "visual_evidence", "operator_gap"):
            value = self._first_present(event.get(key), outcome.get(key), task_context.get(key), None)
            if value is not None:
                payload[key] = value
        for contract in (
            event.get("capability_contract"),
            outcome.get("capability_contract"),
            task_context.get("capability_contract"),
        ):
            if isinstance(contract, dict):
                payload["capability_contract"] = dict(contract)
                break
        return payload

    def _outcome_trace_result(
        self,
        *,
        event: dict,
        outcome: dict,
        correction: str,
        terminal_failure: dict[str, str] | None = None,
    ) -> str:
        success = self._bool_or_none(outcome.get("success"))
        if success is None:
            success = self._bool_or_none(event.get("success"))
        input_quality = event.get("input_quality") or {}
        if isinstance(input_quality, dict) and input_quality.get("learnable") is False:
            return "uncertain"
        if correction or terminal_failure or success is False:
            return "bad"
        if success is True:
            return "success"
        return "uncertain"

    def _outcome_trace_feedback(self, *, event: dict, outcome: dict, correction: str) -> str:
        return self._first_text(
            correction,
            event.get("feedback"),
            event.get("user_feedback"),
            event.get("userFeedback"),
            outcome.get("feedback"),
            outcome.get("user_feedback"),
            outcome.get("userFeedback"),
            outcome.get("notes"),
            outcome.get("reason"),
            event.get("error"),
        )

    def _resolve_policy_attribution(self, *, event: dict, task_context: dict) -> dict[str, Any]:
        event_policy_attribution = self._normalize_policy_attribution(event)
        if self._policy_attribution_has_values(event_policy_attribution):
            return event_policy_attribution
        policy_attribution = self._normalize_policy_attribution(task_context)
        if self._policy_attribution_has_values(policy_attribution):
            return policy_attribution
        fallback = self._recall_audit_policy_attribution(event=event)
        if fallback:
            return fallback
        return policy_attribution

    def _recall_audit_policy_attribution(self, *, event: dict) -> dict[str, Any]:
        session_id = self._session_id_from_event(event)
        if not session_id:
            return {}
        scope = self._scope_from_event(event)
        scope_ref = ScopeRef.from_dict(scope)
        audits = None
        lookup = getattr(self.runtime.store, "list_records_by_meta_value", None)
        if callable(lookup):
            try:
                audits = lookup(
                    kinds=["recall_view"],
                    scope=scope_ref,
                    meta_key="session_id",
                    meta_value=session_id,
                    limit=10,
                )
            except Exception:
                audits = None
        if audits is None:
            audits = self.runtime.store.list_records(kinds=["recall_view"], scope=scope_ref, limit=500)
        for audit in audits:
            if audit.scope == scope_ref and str(audit.content.get("session_id") or "") == session_id:
                return self._normalize_policy_attribution(audit.content)
        return {}

    def _normalize_policy_attribution(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            payload = {}
        nested = payload.get("policy_attribution")
        if isinstance(nested, dict):
            merged = dict(nested)
            merged.update({key: value for key, value in payload.items() if key != "policy_attribution"})
            payload = merged
        return {
            "policy_suggestion_ids": self._coerce_string_list(payload.get("policy_suggestion_ids")),
            "policy_sources": self._coerce_string_list(payload.get("policy_sources")),
            "matched_event_type": str(payload.get("matched_event_type") or ""),
            "selected_records": self._coerce_selected_records(payload.get("selected_records")),
        }

    def _policy_attribution_has_values(self, policy_attribution: dict[str, Any]) -> bool:
        if policy_attribution.get("policy_suggestion_ids"):
            return True
        if policy_attribution.get("policy_sources"):
            return True
        if policy_attribution.get("selected_records"):
            return True
        return bool(str(policy_attribution.get("matched_event_type") or "").strip())

    def _coerce_selected_records(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        records: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            records.append(dict(item))
        return records

    def _extract_policy_suggestion_ids(self, policy_suggestions: Any) -> list[str]:
        return self._coerce_string_list(
            [
                suggestion.get("id")
                for suggestion in self._coerce_dicts(policy_suggestions)
                if str(suggestion.get("id") or "").strip()
            ]
        )

    def _extract_policy_sources(self, policy_suggestions: Any) -> list[str]:
        return self._coerce_string_list(
            [
                suggestion.get("source")
                for suggestion in self._coerce_dicts(policy_suggestions)
                if str(suggestion.get("source") or "").strip()
            ]
        )

    def _coerce_string_list(self, value: Any) -> list[str]:
        if isinstance(value, list | tuple | set):
            raw_values = value
        else:
            raw_values = [value]
        items: list[str] = []
        for item in raw_values:
            if isinstance(item, list | tuple | set):
                items.extend(self._coerce_string_list(item))
                continue
            text = str(item if item is not None else "").strip()
            if text:
                items.append(text)
        return self._dedupe_strings(items)

    def _coerce_dicts(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def _classify_source_trust(
        self,
        *,
        event: dict,
        outcome: dict,
        correction: str,
        verification: str,
        terminal_failure: dict[str, str] | None = None,
    ) -> str:
        input_quality = event.get("input_quality") or {}
        if isinstance(input_quality, dict) and input_quality.get("learnable") is False:
            return "input_diagnostic"
        if terminal_failure or self._classify_terminal_failure(event=event, outcome=outcome):
            return "system_diagnostic"
        explicit = str(
            str(outcome.get("source_trust") or outcome.get("trust") or event.get("source_trust") or "").strip()
        )
        if explicit:
            return explicit
        if correction:
            return "user_explicit"
        if self._has_system_verification(
            outcome=outcome,
            event=event,
            verification=verification,
        ):
            return "system_verified"
        return "agent_inferred"

    def _classify_terminal_failure(self, *, event: dict, outcome: dict, result: str = "") -> dict[str, str] | None:
        task_context = dict(event.get("task_context") or event.get("taskContext") or {})
        bridge_status = self._first_text(
            task_context.get("bridge_status"),
            task_context.get("bridgeStatus"),
            event.get("bridge_status"),
            event.get("bridgeStatus"),
            outcome.get("bridge_status"),
            outcome.get("bridgeStatus"),
        ).lower()
        bridge_status_token = bridge_status.replace("-", "_").replace(" ", "_")
        diagnostic_text = " ".join(
            text.lower()
            for text in (
                self._first_text(outcome.get("notes")),
                self._first_text(outcome.get("reason")),
                self._first_text(event.get("error")),
                self._first_text(result),
                self._first_text(task_context.get("bridge_status")),
                self._first_text(task_context.get("bridgeStatus")),
                self._first_text(event.get("bridge_status")),
                self._first_text(event.get("bridgeStatus")),
            )
            if text
        )
        status_failures = {
            "cooldown": "rate_limit_cooldown",
            "rate_limit": "rate_limit_cooldown",
            "rate_limited": "rate_limit_cooldown",
            "timeout": "timeout",
            "context_overflow": "context_overflow",
            "bridge_failure": "bridge_failure",
            "model_failure": "model_failure",
        }
        if bridge_status_token in status_failures:
            return {"failure_class": status_failures[bridge_status_token]}
        if "rate limit" in diagnostic_text and "cooldown" in diagnostic_text:
            return {"failure_class": "rate_limit_cooldown"}
        timeout_mentioned = "timeout" in diagnostic_text or "timed out" in diagnostic_text
        timeout_resolved = bool(
            re.search(
                r"\b(?:timeout|timed out)\b.{0,80}\b(?:resolved|fixed|recovered|recovery|cleared|completed|healthy|passing)\b",
                diagnostic_text,
            )
            or re.search(
                r"\b(?:resolved|fixed|recovered|recovery|cleared)\b.{0,80}\b(?:timeout|timed out)\b",
                diagnostic_text,
            )
        )
        timeout_unresolved = bool(
            re.search(
                r"\b(?:timeout|timed out)\b.{0,80}\b(?:active|still|pending|unresolved|failed|failure|error|unavailable)\b",
                diagnostic_text,
            )
            or re.search(
                r"\b(?:failed|failure|error)\b.{0,80}\b(?:timeout|timed out)\b",
                diagnostic_text,
            )
        )
        if timeout_mentioned and (not timeout_resolved or timeout_unresolved):
            return {"failure_class": "timeout"}
        if (
            "context overflow" in diagnostic_text
            or "context length" in diagnostic_text
            or "maximum context" in diagnostic_text
            or "too many tokens" in diagnostic_text
        ):
            return {"failure_class": "context_overflow"}
        if "bridge" in diagnostic_text and any(
            marker in diagnostic_text for marker in ("failed", "failure", "error", "offline", "unavailable")
        ):
            return {"failure_class": "bridge_failure"}
        if "model" in diagnostic_text and any(
            marker in diagnostic_text for marker in ("failed", "failure", "error", "unavailable", "overloaded")
        ):
            return {"failure_class": "model_failure"}
        return None

    def _has_system_verification(
        self,
        *,
        outcome: dict,
        event: dict,
        verification: str,
    ) -> bool:
        if str(verification or "").strip():
            return True
        fields = (
            "health",
            "health_check",
            "health_status",
            "tests",
            "test_results",
            "test_report",
            "verification",
            "verification_method",
            "verified",
        )
        for field in fields:
            if field == "verified":
                if self._bool_or_none(outcome.get(field)) is True:
                    return True
                if self._bool_or_none(event.get(field)) is True:
                    return True
                continue
            if str(outcome.get(field) or "").strip():
                return True
            if str(event.get(field) or "").strip():
                return True
        return False

    def _intent_pattern_from_correction(
        self,
        *,
        user_phrase: str,
        correction: str,
        event_type: str,
        interpreted_intent: str,
        policy_update: str,
        verification: str,
    ) -> dict | None:
        if not user_phrase:
            return None
        if event_type == "media_playback":
            return {
                "pattern": user_phrase,
                "default_event_type": "media_playback",
                "interpreted_intent": "播放音乐给用户听",
                "first_questions": ["想听哪首歌？", "是在当前设备播放，还是发可播放链接/音频？"],
                "execution_policy": [
                    "优先考虑用户能否实际听见",
                    "先判断播放出口和物理条件",
                    policy_update,
                ],
                "success_criteria": verification or "用户能听到或打开播放",
                "confidence": 0.9,
                "correction_from_user": correction,
            }
        return {
            "pattern": user_phrase,
            "default_event_type": event_type or "communication",
            "interpreted_intent": interpreted_intent,
            "execution_policy": [policy_update] if policy_update else ["先复述真实意图，再执行并验证"],
            "success_criteria": verification or "用户确认结果符合意图",
            "confidence": 0.82,
            "correction_from_user": correction,
        }

    def _looks_like_media_playback(self, user_phrase: str, correction: str) -> bool:
        combined = f"{user_phrase} {correction}"
        compact = "".join(ch for ch in combined.lower() if ch.isalnum())
        has_song_request = any(marker in compact for marker in ("唱", "歌", "music", "song", "playmusic", "playsong"))
        wants_audio = any(marker in compact for marker in ("听", "播放", "放", "音频", "hear", "listen", "play"))
        rejects_creation = any(marker in compact for marker in ("不是让", "不是要", "不对", "写歌词", "创作", "notwrite"))
        return has_song_request and (wants_audio or rejects_creation)

    def _first_text(self, *values: Any) -> str:
        for value in values:
            if isinstance(value, str):
                text = value.strip()
            elif value is None:
                text = ""
            else:
                text = str(value).strip()
            if text:
                return text
        return ""

    def _first_present(self, *values: Any) -> Any:
        for value in values:
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return None

    def _merged_list(self, *values: Any) -> list[str]:
        items: list[str] = []
        for value in values:
            if value is None:
                continue
            if isinstance(value, list | tuple | set):
                iterable = value
            else:
                iterable = [value]
            for item in iterable:
                if isinstance(item, dict):
                    text = self._first_text(
                        item.get("name"),
                        item.get("tool"),
                        item.get("title"),
                        item.get("content"),
                        item.get("summary"),
                    )
                else:
                    text = self._first_text(item)
                if text:
                    items.append(text)
        return self._dedupe_strings(items)

    def _merged_dict(self, *values: Any) -> dict:
        merged: dict[str, Any] = {}
        for value in values:
            if isinstance(value, dict):
                merged.update({str(key): item for key, item in value.items()})
        return merged

    def _iter_dicts(self, *values: Any) -> list[dict]:
        dicts: list[dict] = []
        for value in values:
            if isinstance(value, dict):
                dicts.append(value)
            elif isinstance(value, list | tuple):
                dicts.extend(item for item in value if isinstance(item, dict))
        return dicts

    def _dedupe_strings(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            deduped.append(text)
        return deduped

    def _bool_or_none(self, value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "y", "ok", "success", "succeeded"}:
            return True
        if text in {"false", "0", "no", "n", "fail", "failed", "error"}:
            return False
        return None

    def _float_or_zero(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _int_or_default(self, value: Any, *, default: int = 0) -> int:
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _assistant_messages_from_event(self, event: dict) -> list[dict]:
        messages = event.get("messages") or []
        if not isinstance(messages, list):
            return []
        return [
            {"content": str(message.get("content") or "")}
            for message in messages
            if str(message.get("role") or "").lower() == "assistant"
        ]

    def _clean_prompt_query(self, query: str) -> str:
        query = query.strip()
        if not query:
            return ""
        query = re.sub(r"```(?:json)?\s*.*?```", "", query, flags=re.DOTALL | re.IGNORECASE)
        cleaned_lines: list[str] = []
        skip_prefixes = (
            "system:",
            "conversation info",
            "sender ",
            "sender(",
            "sender:",
        )
        for line in query.splitlines():
            stripped = line.strip()
            if not stripped:
                cleaned_lines.append("")
                continue
            lowered = stripped.lower()
            if lowered.startswith(skip_prefixes):
                continue
            if stripped.startswith("{") and stripped.endswith("}"):
                continue
            cleaned_lines.append(stripped)
        paragraphs = [
            " ".join(part.strip() for part in paragraph.splitlines() if part.strip())
            for paragraph in "\n".join(cleaned_lines).split("\n\n")
        ]
        paragraphs = [paragraph for paragraph in paragraphs if paragraph]
        if paragraphs:
            return paragraphs[-1].strip()
        return " ".join(line for line in cleaned_lines if line).strip()

    def _clean_agent_text(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        lines = text.splitlines()
        while lines:
            candidate = lines[0].strip()
            if not candidate:
                lines.pop(0)
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                break
            if isinstance(parsed, dict) and str(parsed.get("type") or "").lower() == "thinking":
                lines.pop(0)
                continue
            break
        cleaned = "\n".join(lines).strip()
        cleaned = re.sub(r'^\s*\{"type"\s*:\s*"thinking".*?\}\s*', "", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r'"thinkingSignature"\s*:\s*"[^"]+"\s*,?', "", cleaned)
        cleaned = re.sub(r"```(?:json)?\s*.*?```", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
        return cleaned.strip()

    def _is_salient_agent_text(self, text: str) -> bool:
        normalized = "".join(ch for ch in text if ch.isalnum() or ch.isspace()).strip().lower()
        if not normalized:
            return False
        if self._looks_like_prompt_injection(normalized) or self._is_low_value_chatter(normalized):
            return False
        compact = normalized.replace(" ", "")
        noisy_markers = {
            "agentcompleted",
            "complete",
            "completed",
            "done",
            "success",
            "ok",
            "好的",
            "完成",
        }
        if compact in noisy_markers:
            return False
        durable_markers = (
            "decision",
            "decided",
            "summary",
            "summarized",
            "durable",
            "remember",
            "preference",
            "important",
            "rule",
            "long term memory",
            "决定",
            "决策",
            "总结",
            "摘要",
            "长期记忆",
            "重要",
            "规则",
            "记住",
        )
        return any(marker in normalized for marker in durable_markers)

    def _empty_bundle(self, event: dict) -> RecallBundle:
        query = str(event.get("query") or "").strip()
        return RecallBundle(
            items=[],
            rules=[],
            reflections=[],
            confidence=0.0,
            next_action_hint="",
            explanation={
                "query": self._clean_prompt_query(query) if query else "",
                "task_context": dict(event.get("task_context") or {}),
                "selected_count": 0,
                "active_policy": {},
                "rule_count": 0,
                "unknown_record_id": "",
                "graph_expanded": 0,
                "retrieval_mode": "hybrid",
            },
        )
