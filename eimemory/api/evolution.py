from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import asdict
from statistics import mean
from typing import Any

from eimemory.api.memory import MemoryAPI
from eimemory.living import LIVING_MEMORY_META_KEY, enrich_living_memory
from eimemory.models.relation_records import RelationRecord
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, VALID_KINDS, evaluate_memory_quality
from eimemory.scoring import extract_memory_score, score_from_legacy_quality, summarize_scores, with_score_metadata
from eimemory.storage.runtime_store import RuntimeStore


ROI_EVAL_REPORT_TYPES = frozenset({"production_recall_eval", "real_task_replay", "memory_eval_ci", "learning_eval"})
LEARNING_EVAL_REPORT_TYPE = "learning_eval"
REAL_TASK_REPLAY_REPORT_TYPE = "real_task_replay"
REPLAY_DATASET_REPORT_TYPE = "proactive_replay_dataset"
ROI_WEIGHTS = {
    "accepted_feedback": 1.0,
    "replay_passes": 1.0,
    "active_rules": 1.0,
    "eval_pass_reports": 1.0,
    "incidents": 1.0,
    "replay_failures": 1.0,
    "eval_fail_reports": 1.0,
}

OPERATIONAL_INCIDENT_PENALTY_RATE = 0.25
_OPERATIONAL_INCIDENT_KEYWORDS = (
    "quota",
    "rate limit",
    "auth refresh",
    "cron timeout",
    "daily quota",
    "content flagged",
    "context overflow",
    "client closed",
    "idle timed out",
    "model/provider",
    "stale",
    "subscription usage limit",
    "transient",
    "timeout",
    "aborted",
    "usage limit",
)


