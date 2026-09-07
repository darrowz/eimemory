"""Bounded, local cross-encoder admission; similarity is not answer probability.

Only authoritative, already-authorized records may reach this layer. Scores and
configuration are versioned, without retaining queries, texts or credentials.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import ipaddress
import json
import math
import os
import re
from threading import BoundedSemaphore
from time import perf_counter
from typing import Mapping
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from eimemory.models.identity_aliases import normalize_identity_text
from eimemory.models.records import RecordEnvelope


class RelevanceUnavailable(RuntimeError):
    """Public error codes never contain remote bodies, text or secrets."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RelevanceUnavailable("reranker_redirect_rejected")


@dataclass(frozen=True)
class RelevanceConfig:
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:8089"
    api_key: str = field(default="", repr=False)
    model: str = "BAAI/bge-reranker-base"
    revision: str = ""
    max_candidates: int = 12
    max_text_chars: int = 700
    timeout_seconds: float = 2.0
    min_score: float = 0.0  # raw logit, explicitly not a probability
    calibration: str = "unvalidated"

    def __post_init__(self):
        endpoint = urlsplit(self.endpoint)
        try:
            loopback = ipaddress.ip_address(endpoint.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if (endpoint.scheme != "http" or not loopback or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment or endpoint.path not in {"", "/"}):
            raise ValueError("reranker_requires_literal_loopback_endpoint")
        if (not 1 <= self.max_candidates <= 32 or not 128 <= self.max_text_chars <= 4000
                or not math.isfinite(self.timeout_seconds) or not 0.05 <= self.timeout_seconds <= 10
                or not math.isfinite(self.min_score)):
            raise ValueError("reranker_bounds_invalid")
        if self.enabled and (not self.api_key or not re.fullmatch(r"[0-9a-f]{40}", self.revision)
                             or not self.model or len(self.model) > 120):
            raise ValueError("reranker_identity_or_auth_missing")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None):
        env = os.environ if env is None else env
        if env.get("EIMEMORY_RERANKER_ENABLED", "0") != "1":
            return cls()
        return cls(enabled=True,
            endpoint=env.get("EIMEMORY_RERANKER_URL", "http://127.0.0.1:8089"),
            api_key=env.get("EIMEMORY_RERANKER_API_KEY", ""),
            model=env.get("EIMEMORY_RERANKER_MODEL", "BAAI/bge-reranker-base"),
            revision=env.get("EIMEMORY_RERANKER_REVISION", ""),
            max_candidates=int(env.get("EIMEMORY_RERANKER_MAX_CANDIDATES", "12")),
            max_text_chars=int(env.get("EIMEMORY_RERANKER_TEXT_CHARS", "700")),
            timeout_seconds=float(env.get("EIMEMORY_RERANKER_TIMEOUT_SECONDS", "2")),
            min_score=float(env.get("EIMEMORY_RERANKER_MIN_SCORE", "0")),
            calibration=env.get("EIMEMORY_RERANKER_CALIBRATION", "unvalidated"))

    def identity(self):
        return {"policy": "cross-encoder-admission.v1", "enabled": self.enabled,
                "model": self.model, "revision": self.revision,
                "max_candidates": self.max_candidates, "max_text_chars": self.max_text_chars,
                "timeout_seconds": self.timeout_seconds, "min_raw_score": self.min_score,
                "score_kind": "raw_logit", "calibration": self.calibration,
                "projection": "authoritative-distinct-spans.v1"}


class TEIReranker:
    def __init__(self, config: RelevanceConfig):
        self.config = config
        self._slot = BoundedSemaphore(1)
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def score(self, query: str, texts: list[str], *, timeout_seconds: float | None = None):
        if not texts:
            return []
        if len(texts) > self.config.max_candidates or len(query) > 16000:
            raise RelevanceUnavailable("reranker_input_bound")
        if not self._slot.acquire(blocking=False):
            raise RelevanceUnavailable("reranker_busy")
        try:
            timeout = min(self.config.timeout_seconds, timeout_seconds or self.config.timeout_seconds)
            if timeout < 0.05:
                raise RelevanceUnavailable("reranker_deadline_exceeded")
            payload = json.dumps({"query": query, "texts": texts, "raw_scores": True,
                                  "truncate": True, "return_text": False}).encode()
            request = Request(self.config.endpoint.rstrip("/") + "/rerank", data=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + self.config.api_key}, method="POST")
            with self._opener.open(request, timeout=timeout) as response:
                body = response.read(65537)
            if len(body) > 65536:
                raise RelevanceUnavailable("reranker_response_bound")
            return validate_scores(json.loads(body), len(texts))
        except RelevanceUnavailable:
            raise
        except Exception:
            raise RelevanceUnavailable("reranker_request_failed") from None
        finally:
            self._slot.release()


