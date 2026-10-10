"""Pure, bounded research-review diagnostics for durable scheduler receipts."""
from __future__ import annotations

import re

from eimemory.llm.completion_timing import FAILURE_CATEGORIES, failure_category, safe_timing

REVIEW_ERROR_CODES = frozenset({
    "research_review_llm_unconfigured", "research_review_llm_configuration_invalid",
    "research_review_hermes_runtime_unavailable", "research_review_hermes_configuration_invalid",
    "research_review_command_not_found", "research_review_command_permission_denied",
    "research_review_command_timeout", "research_review_command_response_invalid",
    "research_review_execution_failed", "research_review_prompt_invalid",
    "research_review_output_empty", "research_review_output_invalid",
    "review_model_not_allowed", "review_model_invalid", "codex_review_command_failed",
    "command_completion_failed", "research_review_multiple_failures",
}) | frozenset(f"command_completion_failed:{category}" for category in FAILURE_CATEGORIES)


def _safe_error(value: object) -> str:
    return (value if isinstance(value, str) and value in REVIEW_ERROR_CODES
            else "reason_not_reported" if value is None or value == "" else "reason_not_allowlisted")


def research_review_diagnostics(report: object) -> dict:
    """Project fixed codes, internal record references and measured timings only.

    The durable supervisor receipt holds no raw artifacts or exception strings.
    Legacy reports with no reason stay explicitly unknown; never infer a cause.
    """
    report = report if isinstance(report, dict) else {}
    rows = report.get("unavailable_records")
    rows = rows if isinstance(rows, list) else []
    counts: dict[str, int] = {}
    projected = []
    for row in rows[:500]:
        row = row if isinstance(row, dict) else {}
        error = _safe_error(row.get("error"))
        counts[error] = counts.get(error, 0) + 1
        if len(projected) >= 20:
            continue
        item = {"error": error}
        record_id = row.get("record_id")
        if isinstance(record_id, str) and re.fullmatch(r"(?:replay|rec|ref)_[0-9a-f]{12}", record_id):
            item["record_id"] = record_id
        if row.get("stage") in ("prompt", "execution", "model_validation", "output_validation"):
            item["stage"] = row["stage"]
        if row.get("error_type") in ("RuntimeError", "ValueError", "StrictJSONError", "JSONDecodeError",
                                    "UnicodeDecodeError", "UnicodeEncodeError", "TimeoutExpired",
                                    "FileNotFoundError", "PermissionError", "CommandCompletionError"):
            item["error_type"] = row["error_type"]
        category = failure_category(row.get("failure_category"))
        if category:
            item["failure_category"] = category
        if row.get("validation_reason") in ("invalid_json", "invalid_json_type", "duplicate_key",
                                             "nonfinite_number", "json_too_large", "json_too_deep"):
            item["validation_reason"] = row["validation_reason"]
        timing = safe_timing(row.get("completion_timing"))
        if timing:
            item["completion_timing"] = timing
        projected.append(item)
    if not counts:
        counts[_safe_error(report.get("error"))] = 1
    return {"reason_counts": dict(sorted(counts.items())), "unavailable_records": projected,
            "unavailable_records_truncated": len(rows) > 20, "reasons_truncated": len(rows) > 500}
