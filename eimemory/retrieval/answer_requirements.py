"""Conservative answer-shape checks, not query-specific aliases or gold IDs.

An entity match does not answer every question about that entity. Apply only
unambiguous requested attributes; unknown question shapes remain semantic.
"""
import re

_MONEY_QUESTION = re.compile(
    r'多少钱|(?:价格|单价|总价|费用|花费|售价|造价|成本|报价).{0,8}(?:多少|几元)|'
    r'how\s+much\b.{0,100}\b(?:cost|pay|paid|spend|spent|price)\b|'
    r'\b(?:price|cost)\s+(?:of|for)\b', re.I)
_NUMBER = r'(?:\d+(?:[,.]\d+)*|[零〇一二两三四五六七八九十百千万亿]+)'
_MONEY_FACT = re.compile(
    rf'(?:[¥￥$€£]|USD\s*|CNY\s*|RMB\s*|EUR\s*){_NUMBER}|'
    rf'{_NUMBER}\s*(?:万|亿)?\s*(?:元|块钱|美元|美金|欧元|英镑|日元|港币|人民币|USD|CNY|RMB|EUR|dollars?)\b|'
    rf'{_NUMBER}\s*(?:万|亿)?\s*(?:元|块钱|美元|美金|欧元|英镑|日元|港币|人民币)|'
    rf'(?:价格|单价|总价|费用|花费|售价|造价|成本|报价|支付|购入价)\s*(?:为|是|约|大约|大概|[:：=])?\s*{_NUMBER}|'
    r'免费|无需付费|零费用|\bfree\s+of\s+charge\b', re.I)


def requested_attribute(query: str) -> str:
    return 'money' if _MONEY_QUESTION.search(str(query)[:16000]) else ''


def supports_requested_attribute(attribute: str, evidence: str) -> bool:
    return not attribute or (attribute == 'money' and bool(_MONEY_FACT.search(evidence)))
