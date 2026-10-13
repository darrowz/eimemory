"""Bounded multilingual keyword extraction for redacted review packets.

Chinese has no mandatory word separators. Use function-word boundaries and
short, non-overlapping anchors rather than persisting a continuous sentence.
This is a query representation only; it does not establish label relevance.
"""
from __future__ import annotations

import re
from unicodedata import normalize

from .real_query_schema import _contains_person_entity, _looks_like_secret, _query_feature_tokens

# Language particles/question scaffolding, deliberately independent of the
# application's people, products and domain vocabulary.
_CJK_SCAFFOLD = frozenset({
    "请问", "告诉", "知道", "想知道", "需要", "应该", "可以", "能否", "是否", "有没有",
    "如何", "怎么", "怎样", "为什么", "什么", "哪些", "哪个", "多少", "具体", "到底",
    "之前", "之后", "时候", "最近", "现在", "上次", "上周", "昨天", "今天", "发生",
    "请", "我", "你", "他", "她", "它", "们", "的", "了", "呢", "吗", "是", "在",
    "与", "和", "把", "对", "给", "将", "从", "到", "要", "有", "不", "想",
    "记忆", "召回", "查询", "案例", "采集", "审计",
})
_CJK_SPLIT = re.compile("|".join(re.escape(word) for word in sorted(_CJK_SCAFFOLD, key=lambda s: (-len(s), s))))
_IDENTITY_MARKER = re.compile(r"(?:姓名|联系人|收件人|我叫|名字是|用户姓名)\s*[:：]?\s*[\u3400-\u9fff]{2,}")
_CREDENTIAL = re.compile(r"(?i)(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]{8,}")
# Unicode word boundaries treat adjacent Chinese as part of an ASCII address
# or credential. Check the original text without those boundaries so splitting
# the query cannot strip the identifying punctuation and expose its value.
_EMAIL = re.compile(r"(?i)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:password|passwd|passphrase|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|session[_-]?cookie|authorization|credential)s?\s*[:=]"
    r"|bearer\s+|(?:AKIA|ASIA)[A-Z0-9]{16}")


def private_query_tokens(query: str) -> tuple[list[str], str]:
    text = normalize("NFKC", query)
    # Inspect before tokenization: splitting an email, named person or credential
    # must not erase the markers that the redaction contract relies on.
    if (_looks_like_secret(text) or _contains_person_entity(text)
            or _IDENTITY_MARKER.search(text) or _CREDENTIAL.search(text)
            or _EMAIL.search(text) or _SECRET_ASSIGNMENT.search(text)):
        return [], "query_features_not_redacted"
    tokens: list[str] = []
    cjk_terms: list[str] = []
    for token in _query_feature_tokens(text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) is None:
            tokens.append(token)
            continue
        for part in _CJK_SPLIT.split(token):
            if len(part) < 2:
                continue
            if len(part) <= 3:
                anchors = [part]
            else:
                # At most three spaced anchors per phrase. Longer phrases retain
                # gaps; we never produce every sliding bigram of a sentence.
                positions = ([0, len(part) - 2] if len(part) < 8 else
                             [0, (len(part) - 2) // 2, len(part) - 2])
                anchors = [part[position:position + 2] for position in positions]
            for anchor in anchors:
                if anchor not in _CJK_SCAFFOLD and anchor not in cjk_terms:
                    cjk_terms.append(anchor)
    # Unordered keywords carry no sentence sequence. A six-anchor cap retains
    # at most eighteen CJK characters irrespective of the original input size.
    return tokens + sorted(cjk_terms[:6]), ""