class EvolutionAPI:
    def __init__(self, store: RuntimeStore) -> None:
        self.store = store

    def observe(self, *, signal_type: str, payload: dict, scope: dict) -> RecordEnvelope:
        normalized_signal_type = signal_type if signal_type in VALID_KINDS else "incident"
        title = str(payload.get("title") or payload.get("incident_type") or signal_type)
        summary = str(payload.get("summary") or "")
        meta = {k: v for k, v in payload.items() if k not in {"title", "summary"}}
        meta["signal_type"] = signal_type
        if not isinstance(meta.get(LIVING_MEMORY_META_KEY), dict):
            meta[LIVING_MEMORY_META_KEY] = enrich_living_memory(
                {
                    "title": title,
                    "summary": summary,
                    "detail": "",
                    "meta": meta,
                }
            )
        record = RecordEnvelope.create(
            kind=normalized_signal_type,
            title=title,
            summary=summary,
            content={"payload": dict(payload)},
            scope=ScopeRef.from_dict(scope),
            source="evolution.observe",
            meta=meta,
        )
        return self.store.append(record)

    def log_reflection(
        self,
        *,
        tag: str,
        miss: str,
        fix: str,
        scope: dict,
    ) -> RecordEnvelope:
        record = RecordEnvelope.create(
            kind="reflection",
            title=f"Reflection: {tag}",
            summary=miss,
            content={"tag": tag, "miss": miss, "fix": fix, "lesson": fix},
            scope=ScopeRef.from_dict(scope),
            source="evolution.log_reflection",
            meta={"tag": tag, "miss": miss, "fix": fix, "lesson": fix},
        )
        return self.store.append(record)

    def feedback(
        self,
        *,
        target_ref: dict,
        decision: str,
        reason: str,
        reviewed_by: str,
        scope: dict,
    ) -> RecordEnvelope:
        record = RecordEnvelope.create(
            kind="feedback",
            title=f"Feedback for {target_ref.get('kind', 'record')}",
            summary=reason,
            content={"target_ref": dict(target_ref), "decision": decision},
            scope=ScopeRef.from_dict(scope),
            source="evolution.feedback",
            meta={
                "decision": decision,
                "reviewed_by": reviewed_by,
                "target_ref": dict(target_ref),
            },
        )
        return self.store.append(record)

    def store_rule(
        self,
        *,
        title: str,
        summary: str,
        task_type: str,
        retrieval_policy: dict,
        response_policy: dict | None = None,
        scope: dict,
        status: str = "candidate",
    ) -> RecordEnvelope:
        response_policy = dict(response_policy or {})
        record = RecordEnvelope.create(
            kind="rule",
            title=title,
            summary=summary,
            content={
                "task_type": task_type,
                "retrieval_policy": dict(retrieval_policy),
                "response_policy": response_policy,
            },
            scope=ScopeRef.from_dict(scope),
            source="evolution.rule",
            status=status,
            meta={
                "task_type": task_type,
                "retrieval_policy": dict(retrieval_policy),
                "response_policy": response_policy,
            },
        )
        return self.store.append(record)

    def list_rules(
        self,
        *,
        scope: dict,
        task_type: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[RecordEnvelope]:
        rules = self.store.list_records(
            kinds=["rule"],
            scope=scope,
            status=status,
            limit=limit,
        )
        if not task_type:
            return rules
        return [rule for rule in rules if str(rule.meta.get("task_type") or "") == task_type]

    def read_reflections(self, *, scope: dict, limit: int = 5) -> list[RecordEnvelope]:
        return self.store.list_records(kinds=["reflection"], scope=scope, limit=limit)

    def get_active_policy(self, *, task_type: str, scope: dict) -> dict:
        return self.store.get_active_policy(task_type=task_type, scope=scope)

    def review_rule(
        self,
        *,
        record_id: str,
        decision: str,
        reviewer: str,
        note: str = "",
    ) -> RecordEnvelope:
        record = self.store.get_by_id(record_id)
        if record is None or record.kind != "rule":
            raise ValueError(f"rule not found: {record_id}")
        record.status = decision
        history = list(record.meta.get("review_history") or [])
        history.append({"decision": decision, "reviewer": reviewer, "note": note})
        record.meta["review_history"] = history
        record.touch()
        return self.store.append(record)

    def promote_rule(
        self,
        *,
        record_id: str,
        promoter: str,
        note: str = "",
    ) -> RecordEnvelope:
        record = self.store.get_by_id(record_id)
        if record is None or record.kind != "rule":
            raise ValueError(f"rule not found: {record_id}")
        record.status = "active"
        history = list(record.meta.get("promotion_history") or [])
        history.append({"promoter": promoter, "note": note})
        record.meta["promotion_history"] = history
        record.touch()
        return self.store.append(record)

    def replay_rule(
        self,
        *,
        record_id: str,
        dataset: list[dict],
    ) -> RecordEnvelope:
        rule = self.store.get_by_id(record_id)
        if rule is None or rule.kind != "rule":
            raise ValueError(f"rule not found: {record_id}")
        memory_api = MemoryAPI(self.store)
        default_task_type = str(rule.meta.get("task_type") or "")
        scores: list[float] = []
        for sample in dataset:
            sample_scope = dict(sample.get("scope") or asdict(rule.scope))
            sample_task_context = dict(sample.get("task_context") or {})
            if default_task_type and not str(sample_task_context.get("task_type") or ""):
                sample_task_context["task_type"] = default_task_type
            bundle = memory_api.recall(
                query=str(sample.get("query") or ""),
                scope=sample_scope,
                task_context=sample_task_context,
                limit=int(sample.get("limit") or 5),
            )
            results = list(bundle.items)
            kinds = [str(item) for item in (sample.get("kinds") or []) if str(item)]
            if kinds:
                results = [item for item in results if item.kind in kinds]
            expected_titles = [str(item) for item in (sample.get("expect_any_title") or [])]
            hit = any(result.title in expected_titles for result in results)
            scores.append(1.0 if hit else 0.0)
        pass_rate = mean(scores) if scores else 0.0
        verdict = "pass" if pass_rate >= 0.8 else "fail"
        report = RecordEnvelope.create(
            kind="replay_result",
            title=f"Replay for {rule.title}",
            summary=f"Replay verdict: {verdict}",
            scope=rule.scope,
            source="evolution.replay",
            meta={
                "target_rule_id": rule.record_id,
                "pass_rate": round(pass_rate, 3),
                "sample_size": len(scores),
                "verdict": verdict,
            },
            content={"dataset_size": len(dataset)},
        )
        return self.store.append(report)

    def evaluate_recall_dataset(
        self,
        *,
        dataset: list[dict],
        scope: dict,
        task_type: str = "",
        profile: str = "balanced",
    ) -> dict:
        scope_ref = ScopeRef.from_dict(scope)
        normalized_profile = self._normalize_recall_profile(profile) or "balanced"
        memory_api = MemoryAPI(self.store)
        samples: list[dict] = []
        hit_count = 0
        misses: list[dict] = []
        for index, sample in enumerate(dataset or []):
            if not isinstance(sample, dict):
                miss = {
                    "index": index,
                    "query": "",
                    "scope": asdict(scope_ref),
                    "task_type": task_type,
                    "expected_titles": [],
                    "expected_record_ids": [],
                    "expected_kinds": [],
                    "returned_titles": [],
                    "error": "invalid_sample",
                }
                misses.append(miss)
                samples.append({**miss, "hit": False, "returned_record_ids": []})
                continue
            sample_scope = dict(sample.get("scope") or asdict(scope_ref))
            query = str(sample.get("query") or "")
            sample_task_context = dict(sample.get("task_context") or {})
            sample_task_type = str(sample.get("task_type") or sample_task_context.get("task_type") or task_type or "")
            if sample_task_type and not str(sample_task_context.get("task_type") or ""):
                sample_task_context["task_type"] = sample_task_type
            sample_profile = self._normalize_recall_profile(sample.get("profile")) or normalized_profile
            sample_task_context.setdefault("recall_profile", sample_profile)
            expected_titles = {str(item) for item in (sample.get("expect_any_title") or []) if str(item)}
            expected_ids = {str(item) for item in (sample.get("expect_any_record_id") or []) if str(item)}
            expected_kinds = {str(item) for item in (sample.get("expect_any_kind") or []) if str(item)}
            search_kinds = [str(item) for item in (sample.get("kinds") or []) if str(item)]
            search_limit = int(sample.get("limit") or 5)
            bundle = memory_api.recall(
                query=query,
                scope=sample_scope,
                task_context=sample_task_context,
                limit=search_limit,
            )
            results = list(bundle.items)
            if search_kinds:
                results = [item for item in results if item.kind in search_kinds]
            hit = self._sample_hit(
                results=results,
                expected_titles=expected_titles,
                expected_ids=expected_ids,
                expected_kinds=expected_kinds,
            )
            if hit:
                hit_count += 1
            else:
                misses.append(
                    {
                        "index": index,
                        "query": query,
                        "scope": sample_scope,
                        "task_type": sample_task_type,
                        "expected_titles": sorted(expected_titles),
                        "expected_record_ids": sorted(expected_ids),
                        "expected_kinds": sorted(expected_kinds),
                        "returned_titles": [item.title for item in results],
                    }
                )
            samples.append(
                {
                    "index": index,
                    "query": query,
                    "scope": sample_scope,
                    "task_type": sample_task_type,
                    "hit": hit,
                    "expected_titles": sorted(expected_titles),
                    "expected_record_ids": sorted(expected_ids),
                    "expected_kinds": sorted(expected_kinds),
                    "returned_record_ids": [item.record_id for item in results],
                    "returned_titles": [item.title for item in results],
                }
            )
        sample_count = len(samples)
        miss_count = sample_count - hit_count
        pass_rate = round((hit_count / sample_count) if sample_count else 0.0, 3)
        return {
            "scope": asdict(scope_ref),
            "task_type": task_type,
            "profile": normalized_profile,
            "sample_count": sample_count,
            "hit_count": hit_count,
            "miss_count": miss_count,
            "pass_rate": pass_rate,
            "misses": misses,
            "samples": samples,
        }

    def promotion_candidates(
        self,
        *,
        scope: dict,
        min_pass_rate: float = 0.8,
    ) -> dict:
        scope_ref = ScopeRef.from_dict(scope)
        min_pass_rate = self._normalize_pass_rate_threshold(min_pass_rate)
        candidate_rules = self.list_rules(scope=scope, status="accepted", limit=500)
        replay_results = self.store.list_records(kinds=["replay_result"], scope=scope, limit=500)
        feedback_records = self.store.list_records(kinds=["feedback"], scope=scope, limit=500)

        candidates: list[dict] = []
        blocked: list[dict] = []
        for rule in candidate_rules:
            latest_feedback = self._latest_feedback_for_rule(rule_id=rule.record_id, feedback_records=feedback_records)
            latest_replay = self._latest_replay_for_rule(rule_id=rule.record_id, replay_results=replay_results)
            blocked_reasons: list[str] = []

            if latest_feedback is None:
                blocked_reasons.append("missing_feedback")
            elif str(latest_feedback.meta.get("decision") or "") != "accept":
                blocked_reasons.append(f"feedback_decision={latest_feedback.meta.get('decision')}")

            if latest_replay is None:
                blocked_reasons.append("missing_replay_result")
                pass_rate = 0.0
            else:
                pass_rate = float(latest_replay.meta.get("pass_rate") or 0.0)
                if str(latest_replay.meta.get("verdict") or "") != "pass":
                    blocked_reasons.append(f"replay_verdict={latest_replay.meta.get('verdict')}")
                if pass_rate < min_pass_rate:
                    blocked_reasons.append(f"pass_rate_below_threshold={round(pass_rate, 3)}")

            payload = {
                "record_id": rule.record_id,
                "title": rule.title,
                "status": rule.status,
                "task_type": str(rule.meta.get("task_type") or ""),
                "latest_feedback": self._feedback_snapshot(latest_feedback),
                "latest_replay_result": self._replay_snapshot(latest_replay),
                "evidence": self._promotion_evidence(latest_feedback, latest_replay),
            }

            if blocked_reasons:
                payload["blocked_reasons"] = blocked_reasons
                payload["blocked_reason"] = "; ".join(blocked_reasons)
                blocked.append(payload)
            else:
                payload["promotion_status"] = "eligible"
                payload["pass_rate"] = round(pass_rate, 3)
                candidates.append(payload)

        candidates.sort(key=lambda item: (-float(item.get("pass_rate") or 0.0), item["title"], item["record_id"]))
        blocked.sort(key=lambda item: (item["title"], item["record_id"]))
        return {
            "scope": asdict(scope_ref),
            "min_pass_rate": round(min_pass_rate, 3),
            "reviewed_rule_count": len(candidate_rules),
            "candidate_count": len(candidates),
            "blocked_count": len(blocked),
            "candidates": candidates,
            "blocked": blocked,
        }

    def capture_recall_gap(
        self,
        *,
        query: str,
        task_context: dict | None,
        scope: dict,
        policy: dict | None = None,
    ) -> dict[str, RecordEnvelope]:
        scope_ref = ScopeRef.from_dict(scope)
        task_context = dict(task_context or {})
        policy = dict(policy or {})
        task_type = str(task_context.get("task_type") or "unknown")
        existing = self._find_recall_gap(
            query=query,
            task_type=task_type,
            task_context=task_context,
            policy=policy,
            scope=scope_ref,
        )
        if existing:
            return existing
        unknown = RecordEnvelope.create(
            kind="unknown",
            title=f"Recall gap: {query[:48] or task_type}",
            summary="Recall returned no strong memory candidates",
            content={"query": query, "task_context": task_context},
            scope=scope_ref,
            source="evolution.recall_gap",
            meta={
                "query": query,
                "task_type": task_type,
                "policy": policy,
                "blocking": True,
            },
        )
        stored_unknown = self.store.append(unknown)
        reflection = RecordEnvelope.create(
            kind="reflection",
            title=f"Reflection for {task_type}",
            summary="Capture the weak-recall episode for future rule promotion",
            content={"query": query, "task_context": task_context},
            scope=scope_ref,
            source="evolution.recall_gap",
            links=[
                LinkRef(
                    relation="derived_from",
                    target_kind="unknown",
                    target_id=stored_unknown.record_id,
                )
            ],
            meta={
                "trigger": "recall_low_confidence",
                "task_type": task_type,
                "policy": policy,
            },
        )
        stored_reflection = self.store.append(reflection)
        return {"unknown": stored_unknown, "reflection": stored_reflection}

    def _find_recall_gap(
        self,
        *,
        query: str,
        task_type: str,
        task_context: dict,
        policy: dict,
        scope: ScopeRef,
    ) -> dict[str, RecordEnvelope] | None:
        unknown = next(
            (
                item
                for item in self.store.list_records(kinds=["unknown"], scope=scope, limit=100)
                if str(item.meta.get("query") or "") == query
                and str(item.meta.get("task_type") or "unknown") == task_type
            ),
            None,
        )
        if unknown is None:
            return None
        reflection = next(
            (
                item
                for item in self.store.list_records(kinds=["reflection"], scope=scope, limit=100)
                if str(item.meta.get("trigger") or "") == "recall_low_confidence"
                and str(item.content.get("query") or "") == query
                and str(item.meta.get("task_type") or "unknown") == task_type
            ),
            None,
        )
        if reflection is None:
            reflection = RecordEnvelope.create(
                kind="reflection",
                title=f"Reflection for {task_type}",
                summary="Capture the weak-recall episode for future rule promotion",
                content={"query": query, "task_context": task_context},
                scope=scope,
                source="evolution.recall_gap",
                links=[
                    LinkRef(
                        relation="derived_from",
                        target_kind="unknown",
                        target_id=unknown.record_id,
                    )
                ],
                meta={
                    "trigger": "recall_low_confidence",
                    "task_type": task_type,
                    "policy": policy,
                },
            )
            reflection = self.store.append(reflection)
        return {"unknown": unknown, "reflection": reflection}

    def build_roi_report(self, *, scope: dict) -> dict:
        incidents = self.store.list_records(kinds=["incident"], scope=scope, limit=500)
        feedback = self.store.list_records(kinds=["feedback"], scope=scope, limit=500)
        replay_results = self.store.list_records(kinds=["replay_result"], scope=scope, limit=500)
        reflections = self.store.list_records(kinds=["reflection"], scope=scope, limit=500)
        learning_eval_records = self.store.list_records(kinds=["learning_eval"], scope=scope, limit=500)
        rules = self.store.list_records(kinds=["rule"], scope=scope, limit=500)

        accepted_feedback_count = sum(1 for item in feedback if item.meta.get("decision") == "accept")
        replay_dataset_count = sum(1 for item in replay_results if self._roi_is_replay_dataset_record(item))
        replay_metric_records = [item for item in replay_results if self._roi_is_replay_metric_record(item)]
        real_task_replay_records = [item for item in replay_metric_records if self._roi_is_real_task_replay_record(item)]
        replay_outcomes = [self._roi_eval_report_outcome(item) for item in replay_metric_records]
        real_task_replay_outcomes = [self._roi_eval_report_outcome(item) for item in real_task_replay_records]
        replay_pass_count = sum(1 for outcome in replay_outcomes if outcome == "pass")
        replay_failure_count = sum(1 for outcome in replay_outcomes if outcome == "fail")
        actual_replay_count = len(replay_metric_records)
        real_task_replay_count = len(real_task_replay_records)
        real_task_replay_pass_count = sum(1 for outcome in real_task_replay_outcomes if outcome == "pass")
        real_task_replay_fail_count = sum(1 for outcome in real_task_replay_outcomes if outcome == "fail")
        accepted_rule_count = sum(1 for item in rules if item.status == "accepted")
        active_rule_count = sum(1 for item in rules if item.status == "active")
        incident_count = len(incidents)
        operational_incident_count = sum(1 for item in incidents if self._roi_is_operational_incident(item))
        incident_penalty_count = float(incident_count - operational_incident_count) + (operational_incident_count * OPERATIONAL_INCIDENT_PENALTY_RATE)
        learning_eval_outcomes = [self._roi_eval_report_outcome(item) for item in learning_eval_records]
        learning_eval_pass_count = sum(1 for outcome in learning_eval_outcomes if outcome == "pass")
        learning_eval_fail_count = sum(1 for outcome in learning_eval_outcomes if outcome == "fail")
        eval_report_outcomes = [
            self._roi_eval_report_outcome(item)
            for item in [*replay_results, *reflections, *learning_eval_records]
            if self._roi_report_type(item) in ROI_EVAL_REPORT_TYPES
            and not self._roi_is_real_task_replay_record(item)
            and not self._roi_is_replay_metric_record(item)
            and not self._roi_has_replay_verdict(item)
        ]
        eval_pass_report_count = sum(1 for outcome in eval_report_outcomes if outcome == "pass")
        eval_fail_report_count = sum(1 for outcome in eval_report_outcomes if outcome == "fail")
        positive = {
            "accepted_feedback": accepted_feedback_count * ROI_WEIGHTS["accepted_feedback"],
            "replay_passes": replay_pass_count * ROI_WEIGHTS["replay_passes"],
            "active_rules": active_rule_count * ROI_WEIGHTS["active_rules"],
            "eval_pass_reports": eval_pass_report_count * ROI_WEIGHTS["eval_pass_reports"],
        }
        negative = {
            "incidents": incident_penalty_count * ROI_WEIGHTS["incidents"],
            "replay_failures": replay_failure_count * ROI_WEIGHTS["replay_failures"],
            "eval_fail_reports": eval_fail_report_count * ROI_WEIGHTS["eval_fail_reports"],
        }
        positive_total = sum(positive.values())
        negative_total = sum(negative.values())
        return {
            "incident_count": incident_count,
            "operational_incident_count": operational_incident_count,
            "incident_penalty_count": incident_penalty_count,
            "feedback_count": len(feedback),
            "accepted_feedback_count": accepted_feedback_count,
            "replay_count": len(replay_results),
            "actual_replay_count": actual_replay_count,
            "replay_pass_count": replay_pass_count,
            "replay_dataset_count": replay_dataset_count,
            "replay_fail_count": replay_failure_count,
            "real_task_replay_count": real_task_replay_count,
            "real_task_replay_pass_count": real_task_replay_pass_count,
            "real_task_replay_fail_count": real_task_replay_fail_count,
            "learning_eval_count": len(learning_eval_records),
            "learning_eval_pass_count": learning_eval_pass_count,
            "learning_eval_fail_count": learning_eval_fail_count,
            "accepted_rule_count": accepted_rule_count,
            "active_rule_count": active_rule_count,
            "roi_signal": positive_total - negative_total,
            "roi_breakdown": {
                "counts": {
                    "incidents": incident_count,
                    "operational_incidents": operational_incident_count,
                    "incident_penalty_count": incident_penalty_count,
                    "accepted_feedback": accepted_feedback_count,
                    "actual_replays": actual_replay_count,
                    "replay_passes": replay_pass_count,
                    "replay_failures": replay_failure_count,
                    "replay_datasets": replay_dataset_count,
                    "real_task_replays": real_task_replay_count,
                    "real_task_replay_passes": real_task_replay_pass_count,
                    "real_task_replay_failures": real_task_replay_fail_count,
                    "learning_evals": len(learning_eval_records),
                    "learning_eval_passes": learning_eval_pass_count,
                    "learning_eval_failures": learning_eval_fail_count,
                    "accepted_rules": accepted_rule_count,
                    "active_rules": active_rule_count,
                    "eval_pass_reports": eval_pass_report_count,
                    "eval_fail_reports": eval_fail_report_count,
                },
                "weights": dict(ROI_WEIGHTS),
                "positive": {**positive, "total": positive_total},
                "negative": {**negative, "total": negative_total},
            },
        }

    @staticmethod
    def _roi_report_payload(record: RecordEnvelope) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        report = record.content.get("report") if isinstance(record.content, dict) else None
        if isinstance(report, dict):
            payload.update(report)
        if isinstance(record.content, dict):
            payload.update({key: value for key, value in record.content.items() if key != "report"})
        payload.update(record.meta)
        return payload

    @classmethod
    def _roi_report_type(cls, record: RecordEnvelope) -> str:
        if record.kind == "learning_eval":
            return LEARNING_EVAL_REPORT_TYPE
        payload = cls._roi_report_payload(record)
        return str(payload.get("report_type") or "").strip()

    @classmethod
    def _roi_has_replay_verdict(cls, record: RecordEnvelope) -> bool:
        if record.kind != "replay_result":
            return False
        verdict = str(cls._roi_report_payload(record).get("verdict") or "").strip().lower()
        return verdict in {"pass", "passed", "success", "fail", "failed", "failure"}

    @classmethod
    def _roi_eval_report_outcome(cls, record: RecordEnvelope) -> str | None:
        payload = cls._roi_report_payload(record)
        if record.kind == "learning_eval":
            status = str(record.status or "").strip().lower()
            if status == "passed":
                return "pass"
            if status == "rejected":
                return "fail"
        verdict = str(payload.get("verdict") or "").strip().lower()
        if verdict in {"pass", "passed", "success"}:
            return "pass"
        if verdict in {"fail", "failed", "failure"}:
            return "fail"
        if "passed_threshold" in payload:
            return "pass" if bool(payload.get("passed_threshold")) else "fail"
        if payload.get("ok") is False:
            return "fail"

        threshold = cls._roi_float(payload.get("threshold"), default=0.8)
        pass_rate = cls._roi_float(payload.get("pass_rate"))
        if pass_rate is not None:
            return "pass" if pass_rate >= threshold else "fail"

        for metric in ("hit_at_k", "hit_at_1", "mrr"):
            value = cls._roi_float(payload.get(metric))
            if value is not None:
                return "pass" if value >= threshold else "fail"

        pass_count = cls._roi_int(payload.get("pass_count"))
        fail_count = cls._roi_int(payload.get("fail_count"))
        if pass_count is not None or fail_count is not None:
            passes = int(pass_count or 0)
            failures = int(fail_count or 0)
            if passes > 0 and failures == 0:
                return "pass"
            if failures > 0 and passes == 0:
                return "fail"
        return None

    @classmethod
    def _roi_is_replay_dataset_record(cls, record: RecordEnvelope) -> bool:
        if record.kind != "replay_result":
            return False
        payload = cls._roi_report_payload(record)
        if cls._roi_report_type(record) == REPLAY_DATASET_REPORT_TYPE:
            return True
        if "dataset_size" in payload and "sample_count" not in payload and "pass_count" not in payload and "verdict" not in payload:
            return True
        if "cases" in payload and str(record.meta.get("report_type") or "") != REAL_TASK_REPLAY_REPORT_TYPE:
            return True
        return False

    @classmethod
    def _roi_is_replay_metric_record(cls, record: RecordEnvelope) -> bool:
        if record.kind != "replay_result":
            return False
        if cls._roi_is_replay_dataset_record(record):
            return False
        payload = cls._roi_report_payload(record)
        report_type = cls._roi_report_type(record)
        if report_type and report_type != REAL_TASK_REPLAY_REPORT_TYPE:
            return False
        verdict = str(payload.get("verdict") or "").strip().lower()
        if verdict in {"pass", "passed", "success", "fail", "failed", "failure"}:
            return True
        if cls._roi_float(payload.get("pass_rate")) is not None:
            return True
        return cls._roi_int(payload.get("pass_count")) is not None or cls._roi_int(payload.get("fail_count")) is not None

    @classmethod
    def _roi_is_real_task_replay_record(cls, record: RecordEnvelope) -> bool:
        if record.kind != "replay_result":
            return False
        payload = cls._roi_report_payload(record)
        if cls._roi_report_type(record) == REAL_TASK_REPLAY_REPORT_TYPE:
            return True
        return str(payload.get("replay_source") or "").strip().lower() == "real_task_replay"

    @classmethod
    def _roi_is_operational_incident(cls, record: RecordEnvelope) -> bool:
        payload = cls._roi_report_payload(record)
        incident_text = " ".join(
            [
                str(record.summary or ""),
                str(record.title or ""),
                str(record.detail or ""),
                str(payload.get("title") or ""),
                str(payload.get("incident_type") or ""),
                str(payload.get("incident_type_hint") or ""),
                str(payload.get("summary") or ""),
                str(payload.get("message") or ""),
            ]
        ).lower()
        return any(item in incident_text for item in _OPERATIONAL_INCIDENT_KEYWORDS)

    @staticmethod
    def _roi_float(value: Any, *, default: float | None = None) -> float | None:
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _roi_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def memory_quality_report(self, *, scope: dict, limit: int | None = None) -> dict:
        records = self._list_memory_records(scope=scope, limit=limit)
        quality_distribution = {
            "candidate": 0,
            "confirmed": 0,
            "core": 0,
            "rejected": 0,
        }
        salience_scores: list[float] = []
        by_source: dict[str, int] = {}
        by_memory_type: dict[str, int] = {}
        missing_quality_count = 0
        missing_score_count = 0
        v1_scores = []
        risk_label_distribution: Counter[str] = Counter()
        provenance_distribution: Counter[str] = Counter()
        provenance_source_distribution: Counter[str] = Counter()
        for record in records:
            quality = dict(record.meta.get("quality") or {})
            tier = str(quality.get("quality_tier") or "").strip().lower()
            capture_decision = str(quality.get("capture_decision") or "").strip().lower()
            if record.status == "rejected" or capture_decision == "reject":
                tier = "rejected"
            elif tier not in quality_distribution:
                tier = "candidate"
                missing_quality_count += 1
            quality_distribution[tier] += 1

            try:
                salience_scores.append(float(quality["salience_score"]))
            except (KeyError, TypeError, ValueError):
                pass

            source = str(record.source or "unknown")
            by_source[source] = by_source.get(source, 0) + 1
            memory_type = str(
                record.meta.get("memory_type")
                or record.content.get("memory_type")
                or "unknown"
            )
            by_memory_type[memory_type] = by_memory_type.get(memory_type, 0) + 1

            score = extract_memory_score(record.meta)
            if score is None:
                missing_score_count += 1
                continue
            v1_scores.append(score)
            risk_label_distribution.update(str(label) for label in (score.explanation.get("risk_labels") or []))
            provenance_label = str(
                score.components.get("provenance").evidence.get("label")
                if score.components.get("provenance") is not None
                else "provenance.unknown"
            )
            provenance_distribution[provenance_label] += 1
            provenance_source_distribution[str(score.provenance.source or "unknown")] += 1

        total_count = len(records)
        accepted_count = total_count - quality_distribution["rejected"]
        scoring_summary = summarize_scores(v1_scores)
        return {
            "memory_count": total_count,
            "accepted_count": accepted_count,
            "rejected_count": quality_distribution["rejected"],
            "quality_distribution": quality_distribution,
            "average_salience": round(mean(salience_scores), 3) if salience_scores else 0.0,
            "scored_count": len(salience_scores),
            "missing_quality_count": missing_quality_count,
            "by_source": dict(sorted(by_source.items())),
            "by_memory_type": dict(sorted(by_memory_type.items())),
            "scoring_v1": {
                "count": scoring_summary["count"],
                "tier_distribution": dict(sorted(scoring_summary["tiers"].items())),
                "average_final_score": scoring_summary["average_final_score"],
                "average_component_scores": dict(sorted(scoring_summary["average_components"].items())),
                "missing_score_count": missing_score_count,
                "risk_label_distribution": dict(sorted(risk_label_distribution.items())),
                "provenance_distribution": dict(sorted(provenance_distribution.items())),
                "provenance_source_distribution": dict(sorted(provenance_source_distribution.items())),
            },
        }

    def repair_memory_quality(
        self,
        *,
        scope: dict,
        apply: bool = False,
        limit: int | None = None,
    ) -> dict:
        records = self._list_memory_records(scope=scope, limit=limit)
        actions: list[dict] = []
        updated: dict[str, RecordEnvelope] = {}
        rejected_ids: set[str] = set()

        for record in records:
            quality = record.meta.get("quality")
            if not isinstance(quality, dict):
                repaired_quality = evaluate_memory_quality(
                    text=_memory_text(record),
                    title=record.title,
                    memory_type=str(record.meta.get("memory_type") or record.content.get("memory_type") or ""),
                    source=record.source,
                    force_capture=bool(record.meta.get("force_capture") or record.content.get("force_capture")),
                )
                actions.append(
                    {
                        "action": "backfill_quality",
                        "record_id": record.record_id,
                        "quality_tier": repaired_quality["quality_tier"],
                        "salience_score": repaired_quality["salience_score"],
                    }
                )
                if apply:
                    record.meta["quality"] = repaired_quality
                    updated[record.record_id] = record
            elif extract_memory_score(record.meta) is None:
                repaired_score = score_from_legacy_quality(
                    record=record,
                    activity="quality.repair",
                    source="quality.repair",
                )
                actions.append(
                    {
                        "action": "backfill_score_v1",
                        "record_id": record.record_id,
                        "quality_tier": repaired_score.tier,
                        "final_score": repaired_score.final_score,
                    }
                )
                if apply:
                    record.meta = with_score_metadata(record.meta, repaired_score, preserve_quality=True)
                    updated[record.record_id] = record

            if record.status != "rejected" and _is_mojibake_or_noisy_memory(record):
                actions.append(
                    {
                        "action": "reject",
                        "reason": "mojibake_or_noise",
                        "record_id": record.record_id,
                    }
                )
                rejected_ids.add(record.record_id)
                if apply:
                    record.status = "rejected"
                    record.meta["rejection_reason"] = "mojibake_or_noise"
                    record.touch()
                    updated[record.record_id] = record

        duplicate_actions = self._duplicate_repair_actions(records, rejected_ids)
        actions.extend(duplicate_actions)
        for action in duplicate_actions:
            rejected_ids.add(str(action["record_id"]))
            if not apply:
                continue
            record = next((item for item in records if item.record_id == action["record_id"]), None)
            if record is None:
                continue
            record.status = "rejected"
            record.meta["duplicate_of"] = action["duplicate_of"]
            record.meta["rejection_reason"] = "duplicate"
            record.touch()
            updated[record.record_id] = record

        if apply:
            for record in updated.values():
                self.store.append(record)

        quality_backfilled_count = sum(1 for action in actions if action["action"] == "backfill_quality")
        score_backfilled_count = sum(1 for action in actions if action["action"] == "backfill_score_v1")
        return {
            "scanned_count": len(records),
            "backfilled_count": quality_backfilled_count + score_backfilled_count,
            "quality_backfilled_count": quality_backfilled_count,
            "score_backfilled_count": score_backfilled_count,
            "rejected_count": len(rejected_ids),
            "duplicate_count": len(duplicate_actions),
            "applied": bool(apply),
            "actions": actions,
        }

    def _duplicate_repair_actions(
        self,
        records: list[RecordEnvelope],
        rejected_ids: set[str],
    ) -> list[dict]:
        buckets: dict[str, list[RecordEnvelope]] = {}
        for record in records:
            if record.status == "rejected" or record.record_id in rejected_ids:
                continue
            key = _duplicate_text_key(record)
            if not key:
                continue
            buckets.setdefault(key, []).append(record)

        actions: list[dict] = []
        for duplicate_records in buckets.values():
            if len(duplicate_records) < 2:
                continue
            kept = sorted(
                duplicate_records,
                key=lambda item: (-_record_salience(item), item.time.created_at, item.record_id),
            )[0]
            for duplicate in duplicate_records:
                if duplicate.record_id == kept.record_id:
                    continue
                actions.append(
                    {
                        "action": "reject",
                        "reason": "duplicate",
                        "record_id": duplicate.record_id,
                        "duplicate_of": kept.record_id,
                    }
                )
        return actions

    def reflection_stats(self, *, scope: dict) -> dict:
        reflections = self.store.list_records(kinds=["reflection"], scope=scope, limit=500)
        incidents = self.store.list_records(kinds=["incident"], scope=scope, limit=500)
        unknowns = self.store.list_records(kinds=["unknown"], scope=scope, limit=500)
        tags: dict[str, int] = {}
        for item in reflections:
            tag = str(item.meta.get("tag") or "untagged")
            tags[tag] = tags.get(tag, 0) + 1
        return {
            "reflection_count": len(reflections),
            "incident_count": len(incidents),
            "unknown_count": len(unknowns),
            "tags": tags,
        }

    def reflection_check(self, *, scope: dict) -> dict:
        reflections = self.store.list_records(kinds=["reflection"], scope=scope, limit=1)
        incidents = self.store.list_records(kinds=["incident", "unknown"], scope=scope, limit=50)
        latest_reflection_time = reflections[0].time.updated_at if reflections else ""
        pending = [
            item for item in incidents
            if not latest_reflection_time or item.time.updated_at >= latest_reflection_time
        ]
        status = "ALERT" if pending else "OK"
        return {
            "status": status,
            "pending_count": len(pending),
            "latest_reflection_at": latest_reflection_time,
        }

    def reconcile_knowledge(self, *, scope: dict) -> dict:
        scope_ref = ScopeRef.from_dict(scope)
        claims = self.store.list_records(kinds=["claim_card"], scope=scope_ref, limit=1000)
        pages = self.store.list_records(kinds=["knowledge_page"], scope=scope_ref, limit=1000)
        existing_relations = self.store.list_records(kinds=["relation_record"], scope=scope_ref, limit=1000)
        existing_pairs = {
            tuple(sorted([str(record.content.get("subject_id") or ""), str(record.content.get("object_id") or "")]))
            for record in existing_relations
            if record.content.get("relation_type") == "contradicts"
        }
        new_contradiction_ids: list[str] = []
        conflicted_claim_ids: set[str] = set()
        conflicted_source_ids: set[str] = set()
        for index, left in enumerate(claims):
            for right in claims[index + 1:]:
                if not _claims_conflict(left.summary, right.summary):
                    continue
                pair_key = tuple(sorted([left.record_id, right.record_id]))
                conflicted_claim_ids.update(pair_key)
                conflicted_source_ids.update(_claim_source_ids(left))
                conflicted_source_ids.update(_claim_source_ids(right))
                self._demote_conflicting_claim(left, pair_key)
                self._demote_conflicting_claim(right, pair_key)
                if pair_key in existing_pairs:
                    continue
                relation = RelationRecord(
                    relation_record_id=_stable_evolution_id("rel", *pair_key, "contradicts"),
                    paper_source_id=next(iter(_claim_source_ids(left) or _claim_source_ids(right) or {"unknown"})),
                    subject_id=left.record_id,
                    object_id=right.record_id,
                    relation_type="contradicts",
                    evidence_text=f"{left.summary} <> {right.summary}",
                    confidence=0.7,
                    provenance={"detector": "lexical_negation.v1"},
                ).to_record(scope=scope_ref, source="eimemory.evolution.reconcile")
                stored = self.store.append(relation)
                new_contradiction_ids.append(stored.record_id)
                existing_pairs.add(pair_key)
        page_refresh_count = 0
        all_contradiction_ids = set(new_contradiction_ids)
        for page in pages:
            page_claim_ids = set(str(item) for item in (page.content.get("supporting_claim_ids") or []))
            page_source_ids = set(str(item) for item in (page.content.get("source_ids") or []))
            if not (page_claim_ids & conflicted_claim_ids or page_source_ids & conflicted_source_ids):
                continue
            page.status = "needs_refresh"
            contradiction_ids = set(str(item) for item in (page.content.get("contradiction_ids") or []))
            contradiction_ids.update(all_contradiction_ids)
            page.content["contradiction_ids"] = sorted(contradiction_ids)
            page.meta["contradiction_ids"] = sorted(contradiction_ids)
            page.meta["refresh_reason"] = "claim_contradiction"
            page.touch()
            self.store.append(page)
            page_refresh_count += 1
        relation_count = len(
            [
                record
                for record in self.store.list_records(kinds=["relation_record"], scope=scope_ref, limit=1000)
                if record.content.get("relation_type") == "contradicts"
            ]
        )
        return {
            "ok": True,
            "claim_count": len(claims),
            "contradiction_count": relation_count,
            "new_contradiction_count": len(new_contradiction_ids),
            "page_refresh_count": page_refresh_count,
            "conflicted_claim_count": len(conflicted_claim_ids),
        }

    def _demote_conflicting_claim(self, claim: RecordEnvelope, contradiction_pair: tuple[str, str]) -> None:
        current = float(claim.meta.get("reliability", claim.meta.get("confidence", 0.5)) or 0.5)
        demoted = max(0.1, round(current * 0.75, 3))
        contradiction_ids = set(str(item) for item in (claim.meta.get("contradiction_claim_ids") or []))
        contradiction_ids.update(contradiction_pair)
        contradiction_ids.discard(claim.record_id)
        claim.status = "conflicted"
        claim.meta["reliability"] = demoted
        claim.meta["contradiction_claim_ids"] = sorted(contradiction_ids)
        claim.content["confidence"] = demoted
        claim.content["contradiction_claim_ids"] = sorted(contradiction_ids)
        claim.touch()
        self.store.append(claim)

    def _list_memory_records(self, *, scope: dict, limit: int | None = None) -> list[RecordEnvelope]:
        page_size = 500
        records: list[RecordEnvelope] = []
        offset = 0
        while limit is None or len(records) < limit:
            current_limit = page_size if limit is None else min(page_size, limit - len(records))
            if current_limit <= 0:
                break
            page = self.store.list_records(
                kinds=["memory"],
                scope=scope,
                limit=current_limit,
                offset=offset,
            )
            records.extend(page)
            if len(page) < current_limit:
                break
            offset += len(page)
        return records

    def _sample_hit(
        self,
        *,
        results: list[RecordEnvelope],
        expected_titles: set[str],
        expected_ids: set[str],
        expected_kinds: set[str],
    ) -> bool:
        if not expected_titles and not expected_ids and not expected_kinds:
            return bool(results)
        for result in results:
            if expected_titles and result.title not in expected_titles:
                continue
            if expected_ids and result.record_id not in expected_ids:
                continue
            if expected_kinds and result.kind not in expected_kinds:
                continue
            return True
        return False

    def _normalize_recall_profile(self, value: object) -> str:
        profile = str(value or "").strip().lower()
        if profile in {"precision", "balanced", "exploratory"}:
            return profile
        return ""

    def _normalize_pass_rate_threshold(self, value: object) -> float:
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            return 0.8
        if not math.isfinite(threshold):
            return 0.8
        return max(0.0, min(1.0, threshold))

    def _latest_feedback_for_rule(
        self,
        *,
        rule_id: str,
        feedback_records: list[RecordEnvelope],
    ) -> RecordEnvelope | None:
        for record in feedback_records:
            target_ref = record.meta.get("target_ref")
            if not isinstance(target_ref, dict):
                continue
            if str(target_ref.get("record_id") or "") == rule_id:
                return record
        return None

    def _latest_replay_for_rule(
        self,
        *,
        rule_id: str,
        replay_results: list[RecordEnvelope],
    ) -> RecordEnvelope | None:
        for record in replay_results:
            if str(record.meta.get("target_rule_id") or "") == rule_id:
                return record
        return None

    def _feedback_snapshot(self, record: RecordEnvelope | None) -> dict:
        if record is None:
            return {}
        return {
            "record_id": record.record_id,
            "decision": str(record.meta.get("decision") or ""),
            "reviewed_by": str(record.meta.get("reviewed_by") or ""),
            "reason": str(record.summary or ""),
        }

    def _replay_snapshot(self, record: RecordEnvelope | None) -> dict:
        if record is None:
            return {}
        return {
            "record_id": record.record_id,
            "pass_rate": round(float(record.meta.get("pass_rate") or 0.0), 3),
            "sample_size": int(record.meta.get("sample_size") or 0),
            "verdict": str(record.meta.get("verdict") or ""),
        }

    def _promotion_evidence(self, feedback: RecordEnvelope | None, replay: RecordEnvelope | None) -> list[dict]:
        evidence: list[dict] = []
        if feedback is not None:
            evidence.append(
                {
                    "kind": "feedback",
                    "record_id": feedback.record_id,
                    "decision": str(feedback.meta.get("decision") or ""),
                    "reviewed_by": str(feedback.meta.get("reviewed_by") or ""),
                }
            )
        if replay is not None:
            evidence.append(
                {
                    "kind": "replay_result",
                    "record_id": replay.record_id,
                    "pass_rate": round(float(replay.meta.get("pass_rate") or 0.0), 3),
                    "verdict": str(replay.meta.get("verdict") or ""),
                }
            )
        return evidence


def _claims_conflict(left: str, right: str) -> bool:
    left_norm = _claim_basis(left)
    right_norm = _claim_basis(right)
    if left_norm != right_norm:
        return False
    return _is_negated(left) != _is_negated(right)


def _claim_basis(text: str) -> str:
    lowered = re.sub(r"[^a-z0-9\s]", " ", str(text).lower())
    lowered = re.sub(r"\b(does|do|did|not|no|never|fails?|failed|cannot|can|is|are|the|a|an|to)\b", " ", lowered)
    lowered = lowered.replace("improves", "improve").replace("reduced", "reduce")
    return " ".join(lowered.split())


def _is_negated(text: str) -> bool:
    lowered = str(text).lower()
    return bool(re.search(r"\b(does not|do not|did not|not|no|never|fails? to|cannot)\b", lowered))


def _claim_source_ids(claim: RecordEnvelope) -> set[str]:
    source_ids = {str(claim.provenance.get("paper_source_id") or claim.meta.get("paper_source_id") or "")}
    return {source_id for source_id in source_ids if source_id}


def _stable_evolution_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _memory_text(record: RecordEnvelope) -> str:
    return str(record.content.get("text") or record.summary or record.detail or record.title)


def _duplicate_text_key(record: RecordEnvelope) -> str:
    text = _memory_text(record)
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return normalized if len(normalized) >= 12 else ""


def _record_salience(record: RecordEnvelope) -> float:
    quality = record.meta.get("quality") if isinstance(record.meta, dict) else {}
    if not isinstance(quality, dict):
        quality = evaluate_memory_quality(
            text=_memory_text(record),
            title=record.title,
            memory_type=str(record.meta.get("memory_type") or record.content.get("memory_type") or ""),
            source=record.source,
            force_capture=bool(record.meta.get("force_capture") or record.content.get("force_capture")),
        )
    try:
        return float(quality.get("salience_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_mojibake_or_noisy_memory(record: RecordEnvelope) -> bool:
    text = _memory_text(record)
    stripped = text.strip()
    if not stripped:
        return True
    marker_count = stripped.count("?") + stripped.count("\ufffd")
    visible_count = sum(1 for char in stripped if not char.isspace())
    alnum_count = sum(1 for char in stripped if char.isalnum())
    if visible_count == 0:
        return True
    if marker_count >= 4 and marker_count / visible_count >= 0.35:
        return True
    return marker_count >= 3 and alnum_count == 0
