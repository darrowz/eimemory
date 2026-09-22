"""ARCH-01: independent-evidence query policy constants/helpers for Data plane."""
from __future__ import annotations

import re
import unicodedata

POLICY = "independent-evidence-query.v1"
_UNSAFE = re.compile(
    r"只|不|勿|没|无|而|和|与|及|还是|或者|以及|同时|分别|所有|全部|最近|最新|上次|"
    r"今天|昨天|明天|现在|当前|此次|这次|如果|假如|除非|但|并且|是否|能否|"
    r"\b(?:only|not|without|rather|and|or|all|latest|last|recent|today|now|"
    r"if|unless|except|can|could|whether)\b",
    re.I,
)
_LABEL = re.compile(r"[\w\u3400-\u9fff][\w\u3400-\u9fff .:/-]{0,79}", re.UNICODE)


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def label(value: object) -> str:
    if not isinstance(value, str) or len(value) > 160:
        raise ValueError("query_label_invalid")
    result = normalized(value)
    if not _LABEL.fullmatch(result) or _UNSAFE.search(result):
        raise ValueError("query_label_invalid")
    return result
