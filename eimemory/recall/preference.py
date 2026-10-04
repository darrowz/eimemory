"""Bounded personal-preference relevance, never scope or source permission.

Only the request selects a mode. Candidate tags, scores and caller-provided
"evidence" cannot assert support: the predicate reads the authoritative body.
Unknown topics retain the ordinary recall path.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping

from eimemory.metadata import business_metadata

PREFERENCE_MEMORY_TYPES = ('instruction', 'preference', 'operator_preference', 'user_preference', 'persona')
RESPONSE_TOPIC_TERMS = ('回答', '回复', '答复', '回应', '沟通', 'reply', 'replies', 'respond', 'response', 'answer', 'communication')
_CONTEXT_FIELDS = ('intent', 'task_intent', 'task_type', 'query_type')
_CONTEXT_ALIASES = frozenset({'operator.preference', 'operator_preference'})
# Query and assertion grammars are deliberately closed. A response noun plus
# an arbitrary neighboring property is not a supported preference request.
_RESPONSE_WORDS = r'(?:回答|回复|答复|回应|沟通)'
_EN_RESPONSE = r'(?:repl(?:y|ies)|responses?|answers?|communication)'
_ZH_PROPERTY = {'language': r'(?:语言|使用语言)', 'length': r'(?:长度|长短|字数|篇幅)',
                'format': r'(?:格式|排版)', 'tone': r'语气'}
_EN_PROPERTY = {'language': r'language', 'length': r'(?:length|verbosity)',
                'format': r'format', 'tone': r'tone'}
_QUERY_PREFIX = r'(?:请问|请|告诉我)?'
_QUERY_END = r'(?:是(?:什么|怎样)|有(?:哪些|什么)|应该是什么)?'
_GENERAL_ZH = re.compile(
    _QUERY_PREFIX + r'我的' + _RESPONSE_WORDS + r'(?:的)?(?:方式|风格|风格偏好|偏好)' + _QUERY_END + r'$')
_HOW_ZH = re.compile(
    _QUERY_PREFIX + r'(?:以后|今后|之后)?(?:应该|应当|该)?(?:怎么|怎样|如何)' + _RESPONSE_WORDS + r'(?:我)?$')
_GENERAL_EN = re.compile(
    r'(?:what (?:is|are)|describe|tell me)?\s*my\s+(?:preferred\s+)?' + _EN_RESPONSE
    + r'\s+(?:style|preferences?)$', re.I)
_HOW_EN = re.compile(r'how\s+(?:should|do)\s+you\s+(?:reply|respond|answer)(?:\s+to\s+me)?$', re.I)
_ALL_PREFERENCES = re.compile(
    r'^(?:请)?(?:回忆|列出|总结|告诉我|还记得|记得)?(?:一下)?我的(?:个人)?(?:偏好|喜好|习惯)(?:有(?:什么|哪些)|是(?:什么|哪些)|有哪些|是什么|吗)?$|'
    r'^我(?:有什么|有哪些|有何)(?:个人)?(?:偏好|喜好|习惯)$|'
    r'^(?:what are|list|recall|remember|tell me|summarize)?\s*my\s+(?:personal\s+)?preferences$', re.I)

# Finite, same-clause assertion parsing. Do not inherit an actor/assertion from
# a previous sentence, coordinated clause, quotation, or counterfactual.
# Quotes/fences are checked before sentence splitting. Fail closed on the
# whole candidate body set; do not recover a quoted middle sentence as an
# owner assertion. Apostrophes inside words (don't) are not quote delimiters.
_QUOTED_BODY = re.compile(r'''["“”「」『』`]|(?<!\w)['‘’]|['‘’](?!\w)''')
_SENTENCES = re.compile(r'[^。\n.!?！？;；]+[!?！？]?')
_CLAUSES = re.compile(r'[,，]|\b(?:and|but|while|whereas)\b|但是|而且|并且|以及', re.I)
_NONASSERTED = re.compile(
    r'[?？"“”「」『』`]|未知|不知道|不清楚|未记录|未确定|不确定|没有记录|从未|未曾|不曾|并未|并非|不一定|'
    r'假如|如果|假设|据说|听说|是否|为什么|什么|怎么样|'
    r'\b(?:if|unless|suppose|assuming|hypothetically|might|would|could|unknown|unrecorded|whether|'
    r'not\s+(?:known|recorded|sure)|never\s+(?:asked|requested|required)|did\s+not\s+(?:ask|request|require))\b|'
    r"(?:^|\s)'[^']+'", re.I)
_ZH_OWNER = re.compile(r'^(?:用户(?:（[^（）]{1,24}）)?|我|本人)(?:的)?')
_ZH_WANT = re.compile(r'^(?:要求|希望|偏好|不喜欢|不希望|喜欢|讨厌)')
_EN_OWNER = re.compile(r'^(?:(?:the\s+)?user|I)\s+', re.I)
_EN_WANT = re.compile(r"^(?:prefers?|wants?|requests?|requires?|likes?|dislikes?|do\s+not\s+(?:like|want)|don't\s+(?:like|want))\s+", re.I)
_ZH_IMPERATIVE = re.compile(r'^(?:以后|今后|默认|请|不要|避免|必须|应当|回答|回复|答复|回应|沟通|先)')
_EN_IMPERATIVE = re.compile(r'^(?:please|always|never|avoid|use|keep|reply|respond|answer|communicate|do\s+not)\b', re.I)

# One lexicon supplies both value recognition and syntactic roles. A known
# word is never evidence by itself: a whole production must bind it to the
# response head. Roles keep "concisely replies" and "reply concise" outside
# the supported grammar while accepting "concise replies" / "reply concisely".
_ZH_VALUES = {
    'language': {'language': ('西班牙语','葡萄牙语','普通话','中文','英文','英语','汉语','法语','日语','德语')},
    'length': {'adjective': ('简短','简洁','极简','详细','精炼','长篇','啰嗦'),
               'action': ('少解释','不要废话')},
    'format': {'format': ('分点列表','列表','分点','要点','段落','表格','Markdown')},
    'tone': {'adjective': ('礼貌','正式','口语','直接','温和')},
    'structure': {'structure': ('结论','解释')},
}
_EN_VALUES = {
    'language': {'language': ('english','chinese','spanish','french','japanese','german','portuguese')},
    'length': {'adjective': ('concise','brief','short','shorter','detailed','succinct','long','lengthy','verbose'),
               'adverb': ('concisely','briefly')},
    'format': {'format': ('bullet points','bullet point','bullets','bullet','lists','list','paragraphs','paragraph','tables','table','markdown')},
    'tone': {'adjective': ('polite','formal','informal','direct','gentle'),
             'adverb': ('politely','formally','directly','gently')},
    'structure': {'structure': ('conclusion','conclusions','explanation','explanations')},
}


@dataclass(frozen=True)
class PreferenceRecallRequest:
    mode: str
    reason: str
    attribute: str = ''


def canonical_preference_context(value: object) -> str:
    normalized = str(value or '').strip().lower()
    return 'operator_preference' if normalized in _CONTEXT_ALIASES else normalized


def preference_recall_request(query: str, context: Mapping | None = None) -> PreferenceRecallRequest | None:
    text = ' '.join(str(query or '').split()).strip().rstrip('？?。.!！').strip()
    if not text or len(text) > 2048:
        return None
    explicit = any(str((context or {}).get(key) or '').strip().lower() in _CONTEXT_ALIASES
                   for key in _CONTEXT_FIELDS)
    if _ALL_PREFERENCES.fullmatch(text):
        return PreferenceRecallRequest('list_preferences', 'personal_preference_list')
    for attribute in _ZH_PROPERTY:
        zh = (_QUERY_PREFIX + r'我的' + _RESPONSE_WORDS + r'(?:使用的|的)?'
              + _ZH_PROPERTY[attribute] + r'(?:偏好)?' + _QUERY_END)
        en = (r'(?:what (?:is|are)|describe|tell me)?\s*my\s+(?:preferred\s+)?'
              + _EN_RESPONSE + r'\s+' + _EN_PROPERTY[attribute])
        if re.fullmatch(zh, text) or re.fullmatch(en, text, re.I):
            return PreferenceRecallRequest('response_style', 'personal_response_property', attribute)
    if (_GENERAL_ZH.fullmatch(text) or _GENERAL_EN.fullmatch(text)
            or (_HOW_ZH.fullmatch(text) and (explicit or text.endswith('我')))
            or (_HOW_EN.fullmatch(text) and (explicit or text.lower().endswith('to me')))):
        return PreferenceRecallRequest('response_style', 'personal_response_style')
    # Unknown properties (time, recipient, cost, etc.) and extra query objects
    # do not collapse into a broad "style" request, even with explicit context.
    return None


def _assertion_payload(clause: str) -> tuple[str, bool]:
    """Return a bounded predicate complement and whether it has a personal owner."""
    owner = _ZH_OWNER.match(clause)
    if owner:
        rest = clause[owner.end():].lstrip()
        wanted = _ZH_WANT.match(rest)
        if wanted:
            return rest[wanted.end():].strip(), True
        # Chinese zero-copula profile declarations still assert a property.
        if re.match(_RESPONSE_WORDS + r'(?:风格|方式|语言|长度|语气|格式)', rest):
            return rest, True
        return '', False
    owner = _EN_OWNER.match(clause)
    if owner:
        rest = clause[owner.end():]
        wanted = _EN_WANT.match(rest)
        return (rest[wanted.end():].strip(), True) if wanted else ('', False)
    if re.fullmatch(_EN_RESPONSE + r' (?:should|must) be .+', clause, re.I):
        return clause, False
    if _ZH_IMPERATIVE.match(clause) or _EN_IMPERATIVE.match(clause):
        return clause, False
    return '', False


def _bound_values(text: str, vocabulary: dict, roles: set[str], *,
                  chinese: bool = False, attribute: str = '', maximum: int = 4) -> set[str]:
    """Consume a complete, bounded modifier/value slot from the single lexicon."""
    terms = {term.casefold(): attr for attr, forms in vocabulary.items()
             if not attribute or attr == attribute
             for role, values in forms.items() if role in roles for term in values}
    remaining = text.casefold().strip()
    properties = set()
    for _ in range(maximum):
        matched = next((term for term in sorted(terms, key=len, reverse=True)
                        if remaining.startswith(term) and
                        (chinese or len(remaining) == len(term) or remaining[len(term)].isspace())), None)
        if matched is None:
            return set()
        properties.add(terms[matched])
        remaining = remaining[len(matched):].strip()
        if not remaining:
            return properties
    return set()


def _en_prepositional_property(text: str) -> set[str]:
    # Fullmatch makes the preposition's object the property value itself;
    # "in a French cafe" and "before tables" cannot lend their words.
    match = re.fullmatch(r'(in|using|with|as) (.+)', text)
    if not match:
        return set()
    relation, value = match.groups()
    if relation in {'in', 'using'}:
        language = _bound_values(value, _EN_VALUES, {'language'}, maximum=1)
        if language:
            return language
    value = re.sub(r'^(?:a|an) ', '', value)
    return _bound_values(value, _EN_VALUES, {'format'}, maximum=1)


def _en_response_properties(value: str) -> set[str]:
    value = re.sub(r'^(?:(?:please|always|never|do not) ){1,3}', '', value)
    value = re.sub(r'^you to ', '', value)
    # Imperative keep/use/avoid have their own complements; they do not
    # license arbitrary words around a response noun.
    keep = re.fullmatch(r'keep (?:the )?' + _EN_RESPONSE + r' (.+)', value)
    if keep:
        return _bound_values(keep[1], _EN_VALUES, {'adjective'})
    value = re.sub(r'^(?:use|avoid) ', '', value)

    # A noun phrase can have directly attached modifiers, one response
    # predicate, or one typed prepositional complement. No cross-object scan.
    nominal = re.fullmatch(r'(?:(?:a|an|the) )?(?:(.+?) )?(' + _EN_RESPONSE + r')(?:( .+))?', value)
    if nominal:
        modifier, _, tail = nominal.groups()
        properties = _bound_values(modifier, _EN_VALUES, {'adjective', 'language'}) if modifier else set()
        if modifier and not properties:
            return set()
        tail = (tail or '').strip()
        if not tail:
            return properties
        related = _en_prepositional_property(tail)
        if not related:
            copula = re.fullmatch(r'(?:(?:should|must|to) )?(?:be|is|are) (.+)', tail)
            if copula:
                related = (_bound_values(copula[1], _EN_VALUES, {'adjective', 'language'})
                           or _en_prepositional_property(copula[1]))
        if not related:
            for attribute, label in _EN_PROPERTY.items():
                predicate = re.fullmatch(label + r' (?:(?:should|must|to) )?(?:be|is|are) (.+)', tail)
                if predicate:
                    related = _bound_values(predicate[1], _EN_VALUES,
                        {'adjective', 'language', 'format'}, attribute=attribute, maximum=1)
                    break
        if related:
            return properties | related
        # Noun/verb ambiguous forms (reply/answer) can still use the verb
        # production below, but never preserve an unmatched modifier/tail.

    action = re.fullmatch(r'(?:reply|respond|answer|communicate) (.+)', value)
    if action:
        tail = action[1]
        related = (_bound_values(tail, _EN_VALUES, {'adverb'})
                   or _en_prepositional_property(tail))
        if related:
            return related
        manner = re.fullmatch(r'(.+?) ((?:in|using|with|as) .+)', tail)
        if manner:
            adverbs = _bound_values(manner[1], _EN_VALUES, {'adverb'})
            related = _en_prepositional_property(manner[2])
            if adverbs and related:
                return adverbs | related
        structure = re.fullmatch(r'with (.+) first', tail)
        if structure:
            return _bound_values(structure[1], _EN_VALUES, {'structure'}, maximum=1)
    return set()


def _zh_response_properties(value: str) -> set[str]:
    value = re.sub(r'^(?:AI|助手|你)', '', value, flags=re.I)
    value = re.sub(r'^(?:(?:以后|今后|默认|每次|总是|请|不要|避免|必须|应当|应该|优先)){1,4}', '', value)
    # Values before the response directly modify that head. A use relation
    # has a separate slot for its language/format object.
    modifier = re.fullmatch(r'(.+?)(?:的)?' + _RESPONSE_WORDS, value)
    if modifier:
        related = _bound_values(modifier[1], _ZH_VALUES, {'adjective', 'language'}, chinese=True)
        if related:
            return related
    use = re.fullmatch(r'(?:用|使用|采用|以)(.+?)' + _RESPONSE_WORDS, value)
    if use:
        return _bound_values(use[1], _ZH_VALUES, {'language', 'format'}, chinese=True, maximum=1)
    head = re.fullmatch(_RESPONSE_WORDS + r'(.+)', value)
    if not head:
        return set()
    tail = head[1]
    # Zero-copula / modal response predicate: 回复简短 / 回复应该简短.
    predicate = re.sub(r'^(?:应该|应当|必须|要|是|为)', '', tail)
    related = _bound_values(predicate, _ZH_VALUES, {'adjective', 'action'}, chinese=True)
    if related:
        return related
    use = re.fullmatch(r'(?:用|使用|采用|以)(.+)', tail)
    if use:
        return _bound_values(use[1], _ZH_VALUES, {'language', 'format'}, chinese=True, maximum=1)
    for attribute, label in _ZH_PROPERTY.items():
        predicate = re.fullmatch(r'(?:的)?' + label + r'(?:应该|应当|必须)?(?:是|为)?(.+)', tail)
        if predicate:
            return _bound_values(predicate[1], _ZH_VALUES, {'adjective', 'language', 'format'},
                                 chinese=True, attribute=attribute, maximum=1)
    style = re.fullmatch(r'(?:的)?(?:风格|方式)(?:是|为)?(.+)', tail)
    if style:
        return _bound_values(style[1], _ZH_VALUES, {'adjective'}, chinese=True)
    # A prohibition on long response explanations is a length instruction,
    # not permission to borrow adjectives from arbitrary other objects.
    negative = re.fullmatch(r'(?:不要|避免)(.+?)(?:解释)?', tail)
    if negative:
        return _bound_values(negative[1], _ZH_VALUES, {'adjective'},
                             chinese=True, attribute='length', maximum=1)
    structure = re.fullmatch(r'(?:先给|先说|先)(.+)', tail)
    if structure:
        return _bound_values(structure[1], _ZH_VALUES, {'structure'}, chinese=True, maximum=1)
    return set()


def _response_properties(payload: str) -> set[str]:
    """Return properties bound by a complete finite response production.

    Every attribute uses the same contract: its value directly modifies the
    response head or fills that head's explicit property/predicate slot.
    Known-word co-occurrence, order markers and other complements give no
    typed support. Complex unparsed text retains ordinary recall semantics.
    """
    value = payload.strip()
    if not value or len(value) > 512:
        return set()
    if re.search(r'[\u4e00-\u9fff]', value):
        return _zh_response_properties(re.sub(r'\s+', '', value))
    return _en_response_properties(' '.join(value.casefold().split()))


def _body_supports(request: PreferenceRecallRequest, body: str, *, memory_type: str) -> bool:
    for sentence in _SENTENCES.findall(body[:4096]):
        if _NONASSERTED.search(sentence):
            continue
        for clause in _CLAUSES.split(sentence):
            clause = clause.strip()
            if not clause or _NONASSERTED.search(clause):
                continue
            payload, personal_owner = _assertion_payload(clause)
            if not payload:
                break
            properties = _response_properties(payload)
            if request.mode == 'response_style':
                # A sentence is a consecutive chain of independently bound
                # response assertions. Unknown/reporting clauses are barriers:
                # a later 'and please ...' cannot reset a third party's voice.
                if not properties:
                    break
                if not request.attribute or request.attribute in properties:
                    return True
            elif memory_type != 'persona' and personal_owner:
                # General preference lists can contain e.g. food preferences;
                # they still need this clause's explicit personal assertion.
                if re.search(r'[A-Za-z0-9\u4e00-\u9fff]', payload):
                    return True
            elif properties:
                return True
            else:
                break
    return False


def supports_preference_request(request: PreferenceRecallRequest | None, record) -> bool:
    """Typed relevance only; the engine still must enforce authority and gates."""
    if request is None or request.mode not in {'response_style', 'list_preferences'}:
        return False
    if getattr(record, 'kind', None) != 'memory' or getattr(record, 'status', None) != 'active':
        return False
    content = record.content if isinstance(record.content, dict) else {}
    meta = business_metadata(record.meta)
    memory_type = str(meta.get('memory_type') or content.get('memory_type') or '').strip().lower()
    if memory_type not in PREFERENCE_MEMORY_TYPES:
        return False
    layer = str(meta.get('memory_layer') or content.get('memory_layer') or '').strip().lower()
    if layer and layer not in {'l1', 'l3'}:
        return False
    quality = meta.get('quality')
    if isinstance(quality, dict) and quality.get('capture_decision') == 'reject':
        return False
    if str(meta.get('projection_type') or content.get('projection_type') or '').lower() == 'operational_knowledge':
        return False
    # Titles/tags/serialized metadata cannot manufacture an asserted property.
    bodies = [value for value in (content.get('text'), record.detail, record.summary)
              if isinstance(value, str) and value.strip()]
    if any(_QUOTED_BODY.search(body[:4096]) for body in bodies):
        # Conservative abstention also covers genuine assertions outside a
        # quotation in this body. Stored/returned originals remain untouched;
        # this predicate supplies no model verdict or assertion of absence.
        return False
    return any(_body_supports(request, body, memory_type=memory_type) for body in bodies)
