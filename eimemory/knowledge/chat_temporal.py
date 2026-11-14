"""Grounded ordinary-chat observations over the existing temporal journal.

This module owns no storage or projection authority. L1 remains the active
answer evidence; the append-only protocol decides its business applicability.
Unknown observations are retained but cannot become a current fact by default.
Managed stores require upgraded readers: default-off MemoryAPI subtracts their
active bodies; an older binary has no such protection. Inactive journals alone
are not a rollback compatibility boundary for active evidence or its exports.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
import re
import sqlite3
from time import perf_counter

from eimemory.contracts.l1_source_order import (
    L1SourceOrderBlocked, l1_source_event_binding, require_l1_source_parent_current,
    require_l1_supersession_order, validate_l1_source_event,
)
from eimemory.contracts.recall_boundary import authority_digest, exact_ref
from eimemory.contracts.temporal_facts import (
    MAX_EVENTS, TemporalFactAccess, covers, family_id_for, parse_instant,
    resolve_family, revision_id_for, temporal_payload,
)
from eimemory.metadata import business_metadata
from eimemory.models.records import LinkRef, ScopeRef


KEY = "ordinary_chat_temporal_v1"
NAMESPACE = "ordinary.chat.literal.v1"
SLOT_NAMESPACE = "ordinary.chat.literal-slot.v1"
NAMESPACES = frozenset({NAMESPACE, SLOT_NAMESPACE})
CHANNELS = frozenset({"hermes", "codex", "openclaw"})
_DATE = r"(?:\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2}))?|\d{4}年\d{1,2}月\d{1,2}日)"
_DOUBT = re.compile(r"可能|也许|不详|不确定|未知|不是|不再是|假如|假设|据说|[?？]|\b(?:maybe|possibly|unknown|unspecified|not|if|allegedly)\b", re.I)
_PRONOUNS = {"我", "你", "他", "她", "它", "他们", "该对象", "此对象", "这个", "那个",
             "i", "you", "he", "she", "it", "they", "this", "that", "we"}
_INTENT = re.compile(r"(?:^|[：:])\s*(?:自|从|since\b|from\b)|生效|有效期|日期|effective\b|as of\b|更正|纠正|撤回|\b(?:correction|retract)\b", re.I)


def source_user(parent):
    text = str(parent.content.get("text") or "")
    match = re.fullmatch(r"User:[^\S\r\n]*(.*?)\r?\nAssistant:.*", text, re.S)
    if match is None:
        raise ValueError("chat_original_user_missing")
    return match[1].strip()


def _chat_parent(parent):
    meta = business_metadata(parent.meta)
    return (parent.source_id in CHANNELS
            and meta.get("memory_layer") == "l0" and meta.get("capture_origin") == "turn_sync"
            and meta.get("runtime_channel") == parent.source_id)


def eligible_parent(api, parent):
    return getattr(api, "_ordinary_chat_temporal", False) and _chat_parent(parent)


def _date(literal):
    if type(literal) is not str or re.fullmatch(_DATE, literal) is None:
        raise ValueError("chat_date_unresolved")
    if "年" in literal:
        year, month, day = map(int, re.findall(r"\d+", literal))
        return datetime(year, month, day, tzinfo=timezone.utc).isoformat()
    if "T" not in literal:
        return datetime.strptime(literal, "%Y-%m-%d").replace(tzinfo=timezone.utc).isoformat()
    return parse_instant(literal).isoformat()


def query_time(query):
    """(explicit valid instant or server-now, usable). Never known-time replay.

    Calendar-only literals mean midnight UTC, consistently on write and read.
    IDs containing dates have no temporal meaning without a temporal operator.
    """
    cues = list(re.finditer(r"(?:截至|截止|在|于|as\s+of\s+|on\s+|at\s+)(" + _DATE + r")(?![\dA-Za-z])", query, re.I))
    cues.extend(re.finditer(r"(" + _DATE + r")(?:时|的时候)", query))
    grounded_dates = {(m.start(1), m.end(1)): m[1] for m in cues}
    if grounded_dates:
        # Count temporal operators, not unrelated year-looking opaque IDs.
        if len(grounded_dates) != 1 or re.search(_DATE + r"\s*(?:到|至|to\b|and\b)\s*" + _DATE, query, re.I):
            return None, False
        try:
            return _date(next(iter(grounded_dates.values()))), True
        except ValueError:
            return None, False
    if re.search(r"截至|截止|过去|当时|曾经|之前|以后|\bas\s+of\b|\b(?:before|after|historically|when)\b", query, re.I):
        return None, False
    if re.search(r"(?:在|于|\bon\s+|\bat\s+)\d", query, re.I):
        return None, False
    return None, True


def _literal(user, field):
    if type(field) is not dict or set(field) != {"text", "span"}:
        raise ValueError("chat_literal_invalid")
    span, text = field["span"], field["text"]
    if (type(text) is not str or not text or text != text.strip() or len(text) > 512
            or type(span) is not list or len(span) != 2
            or any(type(i) is not int for i in span)
            or not 0 <= span[0] < span[1] <= len(user) or user[span[0]:span[1]] != text):
        raise ValueError("chat_literal_ungrounded")
    return text


def _span(user, text):
    if user.count(text) != 1:
        raise ValueError("chat_literal_not_unique")
    start = user.index(text)
    return {"text": text, "span": [start, start + len(text)]}


def _compile_literal_proposal(user, proposed, source_digest):
    """Locate a new literal-only DTO; never repair an existing span DTO.

    At most four fields of 512 characters search a bounded original source.
    The second search starts one character later so overlaps also refuse.
    Offsets grant no authority: the caller still runs the legacy validator.
    """
    keys = {"schema", "source_digest", "slot", "value", "valid_from", "valid_until", "operation"}
    if (type(proposed) is not dict or set(proposed) != keys
            or type(proposed["schema"]) is not str or proposed["schema"] != "ordinary_temporal_literals.v1"
            or type(proposed["source_digest"]) is not str or proposed["source_digest"] != source_digest
            or type(proposed["operation"]) is not str
            or proposed["operation"] not in {"claim", "transition", "correction", "retraction"}
            or len(user) > 65_536):
        raise ValueError("chat_literal_proposal_invalid")
    compiled = {"source_digest": proposed["source_digest"], "operation": proposed["operation"]}
    for key in ("slot", "value", "valid_from", "valid_until"):
        literal = proposed[key]
        if key == "valid_until" and literal is None:
            compiled[key] = None
            continue
        if type(literal) is not str or not literal or len(literal) > 512 or literal != literal.strip():
            raise ValueError("chat_literal_ungrounded")
        start = user.find(literal)
        if start < 0 or user.find(literal, start + 1) != -1:
            raise ValueError("chat_literal_not_unique")
        compiled[key] = {"text": literal, "span": [start, start + len(literal)]}
    return compiled


def _business_date_role(user, field, *, upper=False):
    start, end = field["span"]
    prefix, suffix = user[:start], user[end:]
    before = r"(?:到|至|until\s+|to\s+)$" if upper else r"(?:自|从|since\s+|from\s+|effective\s+|as of\s+)$"
    after = r"^(?:止|结束)" if upper else r"^(?:起|开始|生效)"
    if not (re.search(before, prefix, re.I) or re.search(after, suffix)):
        raise ValueError("chat_business_date_ungrounded")


def _assertion_clause(user, proposed):
    keys = ("slot", "value", "valid_from") if "slot" in proposed else ("subject", "attribute", "value", "valid_from")
    spans = [proposed[key]["span"] for key in keys]
    if proposed["valid_until"] is not None:
        spans.append(proposed["valid_until"]["span"])
    first, last = min(s[0] for s in spans), max(s[1] for s in spans)
    separators = list(re.finditer(r"[\n。；;!?？]|\.(?=\s|$)", user))
    if any(first <= sep.start() < last for sep in separators):
        raise ValueError("chat_cross_clause_proposal")
    left = max([sep.end() for sep in separators if sep.end() <= first] or [0])
    right = min([sep.end() for sep in separators if sep.start() >= last] or [len(user)])
    quote = user[left:right].strip()
    return quote


def _dated_body(text):
    body = text.strip().rstrip("。.")
    operation = "claim"
    correction = re.match(r"(?:更正|纠正|撤回|Correction|Retraction)\s*[：:]\s*", body, re.I)
    if correction:
        operation = "retraction" if re.search(r"撤回|Retraction", correction[0], re.I) else "correction"
        body = body[correction.end():]
    bounded = re.match(r"(?:从|自)(" + _DATE + r")(?:到|至)(" + _DATE + r")[，,\s]*", body)
    until = None
    if bounded:
        start, until = bounded[1], bounded[2]
        body = body[bounded.end():]
    else:
        prefix = re.match(r"(?:自|从|Since\s+|From\s+|As\s+of\s+|Effective\s+)(" + _DATE + r")(?:起|开始)?[，,\s]*", body, re.I)
        suffix = re.search(r"\s+(?:effective|as of)\s+(" + _DATE + r")$", body, re.I)
        if prefix:
            start, body = prefix[1], body[prefix.end():]
        elif suffix:
            start, body = suffix[1], body[:suffix.start()]
        else:
            raise ValueError("chat_explicit_date_required")
    return start, until, operation, body.strip()


def _quoted_value(value):
    quoted = ((value.startswith('"') and value.endswith('"') and value.count('"') == 2
               and '“' not in value and '”' not in value)
              or (value.startswith('“') and value.endswith('”')
                  and value.count('“') == value.count('”') == 1 and '"' not in value))
    if not quoted and any(char in value for char in '\"“”'):
        raise ValueError("chat_value_quote_unresolved")
    return quoted


def _asserted_context(text, value):
    # Only an exact, complete quoted RHS is opaque. A qualifier outside that
    # value remains part of the assertion, even if it shares a doubt word.
    context = text
    if _quoted_value(value):
        if text.count(value) != 1:
            raise ValueError("chat_literal_not_unique")
        at = text.index(value)
        context = text[:at] + text[at + len(value):]
    if _DOUBT.search(context):
        raise ValueError("chat_assertion_unresolved")


def _relation(body):
    relation = re.fullmatch(r"([^，,。；;\n!?？]+?)(改为|是|为|\s+changed\s+to\s+|\s+is\s+|\s*=\s*)([^，,。；;\n]+)", body, re.I)
    if relation is None:
        raise ValueError("chat_relation_unresolved")
    # An unquoted annotated RHS is not a scalar assertion: a parenthesis may
    # contain a condition, proposal or qualification. Leave that grammar
    # unresolved instead of guessing its meaning from a growing word list.
    # Fully quoted literal names and plain states remain opaque values.
    value = relation[3].strip()
    quoted = _quoted_value(value)
    if value == "待定" or (not quoted and re.search(r"[（）()]", value)):
        raise ValueError("chat_assertion_unresolved")
    return relation[1].strip(), relation[2].strip(), value


def _slot_identity(lhs):
    """Split only a literal boundary; otherwise name the WHOLE slot, not an entity.

    This is also the canonicalizer for structured proposals. A model's choice
    of segmentation cannot create a second family or an external entity ID.
    """
    if lhs.count('"') % 2 or lhs.count('“') != lhs.count('”'):
        raise ValueError("chat_identity_unresolved")
    delimiters = list(re.finditer(r"的|'s\s+|’s\s+|\s*::\s*", lhs))
    identity = None
    if len(delimiters) == 1:
        delimiter = delimiters[0]
        identity = lhs[:delimiter.start()].strip(), lhs[delimiter.end():].strip()
    elif not delimiters:
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_./:-]*)([\u3400-\u9fff]+)", lhs)
        if match:
            identity = match[1], match[2]
    if identity:
        subject, attribute = identity
        if not subject or not attribute or subject.strip('\"“”').casefold() in _PRONOUNS or attribute.casefold() in _PRONOUNS:
            raise ValueError("chat_identity_unresolved")
        return NAMESPACE, subject, attribute
    if (not lhs or len(lhs) > 512
            or re.match(r"(?:" + "|".join(re.escape(p) + (r"\b" if p.isascii() else "") for p in _PRONOUNS) + r")", lhs, re.I)):
        raise ValueError("chat_identity_unresolved")
    # The reserved attribute is local to this namespace. No asserted entity or
    # guessed noun boundary is hidden in this representation.
    return SLOT_NAMESPACE, lhs, "literal-slot"


def _fact_for_slot(lhs):
    namespace, subject, attribute = _slot_identity(lhs)
    return dict(namespace=namespace, entity_id=subject, attribute=attribute,
                cardinality="single", applicability={})


def _compatible_proposal(user, text):
    """Literal assertion grammar for old text-only DTOs and structured DTOs."""
    if user.count(text) != 1:
        raise ValueError("chat_assertion_unresolved")
    start, until, operation, body = _dated_body(text)
    lhs, connective, value = _relation(body)
    _asserted_context(text, value)
    if operation == "claim" and re.search(r"改为|changed", connective, re.I):
        operation = "transition"
    namespace, subject, attribute = _slot_identity(lhs)
    if namespace == NAMESPACE:
        fields = dict(subject=_span(user, subject), attribute=_span(user, attribute))
    else:
        fields = dict(slot=_span(user, lhs))
    return dict(source_digest=sha256(user.encode()).hexdigest(), **fields, value=_span(user, value),
                valid_from=_span(user, start), valid_until=_span(user, until) if until else None,
                operation=operation)


def ground_assertion(user, text, proposal=None):
    """Validate untrusted model fields against the *original user* characters.

    New model proposals supply only literals; legacy proposals retain exact
    offsets. Scope, namespace, authority, source partition, causal targets
    and CAS are never accepted from either format.
    """
    if proposal is None and not _INTENT.search(text):
        # An undated assertion with an explicit literal field boundary is an
        # observation, not an assertion that ingestion time is business time.
        literal_lhs = r"(?:[A-Za-z0-9][A-Za-z0-9_./:-]*[\u3400-\u9fff]+|[^。\n]+(?:的|'s\s+|’s\s+|::)[^。\n]+)"
        if re.fullmatch(literal_lhs + r"(?:改为|是|为|\s+is\s+|\s+changed\s+to\s+|\s*=\s*).+", text) is None:
            return None
    unresolved = dict(schema=KEY, state="unresolved", user_digest=sha256(user.encode()).hexdigest())
    try:
        proposed = proposal if proposal is not None else _compatible_proposal(user, text)
        provenance = proposed
        if type(proposed) is dict and "schema" in proposed:
            proposed = _compile_literal_proposal(user, proposed, unresolved["user_digest"])
            # The accepted new DTO is flat, with immutable string/null values.
            # Retain it for recertification against the exact original parent.
            provenance = dict(provenance)
        common = {"source_digest", "value", "valid_from", "valid_until", "operation"}
        if (type(proposed) is not dict or set(proposed) not in (common | {"subject", "attribute"}, common | {"slot"})
                or proposed["source_digest"] != unresolved["user_digest"]):
            raise ValueError("chat_proposal_invalid")
        value = _literal(user, proposed["value"])
        if "slot" in proposed:
            _literal(user, proposed["slot"])
            s = a = proposed["slot"]["span"]
        else:
            subject, attribute = [_literal(user, proposed[key]) for key in ("subject", "attribute")]
            if subject.strip('\"“”').casefold() in _PRONOUNS or attribute.casefold() in _PRONOUNS:
                raise ValueError("chat_identity_unresolved")
            s, a = proposed["subject"]["span"], proposed["attribute"]["span"]
            if not s[1] <= a[0] < a[1]:
                raise ValueError("chat_relation_order_invalid")
        start = _date(_literal(user, proposed["valid_from"]))
        until = _date(_literal(user, proposed["valid_until"])) if proposed["valid_until"] is not None else None
        _business_date_role(user, proposed["valid_from"])
        if proposed["valid_until"] is not None:
            _business_date_role(user, proposed["valid_until"], upper=True)
        if until is not None and parse_instant(until) <= parse_instant(start):
            raise ValueError("chat_interval_invalid")
        v = proposed["value"]["span"]
        if not a[1] <= v[0]:
            raise ValueError("chat_relation_order_invalid")
        clause = _assertion_clause(user, proposed)
        dated_start, dated_until, dated_operation, body = _dated_body(clause)
        if (body != user[s[0]:v[1]].strip()
                or dated_start != proposed["valid_from"]["text"]
                or dated_until != (proposed["valid_until"]["text"] if proposed["valid_until"] else None)):
            raise ValueError("chat_statement_role_ungrounded")
        lhs, _, literal_value = _relation(body)
        if lhs != user[s[0]:a[1]].strip() or literal_value != value:
            raise ValueError("chat_slot_ungrounded")
        # Always canonicalize the original full LHS, never model segmentation.
        canonical = _compatible_proposal(user, clause)
        fact = _fact_for_slot(lhs)
        if "slot" not in proposed and fact["namespace"] == NAMESPACE:
            if any(proposed[k] != canonical[k] for k in ("subject", "attribute")):
                raise ValueError("chat_subject_boundary_unresolved")
        elif "slot" not in proposed and user[s[1]:a[0]].strip():
            raise ValueError("chat_subject_boundary_unresolved")
        connective = user[a[1]:v[0]].strip()
        if re.fullmatch(r"改为|是|为|=|changed\s+to|is", connective, re.I) is None:
            raise ValueError("chat_relation_unresolved")
        operation = proposed["operation"]
        if operation not in {"claim", "transition", "correction", "retraction"}:
            raise ValueError("chat_operation_invalid")
        if operation == "transition" and not re.fullmatch(r"改为|changed\s+to", connective, re.I):
            raise ValueError("chat_transition_ungrounded")
        expected_operation = dated_operation
        if expected_operation == "claim" and re.fullmatch(r"改为|changed\s+to", connective, re.I):
            expected_operation = "transition"
        if operation != expected_operation:
            raise ValueError("chat_operation_ungrounded")
        if operation in {"correction", "retraction"}:
            marker = r"更正|纠正|\bcorrection\b" if operation == "correction" else r"撤回|\bretraction\b"
            if until is None or re.search(marker, clause, re.I) is None:
                raise ValueError("chat_local_operation_unbounded")
        return dict(schema=KEY, state="resolved", user_digest=unresolved["user_digest"],
                    fact=fact,
                    value=value, operation=operation,
                    interval=dict(valid_from=start, valid_until=until,
                                  until_kind="bounded" if until else "unbounded", precision="second"),
                    proposal=provenance, quote=clause)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError):
        return unresolved


def _partition_access(scope, source_id, *, admit_evidence=lambda record: False):
    """Stable owner identity; the default callback authorizes no evidence.

    Disabled readers use this only to locate journals for subtraction. It is
    never an enablement switch or a way to certify a family without access_for.
    """
    if not scope.tenant_id or not scope.agent_id or not scope.user_id or source_id not in CHANNELS:
        raise PermissionError("ordinary_chat_temporal_identity_unresolved")
    return TemporalFactAccess(scope=scope, source_id=source_id, authority="ordinary-chat-owner.v1",
                              source=source_id + ".chat_temporal", admit_evidence=admit_evidence)


def access_for(api, scope, source_id, *, sqlite=None, deadline=None):
    if not getattr(api, "_ordinary_chat_temporal", False):
        raise PermissionError("ordinary_chat_temporal_unbound")

    def admit(record):
        def check(sqlite):
            if deadline and perf_counter() >= float(deadline):
                raise TimeoutError("chat_temporal_deadline")
            meta = business_metadata(record.meta)
            if (record.kind != "memory" or record.status != "active" or record.scope != scope
                    or record.source_id != source_id or record.source != source_id + ".l1"
                    or meta.get("capture_origin") != "l1_extract" or meta.get("memory_layer") != "l1"
                    or meta.get("runtime_channel") != source_id):
                return False
            binding = meta.get("source_event_binding")
            parent = validate_l1_source_event(record, binding, get_exact=sqlite.get_by_exact_ref)
            if not eligible_parent(api, parent) or record.content.get("text") != record.summary:
                return False
            from eimemory.knowledge.safety import evaluate_knowledge_safety
            if not evaluate_knowledge_safety(parent, task="recall", registry=api.source_registry)["recall_allowed"]:
                return False
            grounding = meta.get(KEY)
            if type(grounding) is not dict or grounding.get("state") != "resolved":
                return False
            return grounding == ground_assertion(source_user(parent), record.summary, grounding.get("proposal"))
        return check(sqlite) if sqlite is not None else api.store.read_consistent(check)

    return _partition_access(scope, source_id, admit_evidence=admit)


def _family(sqlite, api, access, fact, *, deadline=None):
    from eimemory.storage.runtime_store import _certify_temporal_family
    family_id = family_id_for(access, fact)
    family, complete = sqlite.temporal_fact_family(family_id=family_id, scope=access.scope,
                                                  source_id=access.source_id, max_events=MAX_EVENTS)
    if not complete:
        raise ValueError("temporal_family_incomplete")
    pinned_access = access_for(api, access.scope, access.source_id, sqlite=sqlite, deadline=deadline)
    _certify_temporal_family(sqlite, family, access=pinned_access, family_id=family_id,
                             fact=fact, source_registry=api.source_registry)
    return family


def _observation_clause(user, text):
    if user.count(text) != 1:
        return None
    at = user.index(text)
    separators = list(re.finditer(r"[\n。；;!?？]|\.(?=\s|$)", user))
    left = max([s.end() for s in separators if s.end() <= at] or [0])
    right = min([s.end() for s in separators if s.start() >= at + len(text)] or [len(user)])
    return user[left:right].strip()


def ground_observation(api, parent, text, proposal=None, *, sqlite=None):
    """Keep undated observations of an already managed slot out of legacy updates.

    Unrelated undated facts keep their ordinary route. The family lookup is
    partitioned by the trusted parent, and incomplete families fail closed.
    """
    user = source_user(parent)
    grounding = ground_assertion(user, text, proposal)
    if grounding is not None:
        return grounding
    unresolved = dict(schema=KEY, state="unresolved", user_digest=sha256(user.encode()).hexdigest())
    clause = _observation_clause(user, text)
    if clause is None:
        # Dropping a date in an ungrounded model paraphrase cannot bypass this
        # route. Exact unrelated clauses in a mixed turn still take legacy L1.
        return unresolved if _INTENT.search(user) else None
    if _INTENT.search(clause):
        return ground_assertion(user, clause) or unresolved
    try:
        lhs, _, _ = _relation(text.strip().rstrip("。."))
        fact = _fact_for_slot(lhs)
        access = access_for(api, parent.scope, parent.source_id)

        def managed(sqlite):
            family, complete = sqlite.temporal_fact_family(
                family_id=family_id_for(access, fact), scope=parent.scope,
                source_id=parent.source_id, max_events=MAX_EVENTS)
            return bool(family) or not complete

        is_managed = managed(sqlite) if sqlite is not None else api.store.read_consistent(managed)
        if is_managed:
            return unresolved
    except (ValueError, TypeError, KeyError):
        pass
    return None


def exclude_managed_candidates(api, request, items):
    """Subtractive boundary for upgraded readers with temporal recall disabled.

    Active L1 bodies require the certified resolver just as their inactive
    journals do. A marker can only withhold, never grant eligibility; stripped
    markers are checked against the exact journal and original source. This
    does not make an older binary safe on a store containing active bodies.
    No status, source, canonical digest, or persisted history is rewritten.
    """
    deadline = request.task_context_dict().get("_recall_deadline_monotonic")
    items = list(items)
    keep, inspect = set(), []
    for index, item in enumerate(items):
        if KEY in business_metadata(item.meta):
            continue
        if (item.kind == "memory" and item.source_id in CHANNELS
                and item.source == item.source_id + ".l1"):
            inspect.append((index, item))
        else:
            keep.add(index)
    if inspect:
        def read(sqlite):
            for index, item in inspect:
                try:
                    if deadline and perf_counter() >= float(deadline):
                        break
                    access = None
                    if item.scope.tenant_id and item.scope.agent_id and item.scope.user_id:
                        access = _partition_access(item.scope, item.source_id)
                        journal = sqlite.get_by_exact_ref(
                            revision_id_for(access, "ordinary-chat:" + item.record_id),
                            scope=item.scope, source_id=item.source_id, fail_on_unavailable=True,
                            preserve_unknown_occurred_at=True)
                        if journal is not None:
                            continue
                    binding = business_metadata(item.meta).get("source_event_binding")
                    # Pre-integration unbound L1 keeps its legacy path. A bound
                    # but invalid source cannot certify it as unrelated evidence.
                    if binding is not None:
                        parent = validate_l1_source_event(item, binding, get_exact=sqlite.get_by_exact_ref)
                        if _chat_parent(parent):
                            user = source_user(parent)
                            clause = _observation_clause(user, item.summary)
                            if _INTENT.search(user if clause is None else clause):
                                continue
                            # Unmarked pre-integration undated facts are not
                            # managed merely for having an explicit slot. Only
                            # an existing exact family reserves that slot. An
                            # unrelated dated clause in the same L0 does not.
                            if clause is not None and access is not None:
                                try:
                                    lhs, _, _ = _relation(item.summary.strip().rstrip("。."))
                                    fact = _fact_for_slot(lhs)
                                except ValueError:
                                    fact = None
                                if fact is not None:
                                    family, complete = sqlite.temporal_fact_family(
                                        family_id=family_id_for(access, fact), scope=item.scope,
                                        source_id=item.source_id, max_events=MAX_EVENTS)
                                    if family or not complete:
                                        continue
                    keep.add(index)
                except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
                    continue
        from eimemory.storage.recall_deadline import recall_read_scope
        try:
            with recall_read_scope(api.store, {"_recall_collection_deadline_monotonic": deadline}):
                api.store.read_consistent(read)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
            pass  # Unclassified chat evidence stays withheld; other routes survive.
    return [item for index, item in enumerate(items) if index in keep]


def _require_append_order(incoming, prior, binding, request, target, *, get_exact):
    """Authorize interval masking, never physical source retirement.

    The global L1 guard still governs destructive writes. Here equal *known*
    source clocks can coexist only with a grounded, strictly advancing business
    start. All source material is revalidated inside the journal transaction;
    the journal itself supplies exact-family targets, evidence digests and CAS.
    """
    parent = validate_l1_source_event(incoming, binding, get_exact=get_exact)
    old_binding = business_metadata(prior.meta).get("source_event_binding")
    old_parent = validate_l1_source_event(prior, old_binding, get_exact=get_exact)
    # Source clocks retain their native precision; business intervals use the
    # kernel's narrower second-precision parser. No source timestamp is rewritten.
    source_at = datetime.fromisoformat(parent.time.occurred_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    old_source_at = datetime.fromisoformat(old_parent.time.occurred_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    if source_at != old_source_at:
        require_l1_supersession_order(incoming, prior, binding, get_exact=get_exact)
        return
    old = temporal_payload(target)["request"]
    grounded = business_metadata(incoming.meta)[KEY]
    if (request["fact"] != old["fact"] or grounded["fact"] != request["fact"]
            or grounded["interval"] != request["interval"]
            or request["operation"] not in {"transition", "correction", "retraction"}
            or parse_instant(request["interval"]["valid_from"]) <= parse_instant(old["interval"]["valid_from"])
            or request["operation"] in {"correction", "retraction"} and (
                grounded["operation"] != request["operation"] or request["interval"]["until_kind"] != "bounded")):
        raise L1SourceOrderBlocked("l1_source_order_unknown", target=prior)


def persist_observation(api, parent, atom, grounding, *, record_id, receipt):
    """Write guarded L1 evidence, then CAS-append its protocol operation.

    A crash between these commits leaves managed evidence hidden from upgraded
    MemoryAPI readers until retry. Older readers of these active bodies require
    a separate activation/rollback boundary. Completion is last.
    """
    scope, channel = parent.scope, parent.source_id
    binding = l1_source_event_binding(parent)
    text = grounding.get("quote", atom.text)
    access = access_for(api, scope, channel) if scope.tenant_id and scope.agent_id and scope.user_id else None
    if access is None:
        # An admitted L0 can be retained without manufacturing a known owner.
        grounding = dict(schema=KEY, state="unresolved", user_digest=grounding["user_digest"])
    meta = {"runtime_channel": channel, "authoritative": grounding["state"] == "resolved", "capture_origin": "l1_extract",
            "memory_layer": "l1", "semantic_key": "chat-observation:" + record_id, "l1_type": "fact",
            "source_message_ids": [parent.record_id], "conflict_action": "store", "supersedes": [],
            "l1_write_receipt": receipt, KEY: grounding}
    existing = api.store.get_by_exact_ref(record_id, scope=scope, source_id=channel)
    if existing is not None:
        actual = business_metadata(existing.meta)
        if (existing.summary != text or existing.content.get("text") != text
                or existing.status != "active" or existing.source != channel + ".l1"
                or any(actual.get(key) != value for key, value in meta.items())
                or actual.get("source_event_binding") != binding):
            raise L1SourceOrderBlocked("l1_existing_atom_conflict")
        child = existing
    else:
        child = api.ingest(record_id=record_id, text=text, memory_type="fact", title=text[:72],
            scope={**asdict(scope), "preserve_scope": True}, source=channel + ".l1", source_id=channel,
            force_capture=True, evidence=[parent.record_id],
            links=[LinkRef(relation="derived_from", target_kind="memory", target_id=parent.record_id)],
            meta=meta, source_event_binding=binding)
    if grounding["state"] != "resolved":
        return child
    fact, interval = grounding["fact"], grounding["interval"]
    key = "ordinary-chat:" + child.record_id
    revision_id = revision_id_for(access, key)

    def prepare(sqlite):
        require_l1_source_parent_current(parent, binding)
        family = _family(sqlite, api, access, fact)
        prior = next((r for r in family if r.record_id == revision_id), None)
        if prior is not None:
            request = temporal_payload(prior)["request"]
            if (request["fact"] != fact or request["interval"] != interval
                    or request["evidence"] != [child.record_id]
                    or request["value"] != (None if grounding["operation"] == "retraction" else grounding["value"])):
                raise ValueError("idempotency_conflict")
            return request, []
        at = parse_instant(interval["valid_from"])
        operation, targets = grounding["operation"], []
        if operation == "claim" and interval["until_kind"] == "unbounded":
            # An explicitly later open-ended state of the same literal single
            # field establishes a boundary even when it says "is", not "changed".
            # Equal business dates never acquire an invented statement ordering.
            if any(temporal_payload(r)["request"]["operation"] != "retraction"
                   and covers(temporal_payload(r)["request"]["interval"], at)
                   and parse_instant(temporal_payload(r)["request"]["interval"]["valid_from"]) < at
                   for r in family):
                operation = "transition"
        for previous in family:
            request = temporal_payload(previous)["request"]
            if request["operation"] == "retraction":
                continue
            old = request["interval"]
            overlaps = (old["valid_from"] is not None and old["until_kind"] != "unknown"
                        and (interval["valid_until"] is None or parse_instant(old["valid_from"]) < parse_instant(interval["valid_until"])))
            overlaps = overlaps and (old["valid_until"] is None or at < parse_instant(old["valid_until"]))
            eligible = operation in {"correction", "retraction"} and overlaps
            if operation == "transition":
                eligible = covers(old, at) and parse_instant(old["valid_from"]) < at
            if operation == "retraction":
                eligible = eligible and request["value"] == grounding["value"]
            if eligible:
                targets.append(previous)
        if operation != "claim" and not targets:
            if operation in {"correction", "retraction"}:
                raise ValueError("chat_local_target_unavailable")
            operation = "claim"  # first observed transition has no invented predecessor
        return dict(operation=operation, fact=fact,
                    value=None if operation == "retraction" else grounding["value"],
                    interval=interval, targets=[r.record_id for r in targets],
                    expected_revision=family[-1].record_id if family else None,
                    evidence=[child.record_id], idempotency_key=key), targets

    request, targets = api.store.read_consistent(prepare)

    def write_admit(record):
        if access.admit_evidence(record) is not True:
            return False
        if record.record_id == child.record_id:
            def causal(sqlite):
                validate_l1_source_event(record, binding, get_exact=sqlite.get_by_exact_ref)
                for target in targets:
                    current = sqlite.get_by_exact_ref(target.record_id, scope=scope, source_id=channel,
                                                      fail_on_unavailable=True, preserve_unknown_occurred_at=True)
                    if current is None or current.to_dict() != target.to_dict():
                        raise ValueError("revision_conflict")
                    for evidence_id in current.evidence:
                        previous = sqlite.get_by_exact_ref(evidence_id, scope=scope, source_id=channel)
                        if previous is None:
                            raise L1SourceOrderBlocked("l1_source_parent_unavailable")
                        _require_append_order(record, previous, binding, request, current,
                                              get_exact=sqlite.get_by_exact_ref)
                return True
            return api.store.read_consistent(causal)
        return True

    api.store.apply_temporal_facts(operations=[request], access=replace(access, admit_evidence=write_admit),
                                   enabled=True, source_registry=api.source_registry)
    return child


def _query_envelope(text):
    """Question syntax only: no domain nouns and no identity normalization."""
    text = re.sub(r"(?:截至|截止|在|于|as\s+of\s+|on\s+|at\s+)(" + _DATE + r")(?![\dA-Za-z])", "", text, flags=re.I)
    text = re.sub(r"(" + _DATE + r")(?:时|的时候)", "", text)
    return re.fullmatch(
        r"(?:[\s?？,，。.:：]|现在|当前|目前|的|请问|查询|是|为|谁|什么|多少|哪个|哪里|如何|何时|怎么样|"
        r"\b(?:what|which|who|where|when|how|is|are|was|were|does|do|did|the|a|an|of|for|"
        r"now|current|currently|please|tell|me)\b)*", text, re.I) is not None


# The matcher reads untrusted metadata before family certification. Bound both
# its input and its actual state transitions, including envelope checks.
_QUERY_MAX_CHARS = 4096
_QUERY_MAX_WORK = 262144
_QUERY_MATCH_SECONDS = 0.10


def _query_literal(text, *, slot=False):
    """Compile literal characters and syntax edges, never an ambiguous regex."""
    parts, quoted = [], False
    for index, char in enumerate(text):
        if char in '\"“”':
            quoted = not quoted
        parts.append(("space", "") if char.isspace() and not quoted else ("literal", char))
        if (slot and not quoted and index + 1 < len(text)
                and "\u3400" <= text[index + 1] <= "\u9fff"):
            parts.append(("marker", ""))
    return parts


def _query_gap_ends(query, at):
    if at >= len(query):
        return ()
    ends = [at + 1] if query[at].isspace() else []
    for literal in ("的", "::", "现在", "当前", "目前"):
        if query.startswith(literal, at):
            ends.append(at + len(literal))
    for literal in ("'s", "’s"):
        end = at + len(literal)
        if query.startswith(literal, at) and end < len(query) and query[end].isspace():
            ends.append(end + 1)
    for word in ("current", "currently", "now", "the"):
        end = at + len(word)
        if (query[at:end].casefold() == word
                and (not at or not (query[at - 1].isalnum() or query[at - 1] == "_"))
                and (end == len(query) or not (query[end].isalnum() or query[end] == "_"))):
            ends.append(end)
    return ends


def _query_matches(query, fact, *, deadline=None, max_work=_QUERY_MAX_WORK):
    """Bounded NFA: each (literal instruction, original query offset) once.

    Optional current syntax never removes a character from the fact. The same
    visited state merges alternative allocations of repeated marker words;
    regex backtracking and input-dependent recursion are absent.
    """
    subject, attribute = fact.get("entity_id"), fact.get("attribute")
    if (type(query) is not str or len(query) > _QUERY_MAX_CHARS
            or type(subject) is not str or not 1 <= len(subject) <= 512
            or type(attribute) is not str or not 1 <= len(attribute) <= 512):
        return False
    stop = perf_counter() + _QUERY_MATCH_SECONDS
    if deadline is not None:
        stop = min(stop, float(deadline))
    remaining = max_work

    def spend(work=1):
        nonlocal remaining
        remaining -= work
        if remaining < 0 or perf_counter() >= stop:
            raise TimeoutError("chat_query_match_bound")

    is_slot = fact.get("namespace") == SLOT_NAMESPACE
    program = _query_literal(subject, slot=is_slot)
    if not is_slot:
        if attribute[:1].isascii():
            program.append(("gap_once", ""))
        program.append(("gap", ""))
        program.extend(_query_literal(attribute, slot=True))
    first, last = subject[0], subject[-1] if is_slot else attribute[-1]
    ascii_first = first.isascii() and (first.isalnum() or first == "_")
    ascii_last = last.isascii() and (last.isalnum() or last == "_")
    visited, pending, suffixes = set(), [], {}

    def add(pc, at):
        state = (pc, at)
        if state not in visited:
            spend()
            visited.add(state)
            pending.append(state)

    try:
        for at, char in enumerate(query):
            spend()
            if char != first:
                continue
            if ascii_first and at and query[at - 1].isascii() and (query[at - 1].isalnum() or query[at - 1] == "_"):
                continue
            spend(at + 1)
            if _query_envelope(query[:at]):
                add(0, at)
        while pending:
            spend()
            pc, at = pending.pop()
            if pc == len(program):
                if (ascii_last and at < len(query) and query[at].isascii()
                        and (query[at].isalnum() or query[at] == "_")):
                    continue
                if at not in suffixes:
                    spend(len(query) - at + 1)
                    suffixes[at] = _query_envelope(query[at:])
                if suffixes[at]:
                    return True
                continue
            kind, literal = program[pc]
            if kind == "literal":
                if query.startswith(literal, at):
                    add(pc + 1, at + 1)
            elif kind == "space":
                if at < len(query) and query[at].isspace():
                    add(pc, at + 1)
                    add(pc + 1, at + 1)
            elif kind == "marker":
                add(pc + 1, at)
                for marker in ("现在的", "当前的", "目前的"):
                    if query.startswith(marker, at):
                        add(pc + 1, at + len(marker))
            else:
                if kind == "gap":
                    add(pc + 1, at)
                for end in _query_gap_ends(query, at):
                    add(pc if kind == "gap" else pc + 1, end)
        return False
    except TimeoutError:
        return False


@dataclass(frozen=True, slots=True)
class TemporalMatch:
    """Internal snapshot proof, never recovered from metadata or caller flags."""

    request: object = field(repr=False, compare=False)
    query: str
    record_ref: tuple[str, ...]
    record_digest: str
    valid_at: str
    interval: tuple[tuple[str, str | None], ...]
    fact: tuple[str, str, str]
    family_id: str
    revision_id: str
    head_revision: str
    quote: str
    quote_digest: str

    def supports(self, request, *, query, record, text):
        # Request identity prevents replay even for two equal concurrent
        # requests. Full references/digests prevent sibling/pool borrowing.
        return (request is self.request and query == self.query == request.query
                and record.status == "active" and exact_ref(record) == self.record_ref
                and authority_digest(record) == self.record_digest
                and self.quote == record.summary == record.content.get("text")
                and sha256(self.quote.encode()).hexdigest() == self.quote_digest
                and self.quote in text
                and covers(dict(self.interval), parse_instant(self.valid_at)))


@dataclass(frozen=True, slots=True)
class TemporalResolution:
    items: tuple
    matches: tuple[TemporalMatch, ...]

    def supports(self, request, *, query, record, text):
        return any(match.supports(request, query=query, record=record, text=text)
                   for match in self.matches)


def _original_assertions(sqlite, api, scope, source_id, facts, *, deadline):
    """Recertify source-only contradictions, including omitted/invalid DTOs.

    These originals can only subtract answer evidence. They never authorize a
    value, repair model fields, or append a temporal operation. Paging reuses
    the canonical store's exact-owner hydration and digest checks; unrelated
    history does not consume a fixed family count. Incomplete reads time out
    closed under the same recall snapshot and deadline.
    """
    from eimemory.knowledge.safety import evaluate_knowledge_safety

    def key(fact):
        return fact["namespace"], fact["entity_id"], fact["attribute"]

    wanted = {key(fact): fact for fact in facts}
    assertions = {identity: [] for identity in wanted}
    offset = 0
    while True:
        if perf_counter() >= deadline:
            raise TimeoutError("chat_source_recertification_deadline")
        page = sqlite.list_records_by_sources_exact_scope(
            scope=scope, sources=[source_id + ".memory"], kinds=["memory"],
            meta_filters={"memory_layer": "l0", "capture_origin": "turn_sync",
                          "runtime_channel": source_id}, limit=128, offset=offset)
        for parent in page:
            if perf_counter() >= deadline:
                raise TimeoutError("chat_source_recertification_deadline")
            if parent.scope != scope or parent.source_id != source_id or not eligible_parent(api, parent):
                continue
            try:
                require_l1_source_parent_current(parent, l1_source_event_binding(parent))
            except L1SourceOrderBlocked:
                continue
            if not evaluate_knowledge_safety(parent, task="recall", registry=api.source_registry)["recall_allowed"]:
                continue
            try:
                user = source_user(parent)
            except ValueError:
                # A truncated/unparseable role envelope establishes no original
                # assertion and cannot suppress an unrelated certified family.
                continue
            # A canonical literal identity must occur verbatim in its source.
            # This is a cost filter, not an identity/alias grant.
            if not any(fact["entity_id"] in user and (
                    fact["namespace"] == SLOT_NAMESPACE or fact["attribute"] in user)
                       for fact in wanted.values()):
                continue
            # The existing grounding grammar is clause-local. Keep punctuation
            # (including question marks) so modality cannot disappear on split.
            start = 0
            ends = [m.end() for m in re.finditer(r"[\n。；;!?？]|\.(?=\s|$)", user)]
            for end in [*ends, len(user)]:
                if perf_counter() >= deadline:
                    raise TimeoutError("chat_source_recertification_deadline")
                clause, start = user[start:end].strip(), end
                if not clause:
                    continue
                # Parse the exact original clause locally: another occurrence
                # of its slot elsewhere in the same USER is not an alias or
                # an ambiguous character offset. Keep the complete original
                # digest separately; no model proposal/digest is rewritten.
                grounded = ground_assertion(clause, clause)
                if (grounded is None or grounded.get("state") != "resolved"
                        or grounded["operation"] not in {"claim", "transition", "correction", "retraction"}):
                    continue
                identity = key(grounded["fact"])
                if identity in assertions:
                    assertions[identity].append((parent.record_id, sha256(user.encode()).hexdigest(), grounded))
        if len(page) < 128:
            return assertions
        offset += len(page)


def _source_contradicts(grounded, originals, valid_at, certified):
    # Only explicit business time orders these assertions. Capture order cannot
    # turn a subsequently recalled historical state into the current state.
    # Equal-date contradictory originals are unresolved too: neither successful
    # extraction nor capture order is authority to choose one value as certain.
    start = parse_instant(grounded["interval"]["valid_from"])
    for parent_id, user_digest, original in originals:
        if ((parent_id, user_digest, original["quote"]) in certified
                or original["fact"] != grounded["fact"]
                or not covers(original["interval"], valid_at)):
            continue
        operation = original["operation"]
        if operation in {"correction", "retraction"}:
            # Local operations address the overlapping interval, including a
            # retrospective interval that begins before the selected claim.
            # A withdrawal disputes only its exact value; its raw wording is
            # never evidence for that value or for a replacement. The grammar
            # requires bounded operations, and this branch retains that limit.
            if original["interval"]["until_kind"] != "bounded":
                continue
            same_value = original["value"] == grounded["value"]
            if same_value == (operation == "retraction"):
                return True
        elif (operation in {"claim", "transition"}
                and original["value"] != grounded["value"]
                and parse_instant(original["interval"]["valid_from"]) >= start):
            return True
    return False


def resolve_candidates(api, request, items, *, expand, include_admission=False):
    """Only replace managed L1 candidates with certified, active source evidence.

    Added family members get no borrowed ranking/vector/fragment proof. Internal
    receipts preserve their own certified query match through native relevance;
    identity, lane, answer-shape and final evidence gates remain in force.
    A final pass uses expand=False and can only subtract after authority races.
    """
    scope = request.scope.to_scope_ref()
    explicit, usable_time = query_time(request.query)
    valid_at = parse_instant(explicit) if explicit else datetime.now(timezone.utc).replace(microsecond=0)
    deadline = request.task_context_dict().get("_recall_deadline_monotonic")
    ordinary = []  # only evidence positively classified as unrelated legacy
    try:
        def read(sqlite):
            families = {}
            for item in items:
                if deadline and perf_counter() >= float(deadline):
                    raise ValueError("chat_temporal_deadline")
                grounding = business_metadata(item.meta).get(KEY)
                # A stripped/changed child marker cannot turn journal evidence
                # into unqualified legacy truth. The deterministic protocol ID
                # is checked in the same pinned snapshot, never by a new index.
                try:
                    if (grounding is None and item.scope == scope and item.source_id in CHANNELS
                            and item.source == item.source_id + ".l1" and item.kind == "memory"):
                        access = access_for(api, scope, item.source_id, sqlite=sqlite)
                        revision = sqlite.get_by_exact_ref(
                            revision_id_for(access, "ordinary-chat:" + item.record_id),
                            scope=scope, source_id=item.source_id, fail_on_unavailable=True,
                            preserve_unknown_occurred_at=True)
                        if revision is not None:
                            grounding = dict(state="resolved", fact=temporal_payload(revision)["request"]["fact"])
                        else:
                            # Pre-integration chat evidence is not migrated or
                            # reinterpreted as current truth. If it has this route's
                            # literal dated shape, only a certified journal may make
                            # it applicable; a missing journal stays conservative.
                            meta = business_metadata(item.meta)
                            binding = meta.get("source_event_binding")
                            if type(binding) is dict:
                                parent = validate_l1_source_event(item, binding, get_exact=sqlite.get_by_exact_ref)
                                if eligible_parent(api, parent):
                                    grounding = ground_observation(api, parent, item.summary, sqlite=sqlite)
                except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
                    continue  # this unknown item cannot suppress unrelated legacy facts
                if grounding is None:
                    ordinary.append(item)
                    continue
                fact = grounding.get("fact") if type(grounding) is dict else None
                if (not usable_time or type(grounding) is not dict or grounding.get("state") != "resolved"
                        or type(fact) is not dict or fact.get("namespace") not in NAMESPACES
                        or type(fact.get("entity_id")) is not str or type(fact.get("attribute")) is not str
                        or not fact["entity_id"] or not fact["attribute"]
                        or item.scope != scope or request.source_ids is not None and item.source_id not in request.source_ids
                        or request.target_source_id is not None and item.source_id != request.target_source_id
                        or not _query_matches(request.query, fact, deadline=deadline)):
                    continue
                family_key = (item.source_id, fact["namespace"], fact["entity_id"], fact["attribute"])
                families[family_key] = fact
            if len(families) > 16:
                return ordinary, ()
            chosen, matches, originals = [], [], {}
            source_deadline = min(float(deadline), perf_counter() + 0.5) if deadline else perf_counter() + 0.5
            for (source_id, _, _, _), fact in families.items():
                if deadline and perf_counter() >= float(deadline):
                    raise ValueError("chat_temporal_deadline")
                family_chosen, family_matches = [], []
                try:
                    access = access_for(api, scope, source_id)
                    family = _family(sqlite, api, access, fact, deadline=deadline)
                    resolved = resolve_family(family, at=valid_at, limit=MAX_EVENTS)
                    if resolved["status"] != "resolved" or resolved["truncated"]:
                        continue
                    if source_id not in originals:
                        from eimemory.storage.recall_deadline import recall_read_scope
                        with recall_read_scope(api.store, {"_recall_collection_deadline_monotonic": source_deadline},
                                               sqlite=sqlite, lock_held=True):
                            originals[source_id] = _original_assertions(sqlite, api, scope, source_id,
                                [value for identity, value in families.items() if identity[0] == source_id],
                                deadline=source_deadline)
                    source_assertions = originals[source_id][(fact["namespace"], fact["entity_id"], fact["attribute"])]
                    # Certified operations already account for corrections and
                    # retractions. Only the exact unrepresented source clause
                    # is an uncertainty; a sibling clause in that L0 is separate.
                    certified = set()
                    for operation in family:
                        for evidence_id in operation.evidence:
                            evidence = sqlite.get_by_exact_ref(evidence_id, scope=scope, source_id=source_id,
                                                               fail_on_unavailable=True)
                            metadata = business_metadata(evidence.meta)
                            assertion = metadata[KEY]
                            certified.add((metadata["source_event_binding"]["record_id"],
                                           assertion["user_digest"], assertion["quote"]))
                    by_id = {r.record_id: r for r in family}
                    for selected in resolved["items"]:
                        journal = by_id[selected["revision_id"]]
                        for evidence_id in journal.evidence:
                            record = sqlite.get_by_exact_ref(evidence_id, scope=scope, source_id=source_id,
                                                             fail_on_unavailable=True)
                            if record is None or record.status != "active":
                                raise ValueError("chat_temporal_evidence_unavailable")
                            # _family has validated this exact grounding against
                            # the original source, not just a temporal tag. Bind
                            # the chosen journal fact/value to that same quote.
                            grounded = business_metadata(record.meta)[KEY]
                            if grounded["fact"] != fact or grounded["value"] != selected["value"]:
                                raise ValueError("chat_temporal_slot_mismatch")
                            if _source_contradicts(grounded, source_assertions, valid_at, certified):
                                raise ValueError("chat_newer_original_unresolved")
                            if expand:
                                family_chosen.append(record)
                                if include_admission:
                                    family_matches.append(TemporalMatch(
                                        request=request, query=request.query, record_ref=exact_ref(record),
                                        record_digest=authority_digest(record), valid_at=valid_at.isoformat(),
                                        interval=tuple(selected["interval"].items()),
                                        fact=(fact["namespace"], fact["entity_id"], fact["attribute"]),
                                        family_id=family_id_for(access, fact), revision_id=journal.record_id,
                                        head_revision=resolved["head_revision"], quote=record.summary,
                                        quote_digest=sha256(record.summary.encode()).hexdigest()))
                            else:
                                family_chosen.extend(r for r in items if r.record_id == record.record_id
                                              and r.scope == scope and r.source_id == source_id
                                              and authority_digest(r) == authority_digest(record))
                except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
                    continue  # fail closed for this entire family
                chosen.extend(family_chosen)
                matches.extend(family_matches)
            seen = {(r.record_id, r.source_id, tuple(asdict(r.scope).values())) for r in ordinary}
            for record in chosen:
                key = (record.record_id, record.source_id, tuple(asdict(record.scope).values()))
                if key not in seen:
                    ordinary.append(record)
                    seen.add(key)
            return ordinary, tuple(matches)
        from eimemory.storage.recall_deadline import recall_read_scope
        with recall_read_scope(api.store, {"_recall_collection_deadline_monotonic": deadline}):
            selected, matches = api.store.read_consistent(read)
        if not expand:
            allowed = {(exact_ref(r), authority_digest(r)) for r in selected}
            return [r for r in items if (exact_ref(r), authority_digest(r)) in allowed]
        return TemporalResolution(tuple(selected), matches) if include_admission else selected
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
        # No all-version fallback, including on an unavailable family source.
        return TemporalResolution(tuple(ordinary), ()) if include_admission else ordinary
