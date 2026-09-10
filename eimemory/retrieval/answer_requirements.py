"""Conservative answer-shape checks, not query-specific aliases or gold IDs.

An entity match does not answer every question about that entity. Apply only
unambiguous requested attributes; unknown question shapes remain semantic.
"""
import re
import unicodedata
from eimemory.recall.task_queries import task_recall_mode, supports_task_evidence

_MONEY_QUESTION = re.compile(
    r'多少钱|(?:金额|总金额|价格|单价|总价|费用|花费|售价|造价|成本|报价).{0,8}(?:多少|几元)|'
    r'how\s+much\b.{0,100}\b(?:cost|pay|paid|spend|spent|price)\b|'
    r'\b(?:price|cost)\s+(?:of|for)\b', re.I)
_NUMBER = r'(?:\d+(?:[,.]\d+)*|[零〇一二两三四五六七八九十百千万亿]+)'
_MONEY_FACT = re.compile(
    rf'(?:[¥￥$€£]|USD\s*|CNY\s*|RMB\s*|EUR\s*){_NUMBER}|'
    rf'{_NUMBER}\s*(?:万|亿)?\s*(?:元|块钱|美元|美金|欧元|英镑|日元|港币|人民币|USD|CNY|RMB|EUR|dollars?)\b|'
    rf'{_NUMBER}\s*(?:万|亿)?\s*(?:元|块钱|美元|美金|欧元|英镑|日元|港币|人民币)|'
    rf'(?:金额|总金额|价格|单价|总价|费用|花费|售价|造价|成本|报价|支付|购入价)\s*(?:为|是|约|大约|大概|[:：=])?\s*{_NUMBER}|'
    r'免费|无需付费|零费用|\bfree\s+of\s+charge\b', re.I)


# Structured identifiers are constraints, unlike ordinary semantic query terms.
_VERSION = re.compile(r'(?<![\w.])v?(\d+\.\d+(?:\.\d+)+(?:-[\w.-]+)?)(?![\w.])', re.I)
_PROJECT = re.compile(r'(?:项目|project)\s+([\w-]+)|([\w-]+?)项目', re.I)
_MODEL_QUESTION = re.compile(r'型号|哪款|什么款|\b(?:model|which device)\b', re.I)
_OBJECTS = (('phone', r'手机|电话|智能机|\b(?:phone|handset|smartphone)\b'),
            ('computer', r'电脑|笔记本|\b(?:laptop|computer)\b'),
            ('router', r'路由器|\brouter\b'))


def requested_attribute(query: str) -> str:
    query = str(query or '')[:16000]
    if _MONEY_QUESTION.search(query):
        return 'money'
    if _VERSION.search(query) and re.search(r'部署|验收|\bdeploy', query, re.I):
        return 'release_status'
    if _MODEL_QUESTION.search(query):
        for name, pattern in _OBJECTS:
            if re.search(pattern, query, re.I):
                return 'model_' + name
    mode = task_recall_mode(query)
    return f'task_{mode}' if mode else ''


def supports_requested_attribute(attribute: str, evidence: str) -> bool:
    if attribute == 'release_status':
        return (bool(re.search(r'已部署|部署已完成|部署.{0,8}(?:成功|通过)|生产为|发布.{0,8}(?:完成|成功)|\bdeployed\b', evidence, re.I))
                and supports_task_evidence('status', evidence))
    if attribute in {'task_status', 'task_history'}:
        return supports_task_evidence(attribute.removeprefix('task_'), evidence)
    if attribute.startswith('model_'):
        pattern = dict(_OBJECTS).get(attribute[6:], r'(?!)')
        return any(re.search(pattern, sentence, re.I)
                   and re.search(r'(?:型号|\bmodel)(?:\s*(?:为|是|is|[:：]))?\s*[A-Za-z0-9\u4e00-\u9fff][\w -]+|(?:手机|电话|电脑|笔记本|路由器)(?:为|是|[:：])[^，。？?]+|\b(?:phone|handset|laptop|computer|router)\s+is\s+[A-Z][\w-]+(?:\s+[A-Z0-9][\w-]+)+', sentence)
                   and not re.search(r'未知|未记录|不清楚|不知道|什么|哪款|是坏的|没电|丢失|\b(?:unknown|not recorded|broken|lost)\b|[？?]', sentence)
                   for sentence in re.split(r'[。\n]', evidence))
    return not attribute or (attribute == 'money' and bool(_MONEY_FACT.search(evidence)))



