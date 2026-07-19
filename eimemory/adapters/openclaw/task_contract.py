from __future__ import annotations

from collections.abc import Iterable


def classify_openclaw_task_type(
    *,
    explicit: str = "",
    query: str = "",
    tools: Iterable[str] = (),
    action_path: Iterable[str] = (),
) -> str:
    """Classify a terminal OpenClaw task from bounded, execution-side evidence."""
    declared = str(explicit or "").strip()
    if declared and _normalized(declared) not in {"communication", "unknown", "unspecified"}:
        return declared[:160]

    tool_names = {_normalized(item) for item in [*tools, *action_path] if str(item or "").strip()}
    text = _normalized(query)

    if _contains_any(
        text,
        (
            "synthesize",
            "synthesis",
            "research paper",
            "literature review",
            "systematic review",
            "research findings",
            "论文",
            "文献综述",
            "研究材料",
            "研究结论",
            "综合分析",
            "归纳研究",
        ),
    ):
        return "research.synthesis"

    if _has_tool(tool_names, ("apply_patch", "pytest", "compiler", "code_edit", "write_file")) or _contains_any(
        text,
        (
            "patch the ",
            "implement ",
            "implementation",
            "refactor",
            "run pytest",
            "fix the code",
            "code review",
            "修复代码",
            "修改代码",
            "实现功能",
            "重构",
            "单元测试",
            "代码审计",
        ),
    ):
        return "code.implementation"

    if _has_tool(tool_names, ("systemctl", "kubectl", "docker", "journalctl", "ssh", "health")) or _contains_any(
        text,
        (
            "service health",
            "gateway health",
            "deployment status",
            "deploy ",
            "rollout status",
            "production status",
            "健康检查",
            "部署状态",
            "服务状态",
            "运行状态",
            "线上状态",
        ),
    ):
        return "ops.health"

    if _has_tool(
        tool_names,
        ("spreadsheet", "calendar", "email", "document", "slides", "presentation"),
    ) or _contains_any(
        text,
        (
            "spreadsheet",
            "calendar",
            "meeting notes",
            "action list",
            "daily report",
            "电子表格",
            "日历",
            "会议纪要",
            "行动清单",
            "日报",
        ),
    ):
        return "office.daily_task"

    if _has_tool(tool_names, ("web.search", "search_query", "browser.search", "github.search")) or _contains_any(
        text,
        (
            "search ",
            "find sources",
            "look up",
            "github releases",
            "搜索",
            "查找资料",
            "检索",
        ),
    ):
        return "search.discovery"

    if _has_tool(tool_names, ("memory", "eimemory")) or _contains_any(
        text,
        ("memory", "replay buffer", "记忆", "回放缓冲", "知识摄入"),
    ):
        return "memory.governance"

    if _has_tool(tool_names, ("message", "feishu", "slack", "teams", "wechat", "send")):
        return "communication.delivery"

    return "general.execution" if tool_names else "communication"


def _normalized(value: object) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _has_tool(tools: set[str], markers: tuple[str, ...]) -> bool:
    normalized_markers = tuple(_normalized(marker) for marker in markers)
    return any(any(marker in tool for marker in normalized_markers) for tool in tools)
