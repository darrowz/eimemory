"""Pure content matching and local, dated correction decisions for RAW ranking.

Adapted from KRouter (MIT), commit d6b096c4f055f6eebd680240d53418ccd125ee95:
skill/krouter/scripts/recall_index.py: QUERY_STOP, EN_FUNCTION, tokens,
latin_forms, query_content_terms; associate.py: CHANGE_RE, change_lines,
conflicts, apply_conflict. Copyright (c) 2026 KRouter.
Full license: eimemory/raw/KROUTER_NOTICE.txt.

Adaptations: caller-supplied authorized evidence replaces vault/SQL access;
identifier/quote boundaries are retained; no BM25 thresholds or score ratios;
historical cutoffs and ambiguous dates are handled without asserting recency.
No imports from the upstream runtime and no I/O or corpus-wide statistics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
import unicodedata


QUERY_STOP = {
    "什么", "怎么", "怎样", "能不", "不能", "是不", "不是", "哪个", "哪些", "哪里", "应该",
    "可以", "现在", "到底", "一下", "我的", "我们", "你的", "为什", "么要", "有没", "没有",
    "是什", "么样", "要不", "不要", "还是", "这个", "那个", "如何", "多少", "吗我",
    "关于", "帮我", "请问", "一首", "一个",
}
EN_FUNCTION = set("""
a an the and or but if then so of to in on at by for from with about into over under as is are was were be been
being am do does did done doing have has had having can could should would will shall may might must need needs
what which who whom whose when where why how many much long often far old there here this that these those it its
i me my we us our you your he him his she her they them their one ones any some all each every no not more most
less other else than too very just also only ever still again use used using get got make made way ways thing things
please tell know want like
""".split())
FUNCTION_CHARS = set("的了我你他她吗呢吧啊么什")
# Discourse/intent markers cannot themselves establish a shared subject.
SUBJECT_STOP = set("""
user users prefer prefers preference preferences current currently correction corrected update updated
include includes included require requires required provide provides provided latest previous historical
""".split()) | {"更正", "纠正", "修正", "最新", "当前", "之前", "以前", "历史", "当时", "截至", "截止"}
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD = re.compile(r"[\w'-]+", re.UNICODE)
_HISTORY = re.compile(
    r"\b(as\s+of|before|during|histor\w*|previous\w*|earlier|originally|used\s+to|back\s+then|at\s+the\s+time|when\s+(?:did|was|were|had))\b"
    r"|截至|截止|当时|此前|以前|之前|历史|原先|曾经|过去", re.I,
)
_DATE = re.compile(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?(?!\d)")
_YEAR = re.compile(r"(?<![\w'-])(?:19|20)\d{2}(?:[-/]\d+){0,2}(?![\w'-])")
_YEAR_CONTEXT = re.compile(
    r"\b(?:in|on|at|by|for|before|after|during|since|until|through|throughout|from|between|around|circa|year|"
    r"january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(?:the\s+)?$", re.I,
)
_CLAUSE_JOIN = re.compile(r"\b(?:and|but|whereas|while|however|yet|although)\b|但是|然而|并且|而且", re.I)
_CHANGE_LABEL = re.compile(
    r"\s*(?:(?:and|but|whereas|while|however|yet|although)\s+|但是|然而|并且|而且)?"
    r"(?:correction|corrected|更正|纠正|修正)\s*$", re.I,
)
_CURRENT = re.compile(r"\b(?:currently|now\s+prefer|current\s*=\s*true)\b|目前|当前|现在", re.I)
_QUESTION = re.compile(r"(?:是)?(?:什么|谁|哪个|哪些|哪里|几点|多少)")
_NAMED = re.compile(r"(?<![\w'-])[A-Z][A-Za-z0-9_-]{2,}(?![\w'-])")
# A subjectful correction header may introduce a subjectless predicate.
# Arbitrary headers, new named subjects and sentence/line boundaries do not join.
_CHANGE_END = re.compile(r"(?:\b(?:changed|corrected|correction)|更正|纠正|修正)\s*$", re.I)
_CHANGE_PREDICATE = re.compile(r"\s*(?:不再|改为|改成|改用|不是|应为|应该为|"
                               r"(?:they|it)\s+(?:now|no\s+longer)\b|now\b|no\s+longer\b)", re.I)
_CHANGE_RESTATEMENT = re.compile(r"\s*(?:this|that)\s+(?:corrects|corrected|changes|changed)\b", re.I)
# Only discourse, never an entity or fact, may bridge a comma to the next
# assertion. Full matching prevents a correction about another project from
# lending its marker to a later clause containing the queried project.
_CORRECTION_PREAMBLE = re.compile(
    r"\s*(?:(?:correction|corrected|更正|纠正|修正)\s*[:：]?\s*)?"
    r"(?:刚才|刚刚|之前|前面)?(?:(?:登记|记录|记|写|说)错|串档|串号|搞错|说错|弄错)(?:了)?\s*"
    r"|\s*(?:correction|corrected|更正|纠正|修正)\s*[:：]?\s*"
    r"(?:(?:actually|sorry|i\s+was\s+(?:wrong|mistaken))\s*)?", re.I,
)

# Copied/adapted change expressions, including the existing RAW correction forms.
CHANGE_RE = re.compile(
    r"\b(switch(?:ed|ing)?\s+(?:\w+\s+){0,4}?to|switch(?:ed|ing)?\s+from|instead\s+of|chang(?:e|ed|ing)\s+(?:\w+\s+){0,4}?to|"
    r"replac(?:e|ed|es|ing)|mov(?:e|ed|ing)\s+(?:\w+\s+){0,3}?to|migrat(?:e|ed|ing)\s+(?:\w+\s+){0,3}?to|no\s+longer|"
    r"drop(?:ped|ping)?|from\s+now\s+on|rather\s+than|going\s+with|went\s+with|go\s+with|decided\s+against|"
    r"changed\s+(?:my|our)\s+mind|on\s+second\s+thought|changed|corrects|correction|corrected|but\s+now)\b"
    r"|改成|改为|改用|改回|换成|换用|换回|不再用|不用了|弃用|替换|迁到|迁移到|以后都用|以后一律|还是用|决定用|定成"
    r"|更正|纠正|修正|串档|串号|搞错(?:了)?|说错(?:了)?|弄错(?:了)?"
    r"|之前.{0,24}(?:不对|错误|错了)|不是.{0,48}(?:而是|应该是|应为)|(?:应该|应当|应)为", re.I,
)
CONFLICT_DEPTH = 3


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def tokens(text: str) -> set[str]:
    """Upstream NFKC/CJK bigrams, retaining complete mixed identifiers."""
    out: set[str] = set()
    for word in _WORD.findall(normalize(text)):
        start = 0
        for match in _CJK.finditer(word):
            prefix = word[start:match.start()].strip("'_-")
            if len(prefix) >= 2 or prefix.isdecimal():
                out.add(prefix)
            run = match.group()
            out.update(run[i:i + 2] for i in range(len(run) - 1))
            start = match.end()
        suffix = word[start:].strip("'_-")
        if len(suffix) >= 2 or suffix.isdecimal():
            out.add(suffix)
    return out


def _quote_spans(text: str):
    """Yield complete quote interiors in one pass, ignoring word apostrophes."""
    pairs = {'"': '"', "'": "'", '“': '”', '「': '」', '『': '』'}
    start = None
    opener = ""
    for index, char in enumerate(text):
        if char == "\n":
            start = None
        # A word's apostrophe is not a quotation delimiter.
        elif char == "'" and index and text[index - 1].isalnum() and (
            index + 1 < len(text) and text[index + 1].isalnum()
        ):
            continue
        elif start is not None and char == pairs[opener]:
            yield start, index
            start = None
        elif char in pairs and (start is None or char == opener):
            opener, start = char, index + 1


def quoted_phrases(text: str) -> tuple[str, ...]:
    """Extract quotes in one pass, even for arbitrarily many unmatched openers."""
    return tuple(dict.fromkeys(text[start:end].strip() for start, end in _quote_spans(text)
                               if text[start:end].strip()))


def _query_tokens(text: str) -> set[str]:
    # Interrogative copulas are syntax, not part of a Chinese attribute/name.
    # Split before bigram generation so 时间是什么 does not invent 间是.
    # Quoted identities remain literal, including names containing question words.
    parts = []
    end = 0
    for start, stop in _quote_spans(text):
        parts.extend((_QUESTION.sub(" ", text[end:start]), text[start:stop]))
        end = stop
    parts.append(_QUESTION.sub(" ", text[end:]))
    return tokens(" ".join(parts))


def phrase_present(phrase: str, text: str) -> bool:
    """Linear literal search with Unicode word/identifier boundaries."""
    def word_char(char: str) -> bool:
        return bool(char and (char.isalnum() or char == "_"
                              or unicodedata.category(char).startswith("M")))

    if not phrase:
        return False
    # Word boundaries alone accept a shorter name inside a longer quoted
    # identity. Mask nonidentical quote interiors before the literal search;
    # unquoted complete names remain valid evidence.
    visible = list(text)
    for start, end in _quote_spans(text):
        if text[start:end].strip() != phrase:
            visible[start:end] = " " * (end - start)
    text = "".join(visible)
    # KMP avoids rescanning long overlapping matches when every occurrence
    # fails a boundary (for example a quoted name inside a very long word).
    prefixes = [0] * len(phrase)
    size = 0
    for index in range(1, len(phrase)):
        while size and phrase[index] != phrase[size]:
            size = prefixes[size - 1]
        if phrase[index] == phrase[size]:
            size += 1
        prefixes[index] = size
    size = 0
    for index, char in enumerate(text):
        while size and char != phrase[size]:
            size = prefixes[size - 1]
        if char == phrase[size]:
            size += 1
        if size != len(phrase):
            continue
        end = index + 1
        start = end - len(phrase)
        left = text[start - 1] if start else ""
        right = text[end] if end < len(text) else ""
        # Single quotes can delimit a phrase or connect parts of a name.
        left_join = left == "'" and start > 1 and word_char(text[start - 2])
        right_join = right == "'" and end + 1 < len(text) and word_char(text[end + 1])
        if not (word_char(left) or left == "-" or left_join) and not (
            word_char(right) or right == "-" or right_join
        ):
            return True
        size = prefixes[size - 1]
    return False


def date_parts(text: str) -> list[tuple[str, str, str]]:
    """Keep calendar expressions separate from dates embedded in identifiers."""
    def identifier_char(char: str) -> bool:
        return bool(char and not _CJK.fullmatch(char)
                    and (char.isalnum() or char in "_'-"
                         or unicodedata.category(char).startswith("M")))

    return list(dict.fromkeys(
        match.groups() for match in _DATE.finditer(text)
        if not identifier_char(text[match.start() - 1] if match.start() else "")
        and not identifier_char(text[match.end()] if match.end() < len(text) else "")
    ))


def latin_forms(term: str) -> set[str]:
    """Conservative adaptation of upstream singular/plural alternatives."""
    forms = {term}
    if not re.fullmatch(r"[a-z]{2,}", term):
        return forms
    forms.add(term + "s")
    if term.endswith(("s", "x", "z", "ch", "sh")):
        forms.add(term + "es")
    if term.endswith("ies") and len(term) > 4:
        forms.add(term[:-3] + "y")
    elif term.endswith(("ches", "shes", "xes", "zes", "sses")):
        forms.add(term[:-2])
    elif term.endswith("s") and not term.endswith(("ss", "us", "is")):
        forms.add(term[:-1])
    if term.endswith("y") and len(term) > 2 and term[-2] not in "aeiou":
        forms.add(term[:-1] + "ies")
    return forms


@dataclass(frozen=True)
class QueryContent:
    terms: frozenset[str]
    phrases: tuple[str, ...]
    historical: bool
    cutoff: date | None
    before: bool
    named_terms: frozenset[str]


@dataclass(frozen=True)
class ContentMatch:
    terms: frozenset[str]
    phrases: frozenset[str]
    strength: int


def query_content(query: str) -> QueryContent:
    normalized = normalize(query)
    terms = frozenset(t for t in _query_tokens(normalized)
                      if t not in EN_FUNCTION | QUERY_STOP | SUBJECT_STOP
                      and not _DATE.fullmatch(t)
                      and not (_CJK.fullmatch(t) and set(t) & FUNCTION_CHARS))
    phrases = quoted_phrases(normalized)
    dates = date_parts(normalized)
    cutoff = None
    if len(dates) == 1:
        try:
            cutoff = date(*(int(part) for part in dates[0]))
        except ValueError:
            pass
    # A bare four-digit token can be a numeric identifier. Require calendar
    # syntax or temporal context; never guess from the topic noun preceding it.
    # Year/month and ambiguous dates remain historical without an invented cutoff.
    calendar_year = any(
        "-" in m.group() or "/" in m.group()
        or _YEAR_CONTEXT.search(normalized[max(0, m.start() - 64):m.start()])
        for m in _YEAR.finditer(normalized)
    ) or bool(re.search(r"(?<![a-z0-9_'\-])(?:19|20)\d{2}\s*年", normalized))
    historical = bool(_HISTORY.search(normalized) or calendar_year or dates)
    before = bool(re.search(r"\bbefore\b|之前|以前", normalized))
    named_terms = frozenset(normalize(m.group()) for m in _NAMED.finditer(query)) & terms
    return QueryContent(terms, phrases, historical, cutoff, before, named_terms)


def match_content(plan: QueryContent, text: str, title: str = "") -> ContentMatch:
    evidence = normalize(title + "\n" + text)
    available = tokens(evidence)
    matched = frozenset(t for t in plan.terms if latin_forms(t) & available)
    phrases = frozenset(p for p in plan.phrases if phrase_present(p, evidence))
    # Integers define relevance bands, not calibrated probabilities. A literal
    # quoted phrase has precedence over the same words scattered across text.
    strength = len(matched) + len(phrases) * (len(plan.terms) + 1)
    return ContentMatch(matched, phrases, strength)


def common_subject(first: ContentMatch, other: ContentMatch) -> frozenset[str]:
    """A local move cannot discard any content evidence held by the anchor.

    Terms already represent query-relative morphological matches. Retaining
    all anchor terms protects names and IDs without guessing which words are
    generic, while still allowing partial queries and additional new content.
    """
    common = first.terms & other.terms
    if not first.terms or not other.terms:
        return frozenset()
    if not first.terms <= other.terms:
        return frozenset()
    if not first.phrases <= other.phrases:
        return frozenset()
    return common


def _statements(text: str, subject: frozenset[str]):
    """Yield conservative clauses and their unquoted assertion text.

    Sentence/clause punctuation and conjunctions separate evidence. Decimal
    dots, queried dotted identifiers and numeric time colons stay intact. Quoted names
    remain literal subject evidence, but quoted markers are never assertions.
    A quotation containing sentence punctuation is isolated as quoted prose.
    """
    pairs = {'"': '"', "'": "'", "“": "”", "「": "」", "『": "』"}
    visible = list(text)
    start = index = 0
    quote_start = None
    closer = ""
    prose_quote = False
    while index < len(text):
        char = text[index]
        left = text[index - 1] if index else ""
        right = text[index + 1] if index + 1 < len(text) else ""
        apostrophe = char == "'" and left.isalnum() and right.isalnum()
        dot_boundary = char == "." and not (
            left and right and (left.isalnum() or left in "_-")
            and (right.isalnum() or right in "_-")
        )
        if char == "." and not dot_boundary and quote_start is None:
            # A run-together sentence stop is ambiguous with a dotted name.
            # Retain nondecimal dots only when both adjacent components are
            # query subject evidence; otherwise fail closed at the boundary.
            lo, hi = index, index + 1
            while lo and (text[lo - 1].isalnum() or text[lo - 1] in "_'-"):
                lo -= 1
            while hi < len(text) and (text[hi].isalnum() or text[hi] in "_'-"):
                hi += 1
            dot_boundary = not ((left.isdecimal() and right.isdecimal())
                                or tokens(text[lo:hi]) <= subject)
        sentence_end = char in "\r\n。！？!?" or dot_boundary
        if quote_start is not None:
            visible[index] = " "
            prose_quote |= sentence_end
            if (char == closer and not apostrophe) or char in "\r\n":
                if prose_quote:
                    yield text[start:quote_start], "".join(visible[start:quote_start])
                    yield text[quote_start:index + 1], ""
                    start = index + 1
                quote_start = None
            index += 1
            continue
        if char in pairs and not apostrophe:
            quote_start, closer, prose_quote = index, pairs[char], False
            visible[index] = " "
            index += 1
            continue
        # A colon may introduce a correction label or be internal to a time.
        # Other unquoted colons conservatively separate a header/assertion.
        colon_boundary = char in ":：" and not (
            (left.isdecimal() and right.isdecimal() and char == ":")
            or _CHANGE_LABEL.fullmatch(text[start:index])
            or (_CHANGE_END.search("".join(visible[start:index]))
                and _CHANGE_PREDICATE.match(text, index + 1))
        )
        join = _CLAUSE_JOIN.match(text, index) if char.isalpha() else None
        if char in ",，" and _CORRECTION_PREAMBLE.fullmatch(text[start:index]):
            # Keep a label/apology with its immediately following assertion.
            # No sentence, newline, conjunction, or factual clause is bridged.
            index += 1
            continue
        if sentence_end or colon_boundary or char in ",，;；—()[]{}（）【】" or join:
            yield text[start:index], "".join(visible[start:index])
            # Keep a conjunction with its own clause (notably "but now").
            start = index if join else index + 1
            index = join.end() if join else start
        else:
            index += 1
    # An unmatched quotation is ambiguous: do not lend its words or markers
    # to an assertion outside it.
    end = quote_start if quote_start is not None else len(text)
    yield text[start:end], "".join(visible[start:end])


def _assertion_subject(statement: str, assertion: str, subject: frozenset[str],
                       phrases: frozenset[str]) -> str:
    """Ground identity in the assertion, admitting quotes only as name literals.

    Unquoted query anchors must independently support the assertion's role.
    A quoted name can fill another anchor when it is an exact queried literal
    or contains only subject/function terms. A quotation of the whole queried
    fact cannot supply its own assertion context, with or without punctuation.
    This evidence view is for local ordering only; delivered text is untouched.
    """
    available = tokens(assertion)
    outside = {t for t in subject if latin_forms(t) & available}
    grounded = list(assertion)
    for start, end in _quote_spans(statement):
        literal = statement[start:end].strip()
        quoted = tokens(literal)
        inside = {t for t in subject if latin_forms(t) & quoted}
        # An independent attribute/identity anchor must be asserted outside
        # this quote, rather than merely repeating a word from quoted prose.
        if not outside - inside:
            continue
        content = quoted - EN_FUNCTION - QUERY_STOP - SUBJECT_STOP
        if literal in phrases or content and content <= subject:
            grounded[start - 1:end + 1] = statement[start - 1:end + 1]
    return "".join(grounded)


def _bound_marker(text: str, subject: frozenset[str], phrases: frozenset[str],
                  marker: re.Pattern, named_terms: frozenset[str] = frozenset()) -> tuple[bool, bool]:
    any_change = False
    # An immediately adjacent anaphoric restatement may corroborate a change
    # only when both assertions explicitly repeat the queried identity.
    # Unmarked mentions and independent changes to other attributes cannot
    # contribute. Numeric/quoted identity is required in both assertions.
    identity = (named_terms & subject) | frozenset(t for t in subject if any(c.isdecimal() for c in t))
    for line in normalize(text).splitlines():
        previous: set[str] = set()
        for statement, assertion in _statements(line, subject):
            # A dotted/path identifier containing a marker word is still data.
            markers = (m for m in marker.finditer(assertion)
                       if not (m.start() and assertion[m.start() - 1] in "./_-'"
                               or m.start() > 1 and assertion[m.start() - 1] == ":"
                               and assertion[m.start() - 2].isalnum()))
            if next(markers, None) is None:
                previous = set()
                continue
            any_change = True
            grounded = _assertion_subject(statement, assertion, subject, phrases)
            available = tokens(grounded)
            named = {t for t in subject if latin_forms(t) & available}
            if not all(phrase_present(p, grounded) for p in phrases):
                previous = set()
                continue
            if subject and named == subject:
                return True, True
            if (identity or phrases) and identity <= named:
                if previous and _CHANGE_RESTATEMENT.match(assertion) and subject <= previous | named:
                    return True, True
                previous = named
            else:
                previous = set()
    return any_change, False


def change_lines(text: str, subject: frozenset[str], phrases: frozenset[str] = frozenset(),
                 named_terms: frozenset[str] = frozenset()) -> tuple[bool, bool]:
    """Bind changes locally, or corroborate changes explicitly naming one identity."""
    # A name may be capitalized in the evidence rather than in the question.
    # These are still only query-matched anchors, never new relevance evidence.
    named_terms = named_terms | (frozenset(normalize(m.group()) for m in _NAMED.finditer(text)) & subject)
    return _bound_marker(text, subject, phrases, CHANGE_RE, named_terms)


def current_assertion(text: str, subject: frozenset[str], phrases: frozenset[str] = frozenset(),
                      *, metadata: bool = False) -> bool:
    """Currentness, including record metadata, requires a local subject assertion.

    Record metadata cannot override textual currentness about another subject,
    or assemble a subject from disconnected mentions or quoted prose.
    """
    any_current, bound = _bound_marker(text, subject, phrases, _CURRENT)
    if bound or not metadata or any_current:
        return bound
    for statement, assertion in _statements(normalize(text), subject):
        grounded = _assertion_subject(statement, assertion, subject, phrases)
        available = tokens(grounded)
        if (subject and assertion.strip()
                and all(latin_forms(t) & available for t in subject)
                and all(phrase_present(p, grounded) for p in phrases)):
            return True
    return False


def conflict_preference(
    plan: QueryContent, first: ContentMatch, other: ContentMatch,
    first_text: str, other_text: str, first_day: date | None, other_day: date | None,
) -> int | None:
    """Return the preferred pair index (0/1), or no supported chronological decision.

    This is the decision core of upstream conflicts/apply_conflict. The caller
    supplies only its bounded leading candidates and applies the local move.
    """
    subject = common_subject(first, other)
    if not subject or first_day is None or other_day is None or first_day == other_day:
        return None
    if (len(subject) < 2 and len(plan.terms) > 1 and not first.phrases
            and not any(re.search(r"[_\-\d]", t) for t in subject)):
        return None
    if plan.historical:
        if plan.cutoff is None:
            return None
        eligible = [(d < plan.cutoff if plan.before else d <= plan.cutoff)
                    for d in (first_day, other_day)]
        if eligible[0] != eligible[1]:
            return 0 if eligible[0] else 1
        if not all(eligible):
            return None
    newer = int(other_day > first_day)
    text = other_text if newer else first_text
    return newer if change_lines(text, subject, first.phrases, plan.named_terms)[1] else None
