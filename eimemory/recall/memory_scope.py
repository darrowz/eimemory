"""Explicit task applicability, shared by extraction and persona presentation."""
import re


_TASK_SCOPE = re.compile(
    r"本次|这次|此次|本轮|这一轮|当前任务|本任务|本单|"
    r"\b(?:this|current)\s+(?:task|run|turn|request|session)\b|\bfor now\b",
    re.I,
)


def is_task_scoped_memory(text: str) -> bool:
    # ponytail: explicit scope only; implicit applicability needs semantic review.
    return bool(_TASK_SCOPE.search(text))
