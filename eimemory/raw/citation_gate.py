"""Opt-in, query-level lexical citation admission; never an answer entailment test.

Name/unknown-word ideas adapted from KRouter (MIT), commit
d6b096c4f055f6eebd680240d53418ccd125ee95, recall_index.py:
absent_names, unknown_content_terms. Copyright (c) 2026 KRouter.
License: KROUTER_NOTICE.txt. No upstream score thresholds or runtime imports.

Only a complete, request-authorized, stable corpus can establish absence.
Default RAW calls do not enter this module's collection or decision paths.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import wraps
from hashlib import sha256
import json
import re
from time import perf_counter, thread_time

from eimemory.contracts.recall_boundary import authority_digest, bounded_deadline, exact_ref, scope_tuple
from eimemory.identity import hongtu_query_scopes
from eimemory.models.records import ScopeRef, VALID_KINDS
from eimemory.models.source_partitions import normalize_source_ids
from .boundary import (_CURRENT, RawRecallUnavailable, raw_ref_allowed,
                       raw_request_boundary, require_raw_collection_complete)
from ._relevance import EN_FUNCTION, _quote_spans, latin_forms, normalize, phrase_present, query_content, tokens

POLICY_VERSION = "raw-citation-lexical-v2"
TOKENIZER_VERSION = "raw-relevance-nfkc-identifiers-v1"
MAX_ROWS = 500
MAX_SCOPES = 64
MAX_RECORD_BYTES = 262144
MAX_CORPUS_BYTES = 8388608
MAX_POINTER_BYTES = 4096
MAX_TEXT_CHARS = 1048576
MAX_QUERY_CHARS = 4096
MAX_QUERY_TERMS = 128
MAX_GATE_CPU_SECONDS = 0.5
MAX_SCOPE_COMPONENT_CHARS = 256
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN = re.compile(r"[a-z][a-z0-9_.'+-]*\Z")
_NAME = re.compile(r"(?<![\w'-])[A-Za-z][A-Za-z0-9_]*(?:[.'+-][A-Za-z0-9_]+)*(?![\w'-])")
# Only a narrow grammatical subject slot establishes a name role here.
# Polar questions and later predicate/prepositional values stay uncertain.
_OPEN_SUBJECT = re.compile(r"\s*(?:where|when|who)\s+(?:is|are|was|were)\s+(?:the\s+)?", re.I)
_CJK_OPEN = re.compile(r"哪里|何处|何时|何人|谁|什么|哪个|哪些|多少|几点|如何|怎样")
_CJK_POLAR = re.compile(r"吗|是否|能否|可否|能不能|可不可以|是不是")
_CALENDAR = frozenset("""january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday saturday sunday""".split())
# These filters have broader engine semantics than RAW's authority boundary.
# Until their exact SQL/payload semantics are implemented, never ignore them.
_UNSUPPORTED_FILTERS = ("allowed_sources", "blocked_sources", "allowed_memory_types", "organs",
                        "allowed_recall_lanes", "blocked_recall_lanes", "blocked_kinds",
                        "blocked_projection_types", "allowed_kinds", "permissions", "visibility")


def _digest(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CitationDecision:
    status: str
    reason: str
    query_digest: str = ""
    scope_digest: str = ""
    snapshot_digest: str = ""
    complete: bool = False
    corpus_records: int | None = None
    owner_scopes: tuple[tuple[str, str, str, str], ...] = ()
    source_ids: tuple[str, ...] | None = None
    kinds: tuple[str, ...] = ()
    matched_terms: tuple[str, ...] = ()
    missing_terms: tuple[str, ...] = ()
    candidate_source_digests: tuple[tuple[tuple[str, ...], str], ...] = ()
    policy_version: str = POLICY_VERSION
    tokenizer_version: str = TOKENIZER_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


class CitationUnavailable(RawRecallUnavailable):
    def __init__(self, reason: str, decision: CitationDecision | None = None):
        super().__init__("fact_citation_unavailable:" + reason, allow_recovery=False)
        self.decision = decision or CitationDecision("unavailable", reason)


class CitationResults(list):
    """Legacy list content plus explicit admission metadata, including for [].

    Consumers must preserve decision separately when serializing a plain list.
    Ordinary-chat wire integration is deliberately not enabled by this module.
    """
    def __init__(self, rows, decision: CitationDecision):
        super().__init__(rows)
        self.decision = decision


@dataclass(frozen=True)
class _Authority:
    scopes: tuple[tuple[str, str, str, str], ...]
    sources: tuple[str, ...] | None
    kinds: tuple[str, ...]
    deadline: float

    @property
    def digest(self):
        return _digest((self.scopes, self.sources, self.kinds, "active", "raw_ref_allowed-v1"))


@dataclass(frozen=True)
class CitationDocument:
    ref: tuple[str, ...]
    authority_sha256: str
    source_sha256: str
    text: str
    title: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class CitationSnapshot:
    scope_digest: str
    digest: str
    documents: tuple[CitationDocument, ...]


class _Budget:
    """Cooperative CPU/wall checks, including per-row storage hydration.

    Individual bounded JSON/token/payload operations are not preemptible.
    SQLite statements additionally inherit the existing progress deadline.
    """
    def __init__(self, deadline):
        self.deadline = deadline
        self.cpu_start = thread_time()

    def check(self):
        if perf_counter() >= self.deadline:
            raise CitationUnavailable("deadline")
        if thread_time() - self.cpu_start > MAX_GATE_CPU_SECONDS:
            raise CitationUnavailable("cpu_limit")


def _fact_authority_inputs(context, source_ids, outer):
    """Validate before I/O and intersect every source ceiling, never union it.

    Default RAW does not call this. Copy canonical fact filters into both the
    unchanged boundary and collector so their authority cannot diverge.
    """
    def sources(value):
        if value is not None and not isinstance(value, (list, tuple)):
            raise CitationUnavailable("invalid_authority_filter")
        try:
            return normalize_source_ids(value)
        except (TypeError, ValueError) as exc:
            raise CitationUnavailable("invalid_authority_filter") from exc

    ceilings = [sources(source_ids), sources(context.get("source_ids"))]
    if outer is not None and outer.source_ids is not None:
        ceilings.append(sources(tuple(outer.source_ids)))
    effective = None
    for ceiling in ceilings:
        if ceiling is not None:
            effective = set(ceiling) if effective is None else effective.intersection(ceiling)
    effective = None if effective is None else tuple(sorted(effective))
    context["source_ids"] = effective
    if "kinds" in context:
        raw = context["kinds"]
        if (not isinstance(raw, (list, tuple)) or not raw or len(raw) > len(VALID_KINDS)
                or any(not isinstance(kind, str) or len(kind) > 64
                       or kind.strip() not in {"raw_chunk", "memory"} for kind in raw)):
            raise CitationUnavailable("invalid_authority_filter")
        context["kinds"] = tuple(sorted({kind.strip() for kind in raw}))
    if outer is not None and outer.kinds is not None:
        # An inherited malformed boundary cannot safely be repaired locally.
        if any(not isinstance(kind, str) or kind not in VALID_KINDS for kind in outer.kinds):
            raise CitationUnavailable("invalid_authority_filter")
        if not set(context.get("kinds", ("raw_chunk", "memory"))).intersection(outer.kinds):
            raise CitationUnavailable("unsupported_authority_filter")
    return effective


def _authority(scope, context) -> _Authority:
    # This is the SAME request boundary that performs RAW's final hydration.
    # Its sources/kinds/exact owner already include enclosing restrictions.
    state = _CURRENT.get()
    if state is None:
        raise CitationUnavailable("snapshot_unavailable")
    if any(context.get(key) for key in _UNSUPPORTED_FILTERS):
        raise CitationUnavailable("unsupported_authority_filter")
    owners = set()
    for item in hongtu_query_scopes(scope):
        owners.add(scope_tuple(item))
        if item.user_id:
            owners.add(scope_tuple(ScopeRef(tenant_id=item.tenant_id, agent_id=item.agent_id,
                                           workspace_id=item.workspace_id, user_id="")))
    if state.exact_scope is not None:
        owners.intersection_update((state.exact_scope,))
    if len(owners) > MAX_SCOPES:
        raise CitationUnavailable("row_limit")
    kinds = {"raw_chunk", "memory"}
    if state.kinds is not None:
        kinds.intersection_update(state.kinds)
    sources = None if state.source_ids is None else tuple(sorted(state.source_ids))
    return _Authority(tuple(sorted(owners)), sources, tuple(sorted(kinds)), state.deadline)


def _snapshot(store, authority: _Authority, budget: _Budget):
    from .retrieval import _is_raw_candidate, _record_text, authoritative_raw_payload
    budget.check()
    collector = getattr(store, "read_raw_citation_records", None)
    if not callable(collector):
        raise CitationUnavailable("unsupported_store")
    records = collector(owner_scopes=authority.scopes, source_ids=authority.sources,
                        kinds=authority.kinds, deadline=authority.deadline,
                        check_budget=budget.check)
    try:
        require_raw_collection_complete(records)
    except RawRecallUnavailable as exc:
        raise CitationUnavailable("collection_incomplete") from exc
    if not isinstance(records, list) or len(records) > MAX_ROWS:
        raise CitationUnavailable("row_limit")
    documents, payloads, seen = [], {}, set()
    text_chars = 0
    for record in records:
        budget.check()
        ref = exact_ref(record)
        if (not ref[0] or not ref[-1] or ref in seen or ref[1:5] not in authority.scopes
                or (authority.sources is not None and ref[-1] not in authority.sources)
                or record.kind not in authority.kinds or record.status != "active"):
            raise CitationUnavailable("snapshot_unavailable")
        seen.add(ref)
        if not raw_ref_allowed(record) or (record.kind == "memory" and not _is_raw_candidate(record)):
            continue
        text = _record_text(record)
        aliases = tuple(record.aliases)
        text_chars += len(text) + len(record.title) + sum(map(len, aliases))
        if text_chars > MAX_TEXT_CHARS:
            raise CitationUnavailable("text_limit")
        canonical = authority_digest(record)
        documents.append(CitationDocument(ref, canonical, sha256(text.encode("utf-8")).hexdigest(),
                                          text, record.title, aliases))
        payloads[ref] = authoritative_raw_payload(record)
    documents.sort(key=lambda doc: doc.ref)
    snapshot = CitationSnapshot(authority.digest,
                                _digest((authority.digest, [(d.ref, d.authority_sha256) for d in documents])),
                                tuple(documents))
    budget.check()
    return snapshot, payloads


def _identity_literal(query, start, end):
    """Recognize only an open question's leading subject, never a value by case.

    This is an abstention rule, not a parser or a test of an answer's form.
    Quotation alone also does not make a proposed value an identity.
    """
    before = query[:start]
    if before and before[-1] in "\"'“「『":
        before = before[:-1]
    if _OPEN_SUBJECT.fullmatch(before):
        return True
    # Preserve leading quoted CJK subjects in open questions. No inference
    # about unquoted CJK roles, or about polar/permission questions.
    return (not before.strip() and bool(_CJK.search(query[start:end]))
            and bool(_CJK_OPEN.search(query[end:])) and not _CJK_POLAR.search(query))


def _named_literals(query, terms):
    import unicodedata
    original = unicodedata.normalize("NFKC", query)
    for match in _NAME.finditer(original):
        word = match.group()
        term = normalize(word)
        before = original[:match.start()].rstrip()
        if (term in terms and any(c.isupper() for c in word)
                and not (word.isupper() and len(word) <= 2)
                and before and before[-1] not in ".?!。？！:："
                and term not in EN_FUNCTION and term not in _CALENDAR
                and _identity_literal(original, match.start(), match.end())):
            yield term


def assess_citation(query: str, snapshot: CitationSnapshot, *, check_budget=lambda: None) -> CitationDecision:
    """Pure lexical gate over a complete snapshot supplied by the authority path.

    Non-rejection says related, never supported. Full-corpus union presence is
    intentionally conservative about aliases, cross-chunk and multi-hop text.
    """
    if len(query) > MAX_QUERY_CHARS:
        raise CitationUnavailable("query_limit")
    plan = query_content(query)
    terms = {term for term in plan.terms if not term.isdigit()}
    if len(terms) > MAX_QUERY_TERMS:
        raise CitationUnavailable("query_limit")
    available = set()
    normalized_documents = []
    for doc in snapshot.documents:
        check_budget()
        source = normalize(doc.title + "\n" + doc.text + "\n" + "\n".join(doc.aliases))
        normalized_documents.append(source)
        available.update(tokens(source))
    matched = {term for term in terms if latin_forms(term) & available}
    missing = terms - matched
    decision = CitationDecision("related", "related_only", _digest(query), snapshot.scope_digest,
                                snapshot.digest, True, len(snapshot.documents),
                                matched_terms=tuple(sorted(matched)), missing_terms=tuple(sorted(missing)))
    if not snapshot.documents:
        return replace(decision, status="reject", reason="empty_authorized_corpus")
    if any(term in missing for term in _named_literals(query, terms)):
        return replace(decision, status="reject", reason="missing_named_literal")
    normalized_query = normalize(query)
    identity_phrases = {normalized_query[start:end].strip()
                        for start, end in _quote_spans(normalized_query)
                        if _identity_literal(normalized_query, start, end)}
    for phrase in plan.phrases:
        check_budget()
        pt = tokens(phrase)
        # Only quotes in a recognized subject slot establish an identity.
        # Word presence anywhere preserves uncertainty about fragmented names.
        if (phrase in identity_phrases and 0 < len(phrase) <= 80 and 0 < len(pt) <= 6
                and not re.search(r"[\n?!。？！]", phrase)
                and not all(latin_forms(t) & available for t in pt)
                and not any(phrase_present(phrase, source) for source in normalized_documents)):
            return replace(decision, status="reject", reason="missing_quoted_literal")
    latin = {term for term in terms if _LATIN.fullmatch(term)}
    unknown = {term for term in latin & missing if len(term) >= 4}
    if not _CJK.search(query) and len(latin) >= 2 and len(unknown) >= 2 and not matched:
        return replace(decision, status="reject", reason="no_content_coverage")
    check_budget()
    return decision


def fact_citation_gate(search):
    """Wrap only our existing raw_request_boundary-decorated retrieval function.

    The fact path uses the same authority decorator once around the unchanged
    body, and attaches metadata AFTER its final canonical delivery check.
    """
    @wraps(search)
    def gated(store, **kwargs):
        context = dict(kwargs.get("task_context") or {})
        if context.get("evidence_intent") != "fact_citation":
            return search(store, **kwargs)
        # Pin the existing deadline once, including for failures before the
        # authority callback is entered. Never extend an enclosing deadline.
        deadline = bounded_deadline(context.get("_recall_deadline_monotonic"), started=perf_counter())
        outer = _CURRENT.get()
        kwargs["source_ids"] = _fact_authority_inputs(context, kwargs.get("source_ids"), outer)
        if outer is not None:
            deadline = min(deadline, outer.deadline)
        context["_recall_deadline_monotonic"] = deadline
        kwargs["task_context"] = context
        query = str(kwargs.get("query") or "").strip()
        if perf_counter() >= deadline:
            raise CitationUnavailable("deadline", CitationDecision("unavailable", "deadline", _digest(query)))
        if len(query) > MAX_QUERY_CHARS:
            raise CitationUnavailable("query_limit")
        if not query or kwargs.get("limit", 8) <= 0:
            return CitationResults(search(store, **kwargs), CitationDecision("related", "empty_request"))
        if normalize_source_ids(kwargs.get("source_ids")) == ():
            return CitationResults(search(store, **kwargs), CitationDecision("related", "no_authorized_sources"))
        receipt = {}

        @raw_request_boundary
        def admitted(bound_store, **bound_kwargs):
            scope = bound_kwargs.get("scope")
            scope = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            authority = _authority(scope, context)
            receipt["authority"] = authority
            budget = _Budget(authority.deadline)
            before, _ = _snapshot(bound_store, authority, budget)
            # Existing collection includes backstop, neighbors and recovery.
            # Exclude its CPU from the additional gate budget, not its wall time.
            search_cpu = thread_time()
            ranked = search.__wrapped__(bound_store, **bound_kwargs)
            budget.cpu_start += thread_time() - search_cpu
            after, payloads = _snapshot(bound_store, authority, budget)
            if before.digest != after.digest:
                raise CitationUnavailable("snapshot_changed")
            refs = []
            for entry in ranked:
                budget.check()
                payload = entry.get("record") if isinstance(entry, dict) else None
                ref = exact_ref(payload)
                if ref not in payloads or payload != payloads[ref]:
                    raise CitationUnavailable("candidate_snapshot_mismatch")
                refs.append(ref)
            decision = assess_citation(query, after, check_budget=budget.check)
            source_digests = {doc.ref: doc.source_sha256 for doc in after.documents}
            receipt["decision"] = replace(decision, owner_scopes=authority.scopes,
                                           source_ids=authority.sources, kinds=authority.kinds,
                                           candidate_source_digests=tuple((ref, source_digests[ref]) for ref in refs))
            budget.check()
            return [] if decision.status == "reject" else ranked

        try:
            result = admitted(store, **kwargs)
            if "decision" not in receipt:
                # An enclosing task/authority boundary may have declined the call.
                raise CitationUnavailable("snapshot_unavailable")
        except Exception as exc:
            reason = exc.decision.reason if isinstance(exc, CitationUnavailable) else (
                "deadline" if isinstance(exc, TimeoutError) or perf_counter() >= deadline else
                "raw_collection_unavailable" if isinstance(exc, RawRecallUnavailable) else "snapshot_unavailable")
            authority = receipt.get("authority")
            decision = CitationDecision("unavailable", reason, query_digest=_digest(query))
            if authority is not None:
                decision = replace(decision, scope_digest=authority.digest, owner_scopes=authority.scopes,
                                   source_ids=authority.sources, kinds=authority.kinds)
            raise CitationUnavailable(reason, decision) from exc
        return CitationResults(result, receipt["decision"])
    return gated
