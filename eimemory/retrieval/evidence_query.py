"""Closed, full-question grammar for reviewed business evidence contracts.

No candidate text, regular expressions supplied by a reviewer, model-generated
aliases, substring matching, or similarity participates in query interpretation.
Unsupported forms are a semantic-verifier fallback, never a negative answer.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

POLICY = 'independent-evidence-query.v1'
# Deliberately narrower than general language understanding.
_UNSAFE = re.compile(
    r'只|不|勿|没|无|而|和|与|及|还是|或者|以及|同时|分别|所有|全部|最近|最新|上次|'
    r'今天|昨天|明天|现在|当前|此次|这次|如果|假如|除非|但|并且|是否|能否|'
    r'\b(?:only|not|without|rather|and|or|all|latest|last|recent|today|now|'
    r'if|unless|except|can|could|whether)\b', re.I)
_LABEL = re.compile(r'[\w\u3400-\u9fff][\w\u3400-\u9fff .:/-]{0,79}', re.UNICODE)
_PATTERNS = (
    ('procedure', r'(?P<subject>.+?)(?:应该|应当|需要)?(?:怎么|如何|怎样)(?:处理|操作|办理)'),
    ('procedure', r'(?:应该|应当)?(?:怎么|如何|怎样)(?:处理|操作|办理)(?P<subject>.+)'),
    ('procedure', r'how (?:should|do) (?:i|we) (?:handle|process) (?P<subject>.+)'),
    ('procedure', r'how to (?:handle|process) (?P<subject>.+)'),
    ('fact', r'(?P<subject>.+?)的(?P<attribute>地址|联系人|联系方式)是什么'),
    ('fact', r'what is the (?P<attribute>address|contact) of (?P<subject>.+)'),
)


def normalized(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def label(value: object) -> str:
    if not isinstance(value, str) or len(value) > 160:
        raise ValueError('query_label_invalid')
    result = normalized(value)
    if not _LABEL.fullmatch(result) or _UNSAFE.search(result):
        raise ValueError('query_label_invalid')
    return result


@dataclass(frozen=True)
class QuerySlot:
    intent: str
    subject: str
    attribute: str = ''


def parse_query(query: object) -> QuerySlot | None:
    if not isinstance(query, str) or not 1 <= len(query) <= 256:
        return None
    # A newline may introduce a second instruction; never erase it by folding.
    if any(c in query for c in '\n\r\x00;；。！!'):
        return None
    text = normalized(query)
    if text.endswith('?'):
        text = text[:-1].rstrip()
    if text.startswith('请问'):
        text = text[2:].lstrip()
    if _UNSAFE.search(text) or '?' in text or re.search(r'\d+\.\d+', text):
        return None
    slots = set()
    for intent, pattern in _PATTERNS:
        match = re.fullmatch(pattern, text, re.I)
        if match:
            try:
                subject = label(match['subject'])
                attribute = (match.groupdict().get('attribute') or '')
                attribute = {'address': '地址', 'contact': '联系人'}.get(attribute, attribute)
                # Do not let a second question/instruction be swallowed as a name.
                if re.search(r'怎么|如何|怎样|是什么|\b(?:how|what)\b', subject):
                    return None
                if subject in {'这个','那个','这些','那些','它','他们','it','this','that','these','those','them'}:
                    return None
                slots.add(QuerySlot(intent, subject, attribute))
            except ValueError:
                return None
    return next(iter(slots)) if len(slots) == 1 else None