def validate_scores(rows, count: int) -> list[float]:
    if not isinstance(rows, list) or len(rows) != count:
        raise RelevanceUnavailable("reranker_response_invalid")
    scores = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RelevanceUnavailable("reranker_response_invalid")
        index, score = row.get("index"), row.get("score")
        if (type(index) is not int or not 0 <= index < count or index in scores
                or type(score) not in (float, int) or not math.isfinite(score)):
            raise RelevanceUnavailable("reranker_response_invalid")
        scores[index] = float(score)
    return [scores[i] for i in range(count)]


def record_digest(record: RecordEnvelope) -> str:
    return sha256(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()


def authoritative_text(record: RecordEnvelope, *, max_chars: int) -> str:
    """Do not repeat summary/title or serialize operational metadata as facts.

    Keep the title, then distinct original body spans. A long body retains both
    its start and end instead of silently using a title-only prefix.
    """
    content = record.content if isinstance(record.content, dict) else {}
    parts = []
    for value in (record.detail, content.get("text"), content.get("content"),
                  content.get("value"), record.summary):
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if not any(value in previous for previous in parts):
                parts = [previous for previous in parts if previous not in value]
                parts.append(value)
    title = record.title.strip()[:min(120, max_chars // 4)]
    body = "\n".join(parts)
    if not body:
        return record.title[:max_chars]
    if title and title not in body:
        body = title + "\n" + body
    if len(body) <= max_chars:
        return body
    head = max_chars * 2 // 3
    return body[:head] + "\n…\n" + body[-(max_chars - head - 3):]


class RelevanceAdmission:
    def __init__(self, config: RelevanceConfig, scorer=None):
        self.config = config
        self.scorer = scorer if scorer is not None else TEIReranker(config)

    def select(self, items, *, query, limit, validate, deadline_at=0.0):
        started = perf_counter()
        dropped: dict[str, int] = {}
        def drop(reason):
            dropped[reason] = dropped.get(reason, 0) + 1

        valid, seen = [], set()
        for record in items:
            if not validate(record):
                drop("authority_changed_or_forbidden")
                continue
            text = authoritative_text(record, max_chars=self.config.max_text_chars)
            # Deduplicate within the same permission/source partition only.
            key = (tuple(asdict(record.scope).values()), record.source_id, text)
            if key in seen:
                drop("duplicate_content")
                continue
            seen.add(key)
            valid.append((record, text))
        normalized = normalize_identity_text(query)
        exact = [record for record, _ in valid if normalized and normalized in
                 {normalize_identity_text(record.record_id), normalize_identity_text(record.title)}]
        if exact:
            chosen = exact[:limit]
            status, mode, scored = "evidence_found", "identity_lookup", []
        else:
            bounded = valid[:self.config.max_candidates]
            if len(valid) > len(bounded):
                dropped["candidate_budget"] = len(valid) - len(bounded)
            scored = []
            try:
                remaining = deadline_at - perf_counter() if deadline_at else self.config.timeout_seconds
                if remaining < 0.05:
                    raise RelevanceUnavailable("reranker_deadline_exceeded")
                scores = self.scorer.score(query, [text for _, text in bounded], timeout_seconds=remaining)
                if len(scores) != len(bounded) or any(type(s) not in (int, float) or not math.isfinite(s) for s in scores):
                    raise RelevanceUnavailable("reranker_response_invalid")
                ranked = sorted(zip(bounded, scores), key=lambda pair: -pair[1])
                chosen = []
                for (record, _), score in ranked:
                    admitted = score >= self.config.min_score
                    scored.append({"record_id": record.record_id, "source_id": record.source_id,
                                   "score": score, "admitted": admitted})
                    if admitted:
                        chosen.append(record)
                    else:
                        drop("insufficient_relevance")
                chosen = chosen[:limit]
                status = "evidence_found" if chosen else "no_evidence"
                mode = "cross_encoder"
            except RelevanceUnavailable as exc:
                chosen, status, mode = [], "unavailable", "fail_closed"
                drop(str(exc))
        # Re-read after inference; even a matching ID cannot exempt mutations.
        selected = []
        for record in chosen:
            if validate(record):
                selected.append(record)
            else:
                drop("authority_changed_during_scoring")
        if chosen and not selected:
            status = "unavailable"
        return selected, {**self.config.identity(), "status": status, "mode": mode,
            "candidate_count": len(valid), "selected_count": len(selected),
            "scored": scored, "dropped_reasons": dropped,
            "elapsed_ms": round((perf_counter() - started) * 1000, 3)}
