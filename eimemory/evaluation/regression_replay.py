"""Regression replay cases for known real mistakes."""

from __future__ import annotations

from typing import Any

from eimemory.governance.learning_state import stable_semantic_key


REGRESSION_REPLAY_CASE_REPORT_TYPE = "regression_replay_case"

MISTAKE_TARGET_CAPABILITIES: dict[str, str] = {
    "version_answer_wrong": "evidence.query_first",
    "evidence_not_checked": "evidence.query_first",
    "long_task_lost_contact": "task.progress",
    "field_mapping_wrong": "data.mapping",
    "eval_claim_without_run": "evaluation.query_first",
}

REAL_REGRESSION_REPLAY_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "version_answer_wrong",
        "现在 eimemory 到哪个版本了？",
        ("query version evidence first", "report exact commit and deployed release", "do not answer from memory"),
    ),
    (
        "version_answer_wrong",
        "刚升级完，你确认当前源码版本和部署版本。",
        ("inspect pyproject and runtime health", "compare source commit with deployed release", "do not infer from previous turn"),
    ),
    (
        "version_answer_wrong",
        "1.4.3 现在状态怎么样？",
        ("check source version before status", "check deployed health endpoint", "separate feature status from benchmark status"),
    ),
    (
        "version_answer_wrong",
        "你现在用的是不是最新 eimemory？",
        ("query runtime /health", "query git HEAD", "say unknown if evidence is missing"),
    ),
    (
        "version_answer_wrong",
        "服务重启后起来了嘛？",
        ("check process owner", "check health endpoint", "check systemd user unit"),
    ),
    (
        "evidence_not_checked",
        "这个服务器 IP 是不是被墙了？",
        ("test tcp connectivity", "distinguish refused from timeout", "do not diagnose firewall without evidence"),
    ),
    (
        "evidence_not_checked",
        "你看看这个服务还健康不健康。",
        ("query health endpoint first", "inspect service status", "report exact failing check"),
    ),
    (
        "evidence_not_checked",
        "你现在有哪些能力？",
        ("inspect current code / runtime capability evidence", "separate implemented from proven", "do not rely on capability claims"),
    ),
    (
        "evidence_not_checked",
        "昨天那个测试服务器还能连上不？",
        ("test ssh port", "test auth path", "report connection refused / timeout / auth failure separately"),
    ),
    (
        "evidence_not_checked",
        "benchmark 跑完了吗？",
        ("check benchmark logs", "check process status", "do not claim scores before report exists"),
    ),
    (
        "long_task_lost_contact",
        "装一个 web app / 跑一轮完整 benchmark。",
        ("spawn subagent for tasks over 5 minutes", "main thread stays responsive", "send status before long wait"),
    ),
    (
        "long_task_lost_contact",
        "远端服务器拉新版然后重测。",
        ("delegate long remote benchmark", "record logs path", "return blocker instead of waiting silently"),
    ),
    (
        "long_task_lost_contact",
        "docker compose 调试一下。",
        ("architecture precheck first", "report within 5 minutes", "offer options before 20 minute threshold"),
    ),
    (
        "long_task_lost_contact",
        "SSH 到 honjia 跑个脚本。",
        ("wrap ssh in timeout", "avoid blocking main thread", "detach persistent processes"),
    ),
    (
        "long_task_lost_contact",
        "服务起后台以后你盯一下。",
        ("verify within 5 minutes", "check ps and logs", "notify if dead"),
    ),
    (
        "field_mapping_wrong",
        "把我口述的客户信息自动写进系统。",
        ("dry-run sample first", "confirm mapping with user", "do not bulk write before sample approval"),
    ),
    (
        "field_mapping_wrong",
        "福建省政府发展研究中心人事处林铁强。",
        ("treat organization as Account", "treat person as Contact", "do not create Project from organization name"),
    ),
    (
        "field_mapping_wrong",
        "50 万吨金矿项目，联系人王总。",
        ("project goes to Project", "person goes to Contact", "opportunity links account/project explicitly"),
    ),
    (
        "field_mapping_wrong",
        "签了 30 万合同，帮我记一下。",
        ("high-risk write needs preview", "amount/stage require confirmation", "external business write cannot be silent"),
    ),
    (
        "field_mapping_wrong",
        "给客户建任务并关联公司。",
        ("verify account/contact/project relationship", "do not swap account and project", "show one sample before batch"),
    ),
    (
        "eval_claim_without_run",
        "1.4.5 记忆质量是不是提升了？",
        ("run or locate benchmark report", "separate mechanism from measured quality", "do not claim improvement without scores"),
    ),
    (
        "eval_claim_without_run",
        "LoCoMO / LongMemEval 新分数多少？",
        ("provide exact report path", "provide run command", "say not run if report missing"),
    ),
    (
        "eval_claim_without_run",
        "这次优化有没有效果？",
        ("compare before and after metrics", "include regression replay result", "avoid subjective improvement claim"),
    ),
    (
        "eval_claim_without_run",
        "benchmark watcher 状态正常吗？",
        ("check watcher process and latest status timestamp", "report stale status as stale", "do not treat old scores as current"),
    ),
    (
        "eval_claim_without_run",
        "能不能证明自主进化变强了？",
        ("require replay / eval evidence", "separate execution channel from patch quality", "state unproven when evidence is absent"),
    ),
)


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


def built_in_real_regression_cases() -> list[dict[str, Any]]:
    """Return curated replay cases from real operator feedback and incidents."""

    return [
        mistake_to_replay_case(mistake_type, prompt, list(expected))
        for mistake_type, prompt, expected in REAL_REGRESSION_REPLAY_SPECS
    ]


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