def explicit_project(query: str) -> str:
    match = _PROJECT.search(query)
    if match:
        value = next(v for v in match.groups() if v)
        if value not in {'这个', '当前', '所有', '全部'}:
            return value
    # Named software followed by a requested release is also an explicit entity.
    match = re.search(r'([A-Za-z][\w-]*)\s+v?\d+\.\d+', query)
    return match.group(1) if match else ''


def supports_query_identity(query: str, evidence: str, aliases=()) -> bool:
    text = unicodedata.normalize('NFKC', evidence).casefold()
    if any(version.casefold() not in {v.casefold() for v in _VERSION.findall(text)}
           for version in _VERSION.findall(query)):
        return False
    project = explicit_project(query).casefold()
    if project:
        # Explicit record aliases are authority-owned, never inferred from similarity.
        if project not in {str(a).casefold() for a in aliases}:
            pattern = (r'(?<![A-Za-z0-9_-])' + re.escape(project) + r'(?![A-Za-z0-9_-])'
                       if project.isascii() else re.escape(project))
            if not re.search(pattern, text):
                return False
    return True


# Bind each amount to its own monetary role, rather than any nearby number.
_MONEY_ROLES = {
    'contract_total': r'(?:合同|合约)(?:总金额|金额|总价|价款)|合同价值',
    'prepayment': r'预付款|首付款|定金',
    'budget': r'预算',
    'installment': r'分期付款|本期款|尾款',
    'unit_price': r'单价',
}
_CURRENCY_AMOUNT = rf'(?:(?:人民币|USD|CNY|RMB|EUR|[¥￥$€£])\s*{_NUMBER}(?:万|亿)?(?:元)?|{_NUMBER}\s*(?:万|亿)?\s*(?:元|美元|人民币|USD|CNY|RMB|EUR))'


def _supports_money_role(query: str, evidence: str, aliases=()) -> bool:
    role = next((name for name, pattern in _MONEY_ROLES.items()
                 if re.search(pattern, query, re.I)), '')
    if not role:
        return supports_requested_attribute('money', evidence)
    assertion = re.compile(_MONEY_ROLES[role] + r'\s*(?:为|是|约为|约|共计|合计|[:：=])?\s*'
                           + _CURRENCY_AMOUNT, re.I)
    previous = ''
    for sentence in re.split(r'[。\n]', evidence):
        sentence = sentence.strip()
        identity = supports_query_identity(query, sentence, aliases)
        # Explicit anaphora may inherit the immediately preceding contract's
        # project. Do not transfer identity to another project's amount.
        if (not identity and re.match(r'^(?:该|此|这份)合同', sentence)
                and '合同' in previous and supports_query_identity(query, previous, aliases)):
            identity = True
        if identity:
            for clause in re.split(r'[，,；;]', sentence):
                if (assertion.search(clause)
                        and not re.search(r'未知|未确定|未定|不详|不是|并非|不为|尚未|[？?]', clause)):
                    return True
        previous = sentence
    return False


def supports_answer_requirements(query: str, evidence: str, aliases=()) -> bool:
    attribute = requested_attribute(query)
    if not supports_query_identity(query, evidence, aliases):
        return False
    if attribute == 'release_status':
        return any(supports_query_identity(query, sentence, aliases)
                   and supports_requested_attribute(attribute, sentence)
                   for sentence in re.split(r'[。\n]', evidence))
    subject = ''
    if attribute.startswith('model_'):
        match = re.match(r'^(?:请问)?(.+?)(?:(?:现在|目前)?(?:使用|用)(?:的|的是)?|的)(?:哪款|什么款)?(?:手机|电话|电脑|笔记本|路由器)', query)
        if match and match.group(1) not in {'我', '用户', '自己', '这台', '这个'}:
            subject = match.group(1)
    if subject and subject.casefold() not in evidence.casefold() and subject.casefold() not in aliases:
        return False
    if attribute == 'money' and any(re.search(p, query, re.I) for p in _MONEY_ROLES.values()):
        return _supports_money_role(query, evidence, aliases)
    if attribute == 'money' and explicit_project(query):
        # A number belonging to a second project cannot fill the first one's
        # missing property. Require identity and amount in the same sentence.
        return any(supports_query_identity(query, sentence, aliases)
                   and supports_requested_attribute(attribute, sentence)
                   for sentence in re.split(r'[。\n]', evidence))
    return supports_requested_attribute(attribute, evidence)
