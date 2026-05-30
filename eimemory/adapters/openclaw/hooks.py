from __future__ import annotations

import json
import re
from typing import Any

from eimemory.api.runtime import Runtime
from eimemory.identity import hongtu_identity_meta, hongtu_scope
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef


DEFAULT_RECALL_MODE = "fast"
DEFAULT_RECALL_BUDGET_MS = 800
DEFAULT_FAST_CANDIDATE_LIMIT = 160


class OpenClawMemoryHooks:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def on_message_received(self, event: dict) -> dict:
        message = dict(event.get("message") or {})
        if str(message.get("role") or "").lower() != "user":
            return {"stored": None}
        text = self._clean_user_memory_text(str(message.get("content") or "").strip())
        if self._should_capture_message(text=text, event=event):
            stored = self.runtime.memory.ingest(
                text=text,
                memory_type="conversation",
                title="OpenClaw user message",
                scope=self._scope_from_event(event),
                source="openclaw.message_received",
                force_capture=self._force_capture_requested(event),
                meta=self._identity_meta(event, organ="cognition", modality="text"),
            )
            if stored.status == "rejected":
                return {"stored": None, "rejected": stored.to_dict()}
            return {"stored": stored.to_dict()}
        return {"stored": None}

    def before_prompt_build(self, event: dict) -> dict:
        query = self._clean_prompt_query(str(event.get("query") or event.get("raw_query") or "").strip())
        recall_context = self._resolve_recall_context(event)
        event = dict(event)
        event["task_context"] = recall_context
        if not query:
            bundle = self._empty_bundle({"task_context": recall_context})
            self._audit_prompt_recall(event=event, bundle=bundle, injected=False)
            return {"memory_bundle": bundle.to_dict()}
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
        self._audit_prompt_recall(event=event, bundle=bundle, injected=bool(bundle.items))
        return {
            "memory_bundle": bundle.to_dict(),
            "usage_telemetry": self._usage_telemetry(bundle),
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
        if self._force_capture_requested(event):
            return True
        normalized = "".join(ch for ch in text if ch.isalnum() or ch.isspace()).strip().lower()
        if not normalized:
            return False
        if self._looks_like_prompt_injection(normalized):
            return False
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
        return task_context

    def _coerce_recall_budget_ms(self, value: object) -> int:
        try:
            budget = int(value)
        except (TypeError, ValueError):
            return DEFAULT_RECALL_BUDGET_MS
        if budget <= 0:
            return DEFAULT_RECALL_BUDGET_MS
        return budget

    def _coerce_fast_candidate_limit(self, value: object) -> int:
        try:
            candidate_limit = int(value)
        except (TypeError, ValueError):
            return DEFAULT_FAST_CANDIDATE_LIMIT
        return max(40, min(360, candidate_limit))

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

    def _merge_policy_search(self, *, bundle: RecallBundle, policy_search: dict) -> None:
        suggestions = policy_search.get("policy_suggestions") if isinstance(policy_search, dict) else []
        if isinstance(suggestions, list) and suggestions:
            bundle.explanation["policy_suggestions"] = list(suggestions)
            bundle.explanation["policy_first"] = True
        else:
            existing = bundle.explanation.get("policy_suggestions")
            bundle.explanation["policy_suggestions"] = list(existing) if isinstance(existing, list) else []
            bundle.explanation["policy_first"] = bool(bundle.explanation["policy_suggestions"])
        matched_event_type = str(
            policy_search.get("matched_event_type")
            or bundle.explanation.get("matched_event_type")
            or ""
        )
        bundle.explanation["matched_event_type"] = matched_event_type

    def _audit_prompt_recall(self, *, event: dict, bundle: RecallBundle, injected: bool) -> RecordEnvelope:
        scope = ScopeRef.from_dict(self._scope_from_event(event))
        view = dict(bundle.explanation.get("recall_view") or {})
        injected_ids = [item.record_id for item in bundle.items]
        selected_records = self._selected_records(bundle)
        record = RecordEnvelope.create(
            kind="recall_view",
            title="OpenClaw memory injection audit",
            summary=f"Injected {len(injected_ids)} memory records before prompt build",
            detail="Audit record for OpenClaw before_prompt_build memory recall.",
            content={
                "session_id": self._session_id_from_event(event),
                "query": self._clean_prompt_query(str(event.get("query") or event.get("raw_query") or "").strip()),
                "raw_query": str(event.get("raw_query") or event.get("rawQuery") or event.get("query") or "").strip(),
                "task_context": dict(event.get("task_context") or event.get("taskContext") or {}),
                "selected_count": len(injected_ids),
                "injected": injected,
                "injected_record_ids": injected_ids,
                "selected_records": selected_records,
                "source_composition": dict(bundle.explanation.get("source_composition") or {}),
                "view_type": str(view.get("view_type") or ""),
                "confidence": bundle.confidence,
            },
            tags=["openclaw", "before_prompt_build", "injection_audit"],
            source="openclaw.before_prompt_build",
            scope=scope,
            meta={
                **self._identity_meta(event, organ="cognition", modality="text"),
                "session_id": self._session_id_from_event(event),
                "selected_count": len(injected_ids),
                "injected": injected,
                "view_type": str(view.get("view_type") or ""),
                "source_composition": dict(bundle.explanation.get("source_composition") or {}),
            },
        )
        return self.runtime.store.append(record)

    def _usage_telemetry(self, bundle: RecallBundle) -> dict:
        return {
            "selected_count": len(bundle.items),
            "confidence": bundle.confidence,
            "source_composition": dict(bundle.explanation.get("source_composition") or {}),
            "selected_records": self._selected_records(bundle),
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

    def _record_terminal_memory(self, event: dict, *, end_kind: str, assistant_text: str = "") -> dict:
        scope = self._scope_from_event(event)
        task_context = self._task_context_from_event(event)
        outcome = self._outcome_from_event(event)
        user_messages = self._user_messages_from_event(event)
        correction = self._correction_from_event(event, outcome=outcome, user_messages=user_messages)
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
        event_payload = {
            "source": f"openclaw.{end_kind}",
            "session_id": self._session_id_from_event(event),
            "hook": end_kind,
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
        }
        recorded_event = self.runtime.record_event(event_payload, scope=scope)
        outcome_payload = self._terminal_outcome_payload(
            event=event,
            outcome=outcome,
            correction=correction,
            policy_update=policy_update,
            verification=verification,
            result=result,
            end_kind=end_kind,
        )
        recorded_outcome = self.runtime.record_outcome(recorded_event["id"], outcome_payload, scope=scope)
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
        return {
            "event": recorded_event,
            "outcome": recorded_outcome,
            "pattern": pattern,
        }

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
                values.extend(raw)
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
    ) -> dict:
        success = self._bool_or_none(outcome.get("success"))
        if success is None:
            success = self._bool_or_none(event.get("success"))
        if correction:
            outcome_name = "bad"
            reason = "user_correction_detected"
        elif success is False:
            outcome_name = "bad"
            reason = self._first_text(outcome.get("notes"), outcome.get("reason"), event.get("error"), "execution failed")
        elif success is True and verification:
            outcome_name = "good"
            reason = self._first_text(outcome.get("reason"), outcome.get("notes"), "success verified")
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
            "policy_update": policy_update,
            "verification": verification,
            "result": result,
            "source": f"openclaw.{end_kind}",
        }

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
