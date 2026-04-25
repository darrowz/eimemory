from __future__ import annotations

import json
import re

from eimemory.api.runtime import Runtime
from eimemory.identity import hongtu_identity_meta, hongtu_scope
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef


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
            return {"stored": stored.to_dict()}
        return {"stored": None}

    def before_prompt_build(self, event: dict) -> dict:
        query = self._clean_prompt_query(str(event.get("query") or event.get("raw_query") or "").strip())
        if not query:
            bundle = self._empty_bundle(event)
            self._audit_prompt_recall(event=event, bundle=bundle, injected=False)
            return {"memory_bundle": bundle.to_dict()}
        bundle = self.runtime.memory.recall(
            query=query,
            scope=self._scope_from_event(event),
            task_context=dict(event.get("task_context") or {}),
            limit=8,
        )
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
        }

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
        return RecallBundle(
            items=[],
            rules=[],
            reflections=[],
            confidence=0.0,
            next_action_hint="",
            explanation={
                "query": "",
                "task_context": dict(event.get("task_context") or {}),
                "selected_count": 0,
                "active_policy": {},
                "rule_count": 0,
                "unknown_record_id": "",
                "graph_expanded": 0,
                "retrieval_mode": "hybrid",
            },
        )
