"""Regression replay cases for known real mistakes."""

from __future__ import annotations

from typing import Any

from eimemory.governance.learning_state import stable_semantic_key


REGRESSION_REPLAY_CASE_REPORT_TYPE = "regression_replay_case"

MISTAKE_TARGET_CAPABILITIES: dict[str, str] = {
    "version_answer_wrong": "evidence.query_first",
    "long_task_lost_contact": "task.progress",
    "field_mapping_wrong": "data.mapping",
    "eval_claim_without_run": "evaluation.query_first",
}


def mistake_to_replay_case(mistake_type: str, prompt: str, expected: Any) -> dict[str, Any]:
    mistake = str(mistake_type or "").strip()
    if mistake not in MISTAKE_TARGET_CAPABILITIES:
        raise ValueError(f"unknown mistake_type: {mistake}")
    query = str(prompt or "").strip()
    expected_text = _strings(expected)
    target_capability = MISTAKE_TARGET_CAPABILITIES[mistake]
    return {
        "case_id": stable_semantic_key("regression_replay_case", mistake, query, *expected_text),
        "source": REGRESSION_REPLAY_CASE_REPORT_TYPE,
        "mistake_type": mistake,
        "query": query,
        "input": query,
        "prompt": query,
        "expected": expected_text[0] if expected_text else "",
        "expected_text": expected_text,
        "target_capability": target_capability,
        "task_type": target_capability,
        "labels": ["regression_replay_case", mistake],
    }


def run_regression_replay(cases: list[dict[str, Any]], answers: Any) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        sample = _run_case(case=dict(case), answers=answers, index=index)
        samples.append(sample)
    pass_count = sum(1 for sample in samples if sample["passed"])
    fail_count = len(samples) - pass_count
    return {
        "ok": True,
        "report_type": "regression_replay",
        "verdict": "pass" if fail_count == 0 else "fail",
        "sample_count": len(samples),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "samples": samples,
    }


def _run_case(*, case: dict[str, Any], answers: Any, index: int) -> dict[str, Any]:
    case_id = str(case.get("case_id") or case.get("id") or index)
    query = str(case.get("query") or case.get("input") or case.get("prompt") or "")
    expected_text = _strings(case.get("expected_text") or case.get("expect_any_text") or case.get("expected"))
    answer = _answer_for_case(answers=answers, case=case, case_id=case_id, index=index)
    answer_lower = answer.lower()
    missing = [item for item in expected_text if item.lower() not in answer_lower]
    passed = not missing
    return {
        "index": index,
        "case_id": case_id,
        "mistake_type": str(case.get("mistake_type") or ""),
        "target_capability": str(case.get("target_capability") or ""),
        "query": query,
        "expected_text": expected_text,
        "answer": answer,
        "passed": passed,
        "missing_expected_text": missing,
    }


def _answer_for_case(*, answers: Any, case: dict[str, Any], case_id: str, index: int) -> str:
    if isinstance(answers, dict):
        for key in (case_id, case.get("id"), case.get("query"), case.get("input"), case.get("prompt"), str(index), index):
            if key in answers:
                return str(answers[key] or "")
        return ""
    if isinstance(answers, (list, tuple)):
        return str(answers[index] or "") if index < len(answers) else ""
    return str(answers or "")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        value = value.get("expected_text") or value.get("text") or value.get("expected") or []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []
