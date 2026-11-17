"""Bounded applicability checks over original RAW evidence.

RAW has no typed software-version or validity-window contract.  In particular,
``content.version``, release metadata and storage timestamps are not evidence
that a statement applies to a requested release or date.  This module reads
only explicit declarations and version-bound clauses in original text, plus
the existing withdrawal contract.  It is not an answer-entailment check.

Supported declarations (case insensitive, one per line) are ``Applies to
versions: 2.1, 2.2`` / ``适用版本: 2.1, 2.2``, ``Valid from: <ISO instant>``
and ``Valid until: <ISO instant>``.  The interval is [from, until); an explicit
``unbounded`` end is supported.  Dates mean UTC midnight.  Inline version
clauses support exact versions, major/minor wildcards and explicit from/before
bounds.  Their returned spans bind only the applicable original clauses, not
the whole document.  Partial coverage of a requested range remains unknown.
Ordinary related-material searches must not hide historical documents.
"""
from __future__ import annotations

from collections.abc import Mapping
from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
from typing import Any

from eimemory.knowledge.current_evidence import inactive_evidence_reason
from eimemory.raw._relevance import EN_FUNCTION, SUBJECT_STOP


POLICY_VERSION = "raw-applicability-v1"
MAX_TEXT_CHARS = 262_144
MAX_QUERY_CHARS = 4096
_VERSION = r"[0-9]+(?:\.[0-9]+){0,3}(?:-[A-Za-z][A-Za-z0-9]*(?:[.-][A-Za-z0-9]+)*)?"
_EXACT_VERSION = re.compile(rf"v?({_VERSION})", re.I)
_QUERY_VERSION = re.compile(
    rf"(?<![A-Za-z0-9_.])v({_VERSION})(?![A-Za-z0-9_+\-]|\.[A-Za-z0-9_])|"
    rf"(?:\bversion\s+|版本\s*[:：]?\s*)v?({_VERSION})(?![A-Za-z0-9_+\-]|\.[A-Za-z0-9_])",
    re.I,
)
_VERSION_REQUEST_MARKER = re.compile(r"(?<![A-Za-z0-9_.])v(?=\d)|(?:\bversion\s+|版本\s*[:：]?\s*)(?=[v0-9<>=])", re.I)
_QUERY_TIME = re.compile(
    r"(?:\bas\s+of\s+|截至\s*|截止\s*)(\d{4}-\d{2}-\d{2}(?:T[0-9:.]+(?:Z|[+-]\d\d:\d\d))?)",
    re.I,
)
_DECLARATION = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?(?P<label>applies[ \t]+to[ \t]+versions?|applicable[ \t]+versions?|适用版本|"
    r"valid[ \t]+from|valid[ \t]+until|有效起始|有效截止|status|状态)[ \t]*[:：][ \t]*(?P<value>.*?)[ \t]*$",
    re.I | re.M,
)
_WITHDRAWN_TEXT = frozenset({"revoked", "withdrawn", "superseded", "deprecated", "expired", "已撤销", "已废止", "已失效"})
_CURRENT_TEXT = frozenset({"active", "有效"})


@dataclass(frozen=True)
class ApplicabilityDecision:
    status: str
    reason: str
    required: bool
    policy_version: str = POLICY_VERSION
    spans: tuple[tuple[int, int], ...] = ()
    valid_until: str | None = None


def assess_applicability(
    record: Any, *, query: str, task_context: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ApplicabilityDecision:
    decision = _assess_applicability(record, query=query, task_context=task_context, now=now)
    if decision.status not in {"applicable", "unknown"} or not isinstance(query, str):
        return decision
    context = task_context if isinstance(task_context, Mapping) else {}
    request = context.get("applicability")
    historical = bool(_query_times(query) or
                      (isinstance(request, Mapping) and _instant(request.get("as_of")) is not None))
    text = _original_text(record)
    if historical or len(text) > MAX_TEXT_CHARS:
        return decision
    text = _assertion_text(text)
    cutoffs = [instant for match in _DECLARATION.finditer(text)
               if re.sub(r"\s+", " ", match["label"].lower()) in {"valid until", "有效截止"}
               if (instant := _instant(match["value"])) is not None]
    return replace(decision, valid_until=min(cutoffs).astimezone(timezone.utc).isoformat()) if cutoffs else decision


def _assess_applicability(
    record: Any, *, query: str, task_context: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ApplicabilityDecision:
    """Check a hydrated authoritative record before exposing a factual quote.

    ``task_context['applicability']`` is an optional *request* constraint with
    exactly ``version`` and/or ``as_of`` keys; it is never read as source proof.
    Query and explicit request constraints must agree.  ``now`` pins the clock
    across a request; callers may omit it for UTC wall time.  ``applicable``
    means no applicable restriction failed, not that the answer is supported.
    """
    payload = record if isinstance(record, Mapping) else {
        "status": getattr(record, "status", ""), "meta": getattr(record, "meta", {}),
    }
    if inactive_evidence_reason(payload):
        return _decision("not_applicable", "withdrawn_evidence")
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        return _decision("unknown", "applicability_query_bound")
    text = _original_text(record)
    if len(text) > MAX_TEXT_CHARS:
        return _decision("unknown", "applicability_text_bound")
    text = _assertion_text(text)
    requested, request_error = _query_constraint(query)
    if request_error:
        return _decision("unknown", request_error)
    versions = {next(value for value in match.groups() if value).lower()
                for match in _QUERY_VERSION.finditer(query)} if requested is None else set()
    if requested is None and _VERSION_REQUEST_MARKER.search(query) and not versions:
        return _decision("unknown", "unsupported_requested_version")
    times = _query_times(query)
    context = task_context if isinstance(task_context, Mapping) else {}
    constraints = context.get("applicability", {})
    if not isinstance(constraints, Mapping) or set(constraints) - {"version", "as_of"}:
        return _decision("unknown", "invalid_applicability_request")
    if "version" in constraints:
        explicit_version = _version(constraints["version"])
        if explicit_version is None:
            return _decision("unknown", "invalid_requested_version")
        explicit = _constraint(explicit_version)
        if explicit is None:
            return _decision("unknown", "unsupported_requested_version")
        if requested is not None and requested != explicit:
            return _decision("unknown", "ambiguous_applicability_request")
        requested = explicit
        versions.add(explicit_version)
    if "as_of" in constraints:
        value = constraints["as_of"]
        if not isinstance(value, str) or _instant(value) is None:
            return _decision("unknown", "invalid_requested_time")
        times.add(value)
    instants = {_instant(value) for value in times}
    if None in instants or len(versions) > 1 or len(instants) > 1:
        return _decision("unknown", "ambiguous_applicability_request")
    requested_version = next(iter(versions), None)
    if requested is None and requested_version:
        requested = _constraint(requested_version)
        if requested is None:
            return _decision("unknown", "unsupported_requested_version")
    requested_time = next(iter(instants), None)
    declarations: dict[str, list[str]] = {}
    for match in _DECLARATION.finditer(text):
        label = re.sub(r"\s+", " ", match["label"].lower())
        key = ("versions" if "version" in label or label == "适用版本" else
               "start" if label in {"valid from", "有效起始"} else
               "until" if label in {"valid until", "有效截止"} else "status")
        declarations.setdefault(key, []).append(match["value"])
    statuses = {value.lower() for value in declarations.get("status", [])}
    if statuses & _WITHDRAWN_TEXT:
        return _decision("not_applicable", "withdrawn_declaration")
    if statuses - _CURRENT_TEXT:
        return _decision("unknown", "unknown_evidence_status")
    if any(len(set(values)) > 1 for values in declarations.values()):
        return _decision("unknown", "conflicting_applicability_declarations")
    version_values = declarations.get("versions", [])
    version_decision = None
    if version_values:
        declared = [_version(value.strip()) for value in re.split(r"[,，、]", version_values[0])]
        if not declared or None in declared:
            return _decision("unknown", "unsupported_version_declaration")
        if requested is not None:
            declared_constraints = [_constraint(value) for value in declared]
            if None in declared_constraints:
                return _decision("unknown", "unsupported_version_declaration")
            if not any(_overlap(requested, item) for item in declared_constraints):
                return _decision("not_applicable", "version_mismatch")
            if not any(_covers(item, requested) for item in declared_constraints):
                version_decision = _decision("unknown", "version_range_not_covered")
            elif any(_inline_constraints(clause) for _, _, clause in _clauses(text)
                     if not _DECLARATION.fullmatch(clause)):
                # A document header does not erase explicit distinctions in
                # its body. Bind quotes to the relevant body clause as usual.
                body_decision = _clause_applicability(text, requested, query=query)
                version_decision = (body_decision if body_decision.status == "applicable" else
                                    _decision("unknown", "conflicting_applicability_declarations"))
    elif requested is not None:
        version_decision = _clause_applicability(text, requested, query=query)
        if version_decision.status == "not_applicable":
            return version_decision
    start_values, until_values = declarations.get("start", []), declarations.get("until", [])
    has_interval = bool(start_values or until_values)
    if has_interval:
        start = _instant(start_values[0]) if start_values else None
        unbounded = bool(until_values and until_values[0].lower() == "unbounded")
        until = _instant(until_values[0]) if until_values and not unbounded else None
        if (start_values and start is None) or (until_values and until is None and not unbounded):
            return _decision("unknown", "invalid_validity_declaration")
        if start and until and start >= until:
            return _decision("unknown", "invalid_validity_interval")
        at = requested_time or now or datetime.now(timezone.utc)
        if at.tzinfo is None or at.utcoffset() is None:
            return _decision("unknown", "invalid_reference_time")
        if start and at < start:
            return _decision("not_applicable", "not_yet_valid")
        if until and at >= until:
            return _decision("not_applicable", "validity_expired")
        if start is None or (until is None and not unbounded):
            return _decision("unknown", "incomplete_validity_interval")
    elif requested_time:
        return _decision("unknown", "time_applicability_unknown")
    if version_decision is not None:
        return version_decision
    required = bool(requested is not None or versions or times or declarations)
    return ApplicabilityDecision("applicable", "constraints_satisfied" if required else "no_explicit_constraints", required)


@dataclass(frozen=True)
class _Constraint:
    lower: tuple[int, ...] | None
    upper: tuple[int, ...] | None
    upper_inclusive: bool = False
    lower_inclusive: bool = True
    per_release: bool = False


# A dot-prefixed identifier is still a token after whitespace/CJK/backticks;
# splitting there silently drops extensions, dotfiles and property values.
_CLAUSE_BREAK = re.compile(r"[。；;，,\n!?！？]|\.(?![A-Za-z0-9_])")
_INLINE_VERSION = r"[0-9]{1,6}(?:\.(?:[0-9]{1,6}|[xX])){0,3}"
_INLINE = re.compile(
    rf"(?<![A-Za-z0-9_.])v(?P<v>{_INLINE_VERSION})(?![A-Za-z0-9_.+\-])|"
    rf"(?:版本|固件|协议|客户端|服务端|运行时|数据库|驱动|\bversion|\bfirmware|\bprotocol|\bruntime|\bsdk)[ \t]*v?(?P<p>{_INLINE_VERSION})(?![A-Za-z0-9_.+\-])|"
    rf"(?<![A-Za-z0-9_])(?-i:[A-Z][A-Za-z0-9_-]*)[ \t]+v?(?P<a>{_INLINE_VERSION})(?![A-Za-z0-9_.+\-]|\s*(?:seconds?|minutes?|hours?|days?|bytes?|MB|GB)\b)|"
    rf"(?P<s>{_INLINE_VERSION})(?=版|代|运行时|设备)|"
    rf"(?P<b>[0-9]{{1,6}}\.[0-9]{{1,6}}(?:\.[0-9]{{1,6}}){{0,2}})(?=开始|起|改为|仅接受|仅支持|只接受|支持|允许|必须|默认|需要|采用|使用)|"
    rf"(?<=到)(?P<u>{_INLINE_VERSION})(?![A-Za-z0-9_.+\-])|"
    r"第(?P<generation>[一二三四五六七八九十]|[0-9]+)代",
    re.I,
)
_BEFORE = re.compile(r"(?:未|没|尚未)(?:升级?|升)?到\s*$|(?:低于|小于|早于|before|below|less than)\s*$", re.I)
_FROM = re.compile(r"(?:从|自|since|from|at least)\s*$", re.I)
_GENERATIONS = dict(zip("一二三四五六七八九十", range(1, 11)))
_SUBJECT_SUFFIX = re.compile(
    r"(?:仍在|当前在|目前在|还没升到|尚未升级到|尚未升到|未升级到|没升级到|未升到|"
    r"仍运行|正在运行|当前运行|运行|使用|采用|接收|数据库|协议|固件|客户端|服务端|运行时|设备|版本|第|是|的)$")
_SUBJECT_FUNCTIONS = frozenset(EN_FUNCTION | SUBJECT_STOP | {
    "from", "since", "before", "version", "firmware", "protocol", "runtime", "v", "now",
    "package", "release", "reports", "report", "runs", "running", "uses"})
_MONTHS = {name: index for index, names in enumerate((
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
    ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
    ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
    ("dec", "december")), 1) for name in names}
_MONTH_PATTERN = "(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"
_ORDINAL_DAYS = dict(zip((
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
    "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
    "eighteenth", "nineteenth", "twentieth", "twenty-first", "twenty-second", "twenty-third",
    "twenty-fourth", "twenty-fifth", "twenty-sixth", "twenty-seventh", "twenty-eighth",
    "twenty-ninth", "thirtieth", "thirty-first"), range(1, 32)))
_DAY_PATTERN = (r"(?:[0-9]{1,2}(?:st|nd|rd|th)?|" +
                "|".join(word.replace("-", "[- \\t]") for word in
                         sorted(_ORDINAL_DAYS, key=len, reverse=True)) + ")")
_CALENDAR_DATE = re.compile(
    rf"\b(?:(?P<day>{_DAY_PATTERN})[ \t]+(?:of[ \t]+)?(?P<month>{_MONTH_PATTERN})\.?[ \t]+(?P<year>[0-9]{{4}})"
    rf"|(?P<month_first>{_MONTH_PATTERN})\.?[ \t]+(?:the[ \t]+)?(?P<day_last>{_DAY_PATTERN})[,]?[ \t]+(?P<year_last>[0-9]{{4}}))\b", re.I)
_COUNT_UNIT = re.compile(
    r"[ \t]+(?:[a-z]+[ \t]+){0,2}(?:failures|attempts|probes|requests|events|errors|retries|"
    r"items|packets|samples|cycles|records|checks|days|minutes|seconds)\b", re.I)

# Only explicit endpoint semantics form an interval. A migration "from v2 to
# v3", two releases in one sentence, or a plain quantity is not a declaration
# of compatibility throughout a range.
_RANGE_POINT = r"[0-9]{1,6}(?:\.[0-9]{1,6}){0,3}"
_RANGE_END = r"(?![A-Za-z0-9_+\-]|\.[A-Za-z0-9_])"
_RANGE_PATTERNS = (
    re.compile(rf"(?:\bfrom[ \t]+|从|自)v?(?P<lo>{_RANGE_POINT}){_RANGE_END}[ \t]*"
               rf"(?:\([ \t]*|（)?(?P<li>inclusive|exclusive|不含|含)(?:[ \t]*\)|）)?[ \t]*"
               rf"(?:up[ \t]+to[ \t]+|to[ \t]+|到|至)v?(?P<hi>{_RANGE_POINT}){_RANGE_END}[ \t]*"
               rf"(?:\([ \t]*|（)?(?P<ui>inclusive|exclusive|不含|含)(?:[ \t]*\)|）)?", re.I),
    re.compile(rf"(?P<lop>greater[ \t]+than(?:[ \t]+or[ \t]+equal[ \t]+to)?|at[ \t]+least|大于等于|大于|不低于|>=|≥|>)[ \t]*"
               rf"v?(?P<lo>{_RANGE_POINT}){_RANGE_END}[ \t]*(?:and[ \t]+|且|并且)[ \t]*"
               rf"(?P<uop>less[ \t]+than(?:[ \t]+or[ \t]+equal[ \t]+to)?|at[ \t]+most|小于等于|小于|不超过|<=|≤|<)[ \t]*"
               rf"v?(?P<hi>{_RANGE_POINT}){_RANGE_END}", re.I),
)
_RANGE_ANCHOR = re.compile(r"\b(?:versions?|releases?|firmware|runtime|sdk)\b|(?<![A-Za-z0-9_])v[0-9]|版本|固件|运行时", re.I)
_UNSPECIFIED_RANGE = re.compile(
    rf"(?:\b(?:from|between)[ \t]+|从|自)v?{_RANGE_POINT}[ \t]*(?:to|through|and|到|至)[ \t]*v?{_RANGE_POINT}|"
    rf"[\[(（［][ \t]*v?{_RANGE_POINT}[ \t]*[,，][ \t]*v?{_RANGE_POINT}[ \t]*[\])）］]", re.I)
_COMPARISON_VERSION_REQUEST = re.compile(r"(?:\b(?:versions?|releases?)|版本)[ \t]*(?:[<>=≤≥]+)[ \t]*v?[0-9]", re.I)
_RANGE_SUBJECT_TAIL = re.compile(
    r"(?:[ \t]*\b(?:versions?|releases?|from|since)\b|适用区间[：:]?|版本|从|自)[ \t]*$", re.I)
_RANGE_CONTINUATION = re.compile(
    r"^(?:在(?:这个|该|上述)(?:整个)?(?:版本)?区间内|"
    r"(?:throughout|within|in)[ \t]+(?:this|that|the stated)[ \t]+(?:entire[ \t]+)?(?:version[ \t]+)?(?:range|interval)\b|"
    r"(?:every|each)[ \t]+(?:such[ \t]+)?release\b)", re.I)
_PER_RELEASE_RANGE = re.compile(
    rf"(?:\bfrom[ \t]+|从|自)?v?(?P<lo>{_RANGE_POINT}){_RANGE_END}[ \t]*"
    rf"(?:through\b|to\b|到|至)[ \t]*v?(?P<hi>{_RANGE_POINT}){_RANGE_END}", re.I)
_PER_RELEASE_EXCLUSION = re.compile(
    rf"(?:\b(?:except|excluding|exclude|not[ \t]+including|but[ \t]+not)[ \t]+|不含|排除|不包括)[ \t]*"
    rf"(?P<marker>v(?:ersion[ \t]+)?|release[ \t]+|版本)?(?P<point>{_RANGE_POINT}){_RANGE_END}", re.I)


def _has_release_exclusion(query: str) -> bool:
    for match in _PER_RELEASE_EXCLUSION.finditer(query):
        tail = query[match.end():]
        if not match['marker'] and (_COUNT_UNIT.match(tail) or re.match(r"[ \t]*[次个条项秒分天]", tail)):
            continue  # Explicit counted quantities do not exclude a release.
        return True
    return False


def _per_release_range(text: str):
    """Recognize a request for separate entries, never source range proof.

    Each in-range source can contribute its own entry. The answer-support
    review must still find every requested entry across the selected sources;
    applicability alone makes no completeness assertion.
    """
    matches = list(_PER_RELEASE_RANGE.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    prefix, suffix = text[:match.start()], text[match.end():]
    english = re.search(r"\b(?:for[ \t]+)?(?:each|every)[ \t]+(.+?)[ \t]+(?:release|version)[ \t]*$", prefix, re.I)
    chinese = re.match(r"[ \t]*(?:的)?每个版本", suffix)
    if not (english or chinese):
        return None
    if re.match(r"[ \t]*(?:[（(][ \t]*)?(?:inclusive|exclusive)\b", suffix, re.I):
        return None
    owner = english.group(1) if english else re.sub(r"^(?:请)?(?:分别|逐一)?(?:列出|说明|给出)", "", prefix)
    owner = re.sub(r"(?:从|自)$", "", owner).strip()
    if not owner:
        return None
    lower, upper = _constraint(match['lo']), _constraint(match['hi'])
    if lower is None or upper is None:
        return None
    return match, _Constraint(lower.lower, upper.upper, True, True, True), owner


def _range_version_context(text: str, match) -> bool:
    # A version elsewhere in the sentence cannot lend its meaning to a
    # quantity interval. Require an endpoint's own v marker or immediately
    # adjacent release grammar, including the Chinese postposed noun phrase.
    return bool(
        re.search(r"(?<![A-Za-z0-9_])v[0-9]", match.group(), re.I) or
        re.search(r"(?:\b(?:versions?|releases?|firmware|runtime|sdk)|版本|固件|运行时)[ \t]*[:：]?[ \t]*$",
                  text[:match.start()], re.I) or
        re.match(r"[ \t]*(?:的)?(?:所有|全部)?版本", text[match.end():]))


def _bounded_ranges(text: str):
    if not _RANGE_ANCHOR.search(text):
        return []
    ranges = []
    for pattern in _RANGE_PATTERNS:
        for match in pattern.finditer(text):
            if not _range_version_context(text, match):
                continue
            groups = match.groupdict()
            if "li" in groups:
                lower_inclusive = groups["li"].lower() in {"inclusive", "含"}
                upper_inclusive = groups["ui"].lower() in {"inclusive", "含"}
            else:
                lower_op = re.sub(r"[ \t]+", " ", groups["lop"].lower())
                upper_op = re.sub(r"[ \t]+", " ", groups["uop"].lower())
                lower_inclusive = lower_op not in {"greater than", "大于", ">"}
                upper_inclusive = upper_op not in {"less than", "小于", "<"}
            lower = _constraint(match["lo"]).lower
            upper = _constraint(match["hi"]).upper
            ranges.append((match, _Constraint(lower, upper, upper_inclusive, lower_inclusive)))
    return sorted(ranges, key=lambda item: item[0].start())


def _nonempty_constraint(value: _Constraint) -> bool:
    return value.lower is None or value.upper is None or value.lower < value.upper or (
        value.lower == value.upper and value.lower_inclusive and value.upper_inclusive)


def _non_version_number(match, text):
    """Disambiguate weak product-number syntax, never explicit v/version."""
    value = match.group("a")
    if not value or not value.isdecimal():
        return False
    word = match.group().split()[0].lower()
    if word in {"after", "before", "within", "over", "under"} and _COUNT_UNIT.match(text, match.end()):
        return True
    # A yearless calendar qualifier can coexist with an explicit release.
    # Require a calendar determiner and another explicit version anchor;
    # bare/month-named products and v/version-prefixed numbers stay intact.
    if word in _MONTHS and 1 <= int(value) <= 31:
        prefix = text[:match.start()]
        date_context = re.search(r"\b(?:on|dated|as of|the)[ \t]+$", prefix, re.I)
        following = re.match(r"[ \t]+[a-z][a-z-]*\b", text[match.end():])
        explicit = any(other.group("p") or other.group("v") for other in _INLINE.finditer(text))
        return bool(date_context and following and explicit)
    return False


def _calendar_instant(date) -> datetime | None:
    try:
        day = re.sub(r"[ \t]", "-", (date["day"] or date["day_last"]).lower())
        number = _ORDINAL_DAYS[day] if day in _ORDINAL_DAYS else int(re.sub(r"(?:st|nd|rd|th)$", "", day))
        return datetime(int(date["year"] or date["year_last"]),
                        _MONTHS[(date["month"] or date["month_first"]).lower()],
                        number, tzinfo=timezone.utc)
    except ValueError:
        return None


def _query_times(query: str) -> set[str]:
    times = {match.group(1) for match in _QUERY_TIME.finditer(query)}
    for date in _CALENDAR_DATE.finditer(query):
        # Only an explicit as-of request changes the reference clock. An
        # inventory date, example or incidental "on <date>" is not a validity
        # override. Invalid as-of dates must not fall back to wall time.
        if re.search(r"\bas[ \t]+of[ \t]+(?:the[ \t]+)?$", query[:date.start()], re.I):
            instant = _calendar_instant(date)
            times.add(instant.isoformat() if instant is not None else "invalid")
    return times


def _calendar_date_spans(text: str):
    # The permissive product/major-version form also matches sentence-initial
    # "On 23" and "April 2027". A validated calendar date is not a software
    # version. Keep original offsets and still recognize real versions nearby.
    return [date.span() for date in _CALENDAR_DATE.finditer(text) if _calendar_instant(date) is not None]


def _inline_matches(text: str):
    dates = _calendar_date_spans(text)
    starts = [start for start, _end in dates]
    for match in _INLINE.finditer(text):
        if _non_version_number(match, text):
            continue
        # Explicit version markers and product names retain their authority,
        # even when a following month/year resembles a calendar date.
        word = match.group().split()[0].lower()
        index = bisect_right(starts, match.end() - 1) - 1
        # A month-named product before a date ("May 4 June 2027") is
        # different from the month inside that date. May is also a function
        # word, so it must not fall back to the preposition rule.
        possible_date = match.group("a") and (
            index >= 0 and dates[index][0] <= match.start() and match.start() + len(word) <= dates[index][1]
            if word in _MONTHS else word in EN_FUNCTION)
        if possible_date and index >= 0 and dates[index][1] > match.start():
            continue
        yield match


def _constraint(value: str, *, relation: str = "exact") -> _Constraint | None:
    if not re.fullmatch(_INLINE_VERSION, value):
        return None
    parts = value.lower().split(".")
    if "x" in parts:
        if parts[-1] != "x" or parts.count("x") != 1:
            return None
        prefix = [int(item) for item in parts[:-1]]
        start = tuple(prefix + [0] * (4 - len(prefix)))
        prefix[-1] += 1
        return _Constraint(start, tuple(prefix + [0] * (4 - len(prefix))))
    point = tuple([int(item) for item in parts] + [0] * (4 - len(parts)))
    if relation == "before":
        return _Constraint(None, point)
    if relation == "from":
        return _Constraint(point, None)
    return _Constraint(point, point, True)


def _split_clauses(text: str):
    start = 0
    dates = _calendar_date_spans(text)
    date_starts = [left for left, _right in dates]
    for boundary in (*_CLAUSE_BREAK.finditer(text), None):
        if boundary is not None:
            index = bisect_right(date_starts, boundary.start()) - 1
            if index >= 0 and boundary.start() < dates[index][1]:
                continue
        end = boundary.start() if boundary is not None else len(text)
        raw = text[start:end]
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        if right > left:
            yield start + left, start + right, raw[left:right]
        start = boundary.end() if boundary is not None else len(text)


def _clauses(text: str):
    clauses = list(_split_clauses(text))
    index = 0
    while index < len(clauses):
        start, end, clause = clauses[index]
        ranges = _bounded_ranges(clause)
        if len(ranges) == 1 and _nonempty_constraint(ranges[0][1]) and index + 1 < len(clauses):
            following_start, _following_end, following = clauses[index + 1]
            separator = text[end:following_start]
            explicit_reference = _RANGE_CONTINUATION.match(following)
            sentence_local = not (re.match(r"(?:every|each)\b", following, re.I) and
                                  re.search(r"[。.!?！？]", separator))
            if "\n" not in separator and explicit_reference and sentence_local:
                # Bind only an immediately explicit range reference and the
                # remainder of that one sentence. Never inherit into arbitrary
                # later prose, another paragraph, or a neighboring version.
                index += 1
                end = clauses[index][1]
                while index + 1 < len(clauses):
                    next_start = clauses[index + 1][0]
                    if re.search(r"[。.!?！？;；\n]", text[end:next_start]) or _inline_constraints(clauses[index + 1][2]):
                        break
                    index += 1
                    end = clauses[index][1]
                clause = text[start:end]
        yield start, end, clause
        index += 1


def _inline_constraints(clause: str) -> list[_Constraint]:
    ranges = _bounded_ranges(clause)
    found = [value for _match, value in ranges]
    for match in _inline_matches(clause):
        if any(left.start() <= match.end() - 1 < left.end() for left, _value in ranges):
            continue
        groups = match.groupdict()
        if groups["generation"]:
            word = groups["generation"]
            value = str(_GENERATIONS[word]) if word in _GENERATIONS else word
        else:
            value = next(value for value in groups.values() if value)
        # Test the original prefix immediately before the numeric token, not
        # an entity name or an unrelated number elsewhere in the sentence.
        number_start = next(match.start(key) for key, item in groups.items() if item)
        prefix = clause[:number_start]
        relation = "before" if _BEFORE.search(prefix) else "from" if _FROM.search(prefix) else "exact"
        item = _constraint(value, relation=relation)
        if item is not None and item not in found:
            found.append(item)
    return found


def _query_constraint(query: str) -> tuple[_Constraint | None, str]:
    # A first clause stating the running version is the target; a subsequent
    # question about applying another release does not replace that target.
    unspecified = [match for match in _UNSPECIFIED_RANGE.finditer(query)
                   if _range_version_context(query, match)]
    for start, end, clause in _clauses(query):
        enumeration = _per_release_range(clause)
        if enumeration:
            if _has_release_exclusion(query):
                return None, "unsupported_requested_version"
            value = enumeration[1]
            return (value, "") if _nonempty_constraint(value) else (None, "unsupported_requested_version")
        if any(start <= match.start() < end for match in unspecified):
            return None, "unsupported_requested_version"
        if _COMPARISON_VERSION_REQUEST.search(clause) and not _bounded_ranges(clause):
            return None, "unsupported_requested_version"
        values = _inline_constraints(clause)
        if any(not _nonempty_constraint(value) for value in values):
            return None, "unsupported_requested_version"
        if len(values) > 1:
            return None, "ambiguous_applicability_request"
        if values:
            return values[0], ""
    return None, ""


def _version_subject(clause: str) -> frozenset[str]:
    enumeration = _per_release_range(clause)
    if enumeration:
        return _version_subject(enumeration[2] + " v0")
    ranges = _bounded_ranges(clause)
    if ranges:
        prefix = clause[:ranges[0][0].start()].strip()
        while (trimmed := _RANGE_SUBJECT_TAIL.sub("", prefix)) != prefix:
            prefix = trimmed
        # Reuse the same owner/product rules as an exact release. The range
        # operators themselves are syntax, not a new product identity.
        return _version_subject(prefix + " v0")
    match = next(_inline_matches(clause), None)
    if match is None:
        return frozenset()
    groups = match.groupdict()
    start = next(match.start(key) for key, value in groups.items() if value)
    prefix = clause[:start].strip()
    # Attributes in an interrogative prefix are not the product identity.
    # Keep the identifier nearest the release anchor, plus a Chinese project
    # qualifier where present; this still distinguishes SDK/Quasar and owners.
    words = list(re.finditer(r"[A-Za-z][A-Za-z0-9_-]*", prefix))
    tail = {"version", "firmware", "protocol", "runtime", "v", "package", "release",
            "reports", "report", "runs", "running", "uses"}
    if groups.get("a") and match.group().split()[0].lower() != "v" and words and words[-1].group().lower() == "v":
        # The weak product form also consumes "Product v4". That v is a
        # release marker, not part of the identity, just as in "version 4".
        words.pop()
    if not groups.get("a"):
        while words and words[-1].group().lower() in tail:
            words.pop()
    subjects = set()
    right = None
    for word in reversed(words):
        value = word.group().lower()
        if right is not None and prefix[word.end():right].strip():
            break
        if value in _SUBJECT_FUNCTIONS and not (right is None and groups.get("a")):
            break
        subjects.add(value)
        right = word.start()
    chinese = re.match(r"[\u3400-\u4dbf\u4e00-\u9fff]+", prefix)
    if chinese:
        value = re.sub(r"^(?:请问|请|关于)", "", chinese.group())
        while (trimmed := _SUBJECT_SUFFIX.sub("", value)) != value:
            value = trimmed
        if value and value not in {"从", "自", "低于", "小于", "早于"}:
            subjects.add(value)
    return frozenset(subjects)


def _overlap(left: _Constraint, right: _Constraint) -> bool:
    for lower, upper in ((left, right), (right, left)):
        if lower.lower is not None and upper.upper is not None:
            if lower.lower > upper.upper or (lower.lower == upper.upper and not (
                    lower.lower_inclusive and upper.upper_inclusive)):
                return False
    return True


def _covers(source: _Constraint, requested: _Constraint) -> bool:
    lower = source.lower is None or (requested.lower is not None and (
        source.lower < requested.lower or (source.lower == requested.lower and (
            source.lower_inclusive or not requested.lower_inclusive))))
    upper = source.upper is None or (requested.upper is not None and (
        source.upper > requested.upper or (source.upper == requested.upper and
                                          (source.upper_inclusive or not requested.upper_inclusive))))
    return lower and upper


def _clause_applicability(text: str, requested: _Constraint, *, query: str) -> ApplicabilityDecision:
    overlaps, covered, declared, ambiguous = [], [], False, False
    requested_subject = next((_version_subject(clause) for _, _, clause in _clauses(query)
                              if _inline_constraints(clause) or _per_release_range(clause)), frozenset())
    previous_subject: frozenset[str] = frozenset()
    previous_end = 0
    for start, end, clause in _clauses(text):
        if re.search(r"[。.!?！？\n]", text[previous_end:start]):
            previous_subject = frozenset()
        previous_end = end
        values = _inline_constraints(clause)
        subject = _version_subject(clause)
        if not values and len(requested_subject) == 1:
            owner = next(iter(requested_subject))
            if re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fff]+", owner):
                # Chinese product+release has no word-space ("产品2.3的…").
                # Recognize it only for the already established query owner,
                # never turn an arbitrary decimal quantity into a release.
                bound = re.match(rf"{re.escape(owner)}[ \t]*v?(?P<version>[0-9]+(?:\.[0-9]+){{1,3}})"
                                 r"(?=的|版本|版)", clause)
                if bound and (point := _constraint(bound['version'])) is not None:
                    values, subject = [point], requested_subject
        if subject:
            if subject.issubset(previous_subject):
                subject = previous_subject
            previous_subject = subject
        else:
            subject = previous_subject
        if len(values) > 1 or any(not _nonempty_constraint(value) for value in values):
            ambiguous = True
            continue
        if not values:
            previous_subject = frozenset()
            continue
        # A matching number in a different product cannot qualify this query.
        # An explicit local subject must contain the requested identity; a
        # subjectless continuation may inherit only the preceding identity.
        if requested_subject and not requested_subject.issubset(subject):
            ambiguous = True
            continue
        declared = True
        value = values[0]
        if _overlap(value, requested):
            overlaps.append((start, end))
            if _covers(value, requested) or (requested.per_release and _covers(requested, value)):
                covered.append((start, end))
    if covered:
        reason = "enumerated_version_clause_matched" if requested.per_release else "version_clause_matched"
        return ApplicabilityDecision("applicable", reason, True, spans=tuple(covered))
    if overlaps:
        return ApplicabilityDecision("unknown", "version_range_not_covered", True, spans=tuple(overlaps))
    if declared and not ambiguous:
        return _decision("not_applicable", "version_mismatch")
    return _decision("unknown", "version_applicability_unknown")


def _decision(status: str, reason: str) -> ApplicabilityDecision:
    return ApplicabilityDecision(status, reason, True)


def _version(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    match = _EXACT_VERSION.fullmatch(value.strip())
    return match.group(1).lower() if match else None


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        value = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except ValueError:
        return None


def _original_text(record: Any) -> str:
    if isinstance(record, Mapping):
        content = record.get("content")
        if isinstance(content, Mapping):
            values = [content.get(key) for key in ("raw_text", "text", "body")]
            values += [record.get(key) for key in ("summary", "detail", "title")]
        else:
            values = [record.get(key) for key in ("raw_text", "text", "body", "summary", "detail", "title")]
    else:
        content = getattr(record, "content", {})
        content = content if isinstance(content, Mapping) else {}
        values = [content.get(key) for key in ("raw_text", "text", "body")]
        values += [getattr(record, key, "") for key in ("summary", "detail", "title")]
    return next((value for value in values if isinstance(value, str) and value.strip()), "")


def _assertion_text(text: str) -> str:
    """Ignore explicit Markdown examples without changing original offsets."""
    lines = []
    fence = ""
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        quoted = bool(re.match(r"^[ \t]*>", line))
        hidden = bool(fence or marker or quoted)
        if marker and not quoted:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
        if hidden:
            lines.append("".join(char if char in "\r\n" else " " for char in line))
        else:
            lines.append(line)
    return "".join(lines)
