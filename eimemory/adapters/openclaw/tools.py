from __future__ import annotations

import inspect
from collections import Counter
from typing import Any
from dataclasses import asdict

from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef
from eimemory.models.records import RecordEnvelope


class OpenClawMemoryTools:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def memory_search(self, *, query: str, scope: dict, limit: int = 5) -> dict:
        bundle = self.runtime.memory.recall(
            query=query,
            scope=scope,
            task_context={"task_type": "openclaw.tool.search"},
            limit=limit,
        )
        return {"ok": True, "items": bundle.to_dict()["items"], "meta": {"confidence": bundle.confidence}}

    def memory_store(self, *, text: str, title: str, scope: dict, memory_type: str = "fact") -> dict:
        record = self.runtime.memory.ingest(
            text=text,
            memory_type=memory_type,
            title=title,
            scope=scope,
            source="openclaw.tool.store",
        )
        return {"ok": True, "record": record.to_dict()}

    def memory_explain(self, *, query: str, task_context: dict, scope: dict, limit: int = 5) -> dict:
        bundle = self.runtime.memory.recall(
            query=query,
            scope=scope,
            task_context=task_context,
            limit=limit,
        )
        return {"ok": True, "explanation": bundle.explanation, "items": bundle.to_dict()["items"]}

    def memory_feedback(self, *, target_id: str, decision: str, reason: str, scope: dict) -> dict:
        record = self.runtime.evolution.feedback(
            target_ref={"kind": "memory", "record_id": target_id},
            decision=decision,
            reason=reason,
            reviewed_by="openclaw.tool.feedback",
            scope=scope,
        )
        return {"ok": True, "record": record.to_dict()}

    def memory_learn_status(self, *, scope: dict) -> dict:
        learn_scope = ScopeRef.from_dict(scope)
        return {
            "ok": True,
            "scope": scope,
            "knowledge_intake": self._summarize_knowledge_intake(learn_scope),
            "source_registry": self._summarize_source_registry(learn_scope),
            "external_collection": self._summarize_external_collection(learn_scope),
            "autonomy_loops": self._summarize_autonomy_loops(learn_scope),
            "capability_candidates": self._summarize_skill_candidates(learn_scope),
            "promotion_watch": self._summarize_promotion_watch(learn_scope),
            "code_sandbox": self._summarize_code_sandbox(),
        }

    def memory_run_autonomy(
        self,
        *,
        scope: dict,
        dry_run: bool = True,
        max_goals: int = 1,
        max_promotions: int = 0,
    ) -> dict:
        if not callable(getattr(self.runtime, "run_autonomy_cycle", None)):
            return {"ok": False, "error": "runtime_unavailable", "message": "run_autonomy_cycle is missing"}

        params = inspect.signature(self.runtime.run_autonomy_cycle)
        requested = {
            "scope": asdict(ScopeRef.from_dict(scope)),
            "apply": False,
            "dry_run": bool(dry_run),
            "full": True,
            "force": False,
            "max_goals": int(max_goals),
            "max_promotions": int(max_promotions),
        }
        if "max_promotions" in params.parameters:
            requested["max_promotions"] = int(max_promotions)
        elif "policy" in params.parameters:
            requested["policy"] = {"max_auto_promotions": int(max_promotions)}

        call_kwargs = {
            name: value
            for name, value in requested.items()
            if name in params.parameters or (name == "policy" and "policy" in params.parameters)
        }
        report = dict(self.runtime.run_autonomy_cycle(**call_kwargs))
        report["ok"] = bool(report.get("ok", False))
        return {"ok": True, **report, "requested": requested}

    def memory_source_scan(
        self,
        *,
        scope: dict,
        persist: bool = False,
        source_kind: str | None = None,
        limit: int | None = None,
        fetch: bool = False,
    ) -> dict:
        if not callable(getattr(self.runtime, "collect_external_sources", None)):
            return {"ok": False, "error": "runtime_unavailable", "message": "collect_external_sources is missing"}

        return dict(
            self.runtime.collect_external_sources(
                scope=scope,
                source_kind=source_kind,
                limit=limit,
                persist=bool(persist),
                fetch=bool(fetch),
            )
        )

    def memory_skill_status(self, *, scope: dict, limit: int = 20) -> dict:
        learn_scope = ScopeRef.from_dict(scope)
        capability_candidates = self._list_records_by_kind(learn_scope, candidate_kinds=["capability_candidate"], limit=limit)
        skill_candidates = self._list_records_by_kind(learn_scope, candidate_kinds=["skill_candidate"], limit=limit)

        combined = []
        combined.extend([record.to_dict() for record in capability_candidates])
        combined.extend([record.to_dict() for record in skill_candidates])

        return {
            "ok": True,
            "scope": scope,
            "limit": int(limit),
            "candidate_count": len(combined),
            "capability_candidate_count": len(capability_candidates),
            "skill_candidate_count": len(skill_candidates),
            "candidates": combined[: int(limit)],
        }

    def memory_code_patch_propose(
        self,
        *,
        incident: dict,
        scope: dict,
        create_worktree: bool = False,
        persist_report: bool = False,
    ) -> dict:
        if not callable(getattr(self.runtime, "propose_code_patch", None)):
            return {"ok": False, "error": "runtime_unavailable", "message": "propose_code_patch is missing"}

        return dict(
            self.runtime.propose_code_patch(
                incident=dict(incident or {}),
                scope=scope,
                create_worktree=bool(create_worktree),
                persist_report=bool(persist_report),
            )
        )

    def _summarize_knowledge_intake(self, scope: ScopeRef) -> dict:
        try:
            sources = self.runtime.sources.list_sources() if self.runtime.sources else []
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "message": str(exc)}

        knowledge_candidates = self._list_records_by_kind(scope=scope, candidate_kinds=["knowledge_candidate"], limit=200)
        return {
            "registry_present": bool(self.runtime.sources is not None),
            "source_count": len(sources),
            "enabled_source_count": len([source for source in sources if getattr(source, "enabled", False)]),
            "disabled_source_count": len([source for source in sources if not getattr(source, "enabled", False)]),
            "knowledge_candidate_count": len(knowledge_candidates),
            "latest_scan_at": self._latest_source_scan_time(sources),
            "latest_scan_ok": self._latest_scan_ok(sources),
            "source_kinds": sorted({str(source.source_kind) for source in sources if getattr(source, "source_kind", "")}),
        }

    def _summarize_source_registry(self, _scope: ScopeRef) -> dict:
        try:
            all_sources = self.runtime.sources.list_sources()
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "message": str(exc)}

        counts_by_kind: dict[str, int] = {}
        for source in all_sources:
            source_kind = str(source.source_kind)
            counts_by_kind[source_kind] = counts_by_kind.get(source_kind, 0) + 1
        return {
            "ok": True,
            "source_count": len(all_sources),
            "enabled_count": len([source for source in all_sources if source.enabled]),
            "disabled_count": len([source for source in all_sources if not source.enabled]),
            "by_kind": counts_by_kind,
            "registry_path": str(self.runtime.sources.path) if self.runtime.sources else "",
        }

    def _summarize_external_collection(self, scope: ScopeRef) -> dict:
        capability = hasattr(self.runtime, "collect_external_sources")
        available = bool(capability and callable(getattr(self.runtime, "collect_external_sources")))
        source_count = 0
        if available:
            try:
                source_count = len(self.runtime.sources.list_sources(enabled=True))
            except Exception:
                source_count = 0
        return {
            "available": available,
            "method": "runtime.collect_external_sources" if available else "",
            "enabled_source_count": int(source_count),
            "requested_scope": asdict(scope),
        }

    def _summarize_autonomy_loops(self, scope: ScopeRef) -> dict:
        loops = self._safe_list_runtime_records("list_learning_loops", scope, limit=20, fallback=[])
        goals = self._safe_list_runtime_records("list_learning_goals", scope, limit=20, fallback=[])
        candidates = self._safe_list_runtime_records("list_learning_candidates", scope, limit=20, fallback=[])
        return {
            "available": all(callable(getattr(self.runtime, name, None)) for name in ["list_learning_loops", "list_learning_goals"]),
            "loop_count": len(loops),
            "goal_count": len(goals),
            "candidate_count": len(candidates),
            "latest_loop": loops[0] if loops else {},
            "latest_goal": goals[0] if goals else {},
            "latest_candidate": candidates[0] if candidates else {},
        }

    def _summarize_promotion_watch(self, scope: ScopeRef) -> dict:
        status_counter = Counter()
        watch_records = self._list_records_by_kind(
            scope=scope,
            candidate_kinds=["promotion_request", "learning_loop", "capability_score", "regression_watch"],
            limit=200,
        )
        for record in watch_records:
            status = self._contains_signal_key(record)
            status_counter[status] += 1

        return {
            "available": bool(watch_records or self._is_promotion_watch_signal(asdict(scope))),
            "request_count": len(watch_records),
            "status_distribution": dict(status_counter),
            "latest": watch_records[0].to_dict() if watch_records else {},
        }

    def _summarize_code_sandbox(self) -> dict:
        try:
            from eimemory.governance import sandbox_lab

            proposal_available = callable(getattr(self.runtime, "propose_code_patch", None))
            return {
                "available": bool(hasattr(sandbox_lab, "create_sandbox_experiment") and proposal_available),
                "create_sandbox_experiment": bool(hasattr(sandbox_lab, "create_sandbox_experiment")),
                "code_patch_proposal": proposal_available,
            }
        except Exception as exc:
            return {
                "available": False,
                "error": type(exc).__name__,
                "message": str(exc),
            }

    def _summarize_skill_candidates(self, scope: ScopeRef) -> dict:
        capability_candidates = self._list_records_by_kind(scope=scope, candidate_kinds=["capability_candidate"], limit=50)
        skill_candidates = self._list_records_by_kind(scope=scope, candidate_kinds=["skill_candidate"], limit=50)
        return {
            "ok": True,
            "capability_candidate_count": len(capability_candidates),
            "skill_candidate_count": len(skill_candidates),
            "candidate_count": len(capability_candidates) + len(skill_candidates),
            "latest_capability_candidate": capability_candidates[0].to_dict() if capability_candidates else {},
            "latest_skill_candidate": skill_candidates[0].to_dict() if skill_candidates else {},
        }

    def _is_promotion_watch_signal(self, scope: dict[str, Any]) -> bool:
        try:
            scope_ref = ScopeRef.from_dict(scope)
            loops = self.runtime.store.list_records(kinds=["learning_loop"], scope=scope_ref, limit=1)
            return bool(loops)
        except Exception:
            return False

    def _contains_signal_key(self, record: RecordEnvelope) -> str:
        content = record.content if isinstance(record.content, dict) else {}
        meta = record.meta if isinstance(record.meta, dict) else {}
        candidate_status = (
            str(
                content.get("post_promotion_status")
                or meta.get("post_promotion_status")
                or getattr(record, "status", "")
                or "active"
            )
            .strip()
            .lower()
        )
        if not candidate_status:
            return "unknown"
        return candidate_status

    def _list_records_by_kind(self, scope: ScopeRef, candidate_kinds: list[str], limit: int = 100) -> list[RecordEnvelope]:
        try:
            return self.runtime.store.list_records(kinds=candidate_kinds, scope=scope, limit=max(0, int(limit)))
        except Exception:
            return []

    def _safe_list_runtime_records(self, method_name: str, scope: ScopeRef, *, limit: int = 10, fallback: list[Any] | None = None) -> list[Any]:
        method = getattr(self.runtime, method_name, None)
        if not callable(method):
            return list(fallback or [])
        try:
            return list(method(scope=asdict(scope), limit=limit))
        except Exception:
            return list(fallback or [])

    def _latest_source_scan_time(self, sources: list[Any]) -> str:
        latest = ""
        for source in sources:
            scanned_at = str(getattr(source, "last_scanned_at", "") or "")
            if scanned_at and (not latest or scanned_at > latest):
                latest = scanned_at
        return latest

    def _latest_scan_ok(self, sources: list[Any]) -> bool:
        latest = self._latest_source_scan_time(sources)
        if not latest:
            return False
        for source in sources:
            if str(getattr(source, "last_scanned_at", "")) == latest:
                return str((getattr(source, "metadata", {}) or {}).get("last_scan", {}).get("status", "")).lower() != "error"
        return True
