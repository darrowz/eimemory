from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.intake.closure import DEFAULT_REVIEW_MODEL, RESEARCH_CLOSURE_REPORT_TYPE
from eimemory.models.records import RecordEnvelope, ScopeRef


ModelExecutor = Callable[[str, str], str]

import os
import re

# Optional allowlist via env (comma-separated). Empty = allow any non-empty model
# string that passes a light capability/safety check (vendor-neutral).
_MODEL_TOKEN = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


def _allowed_review_models() -> frozenset[str] | None:
    raw = os.environ.get("EIMEMORY_ALLOWED_REVIEW_MODELS", "").strip()
    if not raw:
        return None
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


# Back-compat name: None means unrestricted (env may still set a list).
ALLOWED_REVIEW_MODELS = _allowed_review_models() or frozenset()


def _validated_review_model(model: str) -> str:
    candidate = str(model or DEFAULT_REVIEW_MODEL).strip() or DEFAULT_REVIEW_MODEL
    allowed = _allowed_review_models()
    if allowed is not None and candidate not in allowed:
        raise ValueError(f"review_model_not_allowed:{candidate}")
    if _MODEL_TOKEN.fullmatch(candidate) is None:
        raise ValueError(f"review_model_invalid:{candidate}")
    return candidate


def review_pending_research_closures(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 20,
    review_model: str = DEFAULT_REVIEW_MODEL,
    executor: ModelExecutor | None = None,
) -> dict[str, Any]:
    """Consume pending research closure reviews with a real model review step.

    A pending research closure is not a closed loop. This runner either stores a
    model review result or marks the item explicitly unavailable, so operators
    can retry or inspect the real blocker instead of leaving a silent queue.
    """

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    review_model = _validated_review_model(review_model)
    records = [
        record
        for record in runtime.store.list_records(kinds=["replay_result"], scope=scope_ref, limit=max(1, int(limit or 1)))
        if _is_pending_research_closure(record)
    ]
    run = executor or codex_exec
    reviewed: list[dict[str, str]] = []
    unavailable: list[dict[str, str]] = []

    for record in records:
        prompt = build_research_closure_review_prompt(record)
        try:
            output = run(_validated_review_model(review_model), prompt).strip()
        except Exception as exc:  # pragma: no cover - subprocess failures differ by host
            rewritten = _rewrite_review_record(
                runtime,
                record,
                status="review_unavailable",
                review_model=str(review_model or DEFAULT_REVIEW_MODEL),
                review_output="",
                review_error=str(exc),
            )
            unavailable.append({"record_id": rewritten.record_id, "error": str(exc)})
            continue

        rewritten = _rewrite_review_record(
            runtime,
            record,
            status="reviewed",
            review_model=str(review_model or DEFAULT_REVIEW_MODEL),
            review_output=output,
            review_error="",
        )
        reviewed.append({"record_id": rewritten.record_id, "review_model_used": str(review_model or DEFAULT_REVIEW_MODEL)})

    return {
        "ok": True,
        "report_type": "research_closure_model_review",
        "scanned": len(records),
        "reviewed": len(reviewed),
        "unavailable": len(unavailable),
        "reviewed_records": reviewed,
        "unavailable_records": unavailable,
        "review_model": str(review_model or DEFAULT_REVIEW_MODEL),
        "fail_closed": True,
    }


def build_research_closure_review_prompt(record: RecordEnvelope) -> str:
    payload = {
        "task": "Review whether this research closure artifact is safe and actionable for eimemory.",
        "rules": [
            "Use only the provided artifact.",
            "Do not invent production facts.",
            "Return concise JSON with verdict, rationale, required_followup, and risk.",
            "Use verdict=approve only when the landing point and next action are directly supported.",
        ],
        "artifact": {
            "record_id": record.record_id,
            "title": record.title,
            "summary": record.summary,
            "content": record.content,
            "meta": record.meta,
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def codex_exec(model: str, prompt: str) -> str:
    result = subprocess.run(
        ["codex", "exec", "--model", model, "-"],
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if result.returncode != 0:
        stderr = str(result.stderr or "").strip()
        stdout = str(result.stdout or "").strip()
        raise RuntimeError(stderr or stdout or f"codex exec failed with exit {result.returncode}")
    return str(result.stdout or "").strip()


def _is_pending_research_closure(record: RecordEnvelope) -> bool:
    return (
        record.kind == "replay_result"
        and str(record.meta.get("report_type") or record.content.get("report_type") or "") == RESEARCH_CLOSURE_REPORT_TYPE
        and str(record.meta.get("review_status") or record.content.get("review_status") or "") == "pending_model_review"
    )


def _rewrite_review_record(
    runtime: Any,
    record: RecordEnvelope,
    *,
    status: str,
    review_model: str,
    review_output: str,
    review_error: str,
) -> RecordEnvelope:
    updated = RecordEnvelope.from_dict(record.to_dict())
    reviewed_at = now_iso()
    updated.content = {
        **dict(updated.content or {}),
        "review_status": status,
        "review_model_used": review_model if status == "reviewed" else "",
        "reviewed_at": reviewed_at,
        "model_review": review_output,
        "review_error": review_error,
    }
    updated.meta = {
        **dict(updated.meta or {}),
        "review_status": status,
        "review_model_used": review_model if status == "reviewed" else "",
        "reviewed_at": reviewed_at,
        "review_error": review_error,
    }
    updated.detail = _review_detail(updated.detail, status=status, review_output=review_output, review_error=review_error)
    updated.touch()
    return runtime.store.rewrite(updated, previous_scope=record.scope)


def _review_detail(detail: str, *, status: str, review_output: str, review_error: str) -> str:
    suffix = f"\n\nModel review status: {status}"
    if review_output:
        suffix = f"{suffix}\n{review_output}"
    if review_error:
        suffix = f"{suffix}\nreview_error: {review_error}"
    return f"{str(detail or '').rstrip()}{suffix}"


def retry_unavailable_research_closures(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Requeue review_unavailable closures so INT-19 does not permanently stall."""
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    reset: list[str] = []
    for record in runtime.store.list_records(
        kinds=["replay_result"], scope=scope_ref, limit=max(1, int(limit or 1)) * 4
    ):
        content = dict(record.content or {})
        if str(content.get("report_type") or "") != RESEARCH_CLOSURE_REPORT_TYPE:
            continue
        if str(content.get("review_status") or "") != "review_unavailable":
            continue
        content["review_status"] = "pending"
        content["review_error"] = ""
        content["review_retry_at"] = now_iso()
        record.content = content
        record.touch()
        runtime.store.rewrite(record) if hasattr(runtime.store, "rewrite") else runtime.store.append(record)
        reset.append(record.record_id)
        if len(reset) >= max(1, int(limit or 1)):
            break
    return {"ok": True, "requeued": len(reset), "record_ids": reset}
