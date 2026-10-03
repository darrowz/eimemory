"""Conservative answer-shape checks, not query-specific aliases or gold IDs.

An entity match does not answer every question about that entity. Apply only
unambiguous requested attributes; unknown question shapes remain semantic.
"""
import re
import unicodedata
from eimemory.recall.task_queries import (task_recall_mode, supports_task_evidence,
                                          _STATE_FACT, _HISTORY_FACT)

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
_VERSION = re.compile(r'(?<![A-Za-z0-9_.])v?(\d+\.\d+(?:\.\d+)+(?:-[A-Za-z0-9_.-]+)?)(?![A-Za-z0-9_.])', re.I)
_PROJECT = re.compile(r'项目\s*([A-Za-z][A-Za-z0-9_-]*)|(?:项目|project)\s+([\w-]+)|([\w-]+?)项目', re.I)
_MODEL_QUESTION = re.compile(r'型号|哪款|什么款|\b(?:model|which device)\b', re.I)
_OBJECTS = (('phone', r'手机|电话|智能机|\b(?:phone|handset|smartphone)\b'),
            ('computer', r'电脑|笔记本|\b(?:laptop|computer)\b'),
            ('router', r'路由器|\brouter\b'))


_SECRET_QUESTION = re.compile(
    r'口令|密码|通行证|凭证|密钥|令牌|'
    r'\b(?:password|passwd|secret|credential|api[_\s-]?key|access[_\s-]?token)\b',
    re.I)
# Require an asserted secret value — procedure text about the same entity is not enough.
_SECRET_FACT = re.compile(
    r'(?:口令|密码|通行证|凭证|密钥|令牌|password|passwd|secret|credential|token|api[_\s-]?key)\s*'
    r'(?:为|是|is|[:：=])\s*\S+',
    re.I)


# The knowledge layer already distinguishes constraint units from facts/persona.
# Recognize the requested property, independent of project names or releases.
_CONSTRAINT_FACT = re.compile(
    r'(?:必须|不得|禁止|需要|须)(?!什么|哪些|吗)\S+|'
    r'(?:要求|规则|约束|标准|条件)\s*[:：]\s*\S+|'
    r'\b(?:must|shall|required|requires?)\s+\S+', re.I)

# Mentioning a standard, production rule or conditional model is not a request
# for constraints. Unlike task-intent suppression, admission needs a question
# about the property (or a noun-phrase lookup ending in that property).
_CONSTRAINT_REQUEST = re.compile(
    r'(?:什么|哪些|何种)[^。！？!?\n，,；;]{0,16}(?:要求|规则|约束|标准|条件)|'
    r'(?:要求|规则|约束|标准|条件)\s*(?:是什么|有哪些|是哪些|有哪|如何|怎么|怎样|吗|[？?]|$)|'
    r'\b(?:what|which|list|show)\b[^.!?\n]{0,64}\b(?:requirements?|rules?|constraints?|criteria|conditions?)\b|'
    r'\b(?:requirements?|rules?|constraints?|criteria|conditions?)\s*\??$', re.I)


def requested_attribute(query: str) -> str:
    query = str(query or '')[:16000]
    if _MONEY_QUESTION.search(query):
        return 'money'
    if _SECRET_QUESTION.search(query):
        return 'secret'
    if _CONSTRAINT_REQUEST.search(query.strip()):
        return 'constraint'
    if _VERSION.search(query) and re.search(r'部署|验收|\bdeploy', query, re.I):
        return 'release_status'
    if _MODEL_QUESTION.search(query):
        for name, pattern in _OBJECTS:
            if re.search(pattern, query, re.I):
                return 'model_' + name
    mode = task_recall_mode(query)
    return f'task_{mode}' if mode else ''


def supports_requested_attribute(attribute: str, evidence: str) -> bool:
    if attribute == 'constraint':
        return bool(_CONSTRAINT_FACT.search(evidence)) and not re.search(r'[？?]|未知|未记录|未确定', evidence)
    if attribute == 'secret':
        return bool(_SECRET_FACT.search(evidence))
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
    # A suffix match starting inside a release token is not a project name
    # (e.g. the "29的" in "Alpha v1.14.29的项目"). Use the named-release
    # fallback below instead of letting one identifier consume another.
    if match and not any(v.start() <= match.start() < v.end() for v in _VERSION.finditer(query)):
        value = next(v for v in match.groups() if v)
        # In coordinated generic nouns (项目预算与项目进度), the lazy
        # suffix matcher spans from the first 项目 to the second. That span is
        # grammar, not an explicit project identity; leave it to semantic review.
        coordinated_generic = bool(re.fullmatch(r'项目.+(?:和|与|及|以及)', value))
        if value not in {'这个', '当前', '所有', '全部'} and not coordinated_generic:
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


def _supports_bound_attribute(query: str, evidence: str, attribute: str, aliases) -> bool:
    project = explicit_project(query).casefold()
    previous = ''
    # ponytail: only a standalone heading and its next bounded line inherit
    # identity; general discourse/coreference needs grounded source structure.
    parts = re.split(r'([。!?！？\n])', evidence[:4096])
    for index, part in enumerate(parts):
        if part == '\n':
            continue
        sentence = part.strip()
        projects = {next(v for v in m.groups() if v).casefold()
                    for m in _PROJECT.finditer(sentence)}
        conflicting = bool(project and projects - {project})
        question = attribute == 'constraint' and index + 1 < len(parts) and parts[index + 1] in '?!？！'
        if not conflicting and not question and supports_requested_attribute(attribute, sentence):
            if supports_query_identity(query, sentence, aliases):
                return True
            heading = re.sub(r'^#{1,6}\s+', '', previous).rstrip(':：').strip()
            heading_project = _VERSION.sub('', heading).strip()
            versions = {v.casefold() for v in _VERSION.findall(heading)}
            # Full-match syntax prevents notifications or other prose becoming
            # an implicit identity grant. Versions belong to this heading only;
            # an explicit different version in the assertion overrides it.
            if (len(previous) + len(sentence) <= 512
                    and (_PROJECT.fullmatch(heading_project)
                         or versions and heading_project.casefold() == project)
                    and supports_query_identity(query, heading)
                    and not {v.casefold() for v in _VERSION.findall(sentence)} - versions
                    and not projects
                    and (_STATE_FACT.match(sentence)
                         or attribute == 'task_history' and _HISTORY_FACT.match(sentence)
                         or attribute == 'constraint' and _CONSTRAINT_FACT.match(sentence))):
                return True
        previous = sentence
    return False


def supports_answer_requirements(query: str, evidence: str, aliases=()) -> bool:
    attribute = requested_attribute(query)
    if not supports_query_identity(query, evidence, aliases):
        return False
    if attribute in {'task_status', 'task_history'} and not supports_requested_attribute(attribute, evidence):
        return False
    if attribute in {'release_status', 'task_status', 'task_history', 'constraint'}:
        return _supports_bound_attribute(query, evidence, attribute, aliases)
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
