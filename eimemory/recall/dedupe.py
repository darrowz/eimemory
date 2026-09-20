"""Memory identity for recall dedupe.

``memory_content_key`` is exact full-payload identity. Callers must also
partition by scope/source ID.

``preference_paraphrase_key`` is a separate near-paraphrase identity for
preference-like memories. It collapses wording variants that share the same
modality roles or ordered steps, and deliberately does **not**:
- use dense cosine (dense-admission contract)
- merge opposite default/conditional roles
- merge opposite step orders
- merge versioned or differently sourced/statused payloads
"""
from __future__ import annotations

from hashlib import sha256
import json
import re

from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope


_PREFERENCE_TYPES = frozenset({
    "preference",
    "instruction",
    "persona",
    "user_preference",
    "operator_preference",
    "living_posture",
})

_SYNONYM_GROUPS = (
    frozenset({"链接", "短链", "网址", "地址", "url", "link"}),
    frozenset({"检查", "复核", "查看", "核对"}),
    frozenset({"评估", "评价"}),
    frozenset({"摘要", "总结", "概括"}),
)
_DROP_MODIFIERS = frozenset({"文章", "内容", "一个", "一种"})
_CANON: dict[str, str] = {}
for _group in _SYNONYM_GROUPS:
    _canon = sorted(_group, key=lambda item: (len(item), item))[0]
    for _term in _group:
        _CANON[_term] = _canon
# Generic procedure/entity atoms (not product-specific brands).
_EXTRA_ATOMS = (
    "作品", "标题", "文案", "读取", "断开", "电源", "纸路", "取出", "残纸",
    "关闭", "卸下", "镜头", "更换", "打印机", "卡纸", "机身", "灰尘", "抽",
)
for _term in _EXTRA_ATOMS:
    _CANON.setdefault(_term, _term)
_LEXICON = tuple(
    sorted(set(_CANON) | set(_EXTRA_ATOMS) | set(_DROP_MODIFIERS), key=len, reverse=True)
)

_MARKERS = tuple(sorted({
    "只有明确要求", "明确要求", "只有", "默认", "收到", "拿到",
    "才提供", "提供", "先给", "给", "先", "再", "然后", "最后", "后", "时", "才",
    "应该", "怎么", "怎样", "如何", "可以", "需要", "进行", "处理",
    "并", "与", "和", "的", "了",
}, key=len, reverse=True))

_CLAUSE_SPLIT = re.compile(r"[；;。\n，,]")
_STEP_RE = re.compile(r"(?:先|再|然后|最后)([^再然后最后；;。，,\n]+)")
_CONDITIONAL_MARKERS = ("只有", "明确要求", "除非")
_NEGATION_PREFIX = "不别勿未非没"
_ASCII_ATOM = re.compile(r"[A-Za-z]{2,}|\d+")
_CLEAN_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def memory_content_key(item: RecordEnvelope) -> str:
    # Projection truncation and token sets cannot establish fact equivalence.
    content = dict(item.content)
    if isinstance(content.get("text"), str):
        content["text"] = " ".join(content["text"].split())
    metadata = {
        key: value
        for key, value in business_metadata(item.meta).items()
        if key not in {"quality", "scoring"}
    }
    memory_type = str(metadata.get("memory_type") or content.get("memory_type") or "").lower()
    text = json.dumps(
        [
            item.kind,
            item.source,
            item.status,
            " ".join(item.title.split()),
            " ".join(item.summary.split()),
            item.detail,
            content,
            metadata,
            item.provenance,
            item.tags,
            [(link.relation, link.target_kind, link.target_id) for link in item.links],
            item.evidence,
            item.aliases,
            item.aliases_version,
            item.time.occurred_at if memory_type in {"event", "commitment"} else "",
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return sha256(text.encode("utf-8")).hexdigest()[:24]


def preference_paraphrase_key(item: RecordEnvelope) -> str | None:
    """Near-paraphrase identity for preference-like memories, or None.

    Exact duplicates still collapse via ``memory_content_key``. This key only
    applies when a preference body yields a structured fingerprint.
    """
    if item.kind != "memory":
        return None
    content = item.content if isinstance(item.content, dict) else {}
    metadata = business_metadata(item.meta)
    memory_type = str(
        metadata.get("memory_type") or content.get("memory_type") or ""
    ).strip().lower()
    if memory_type not in _PREFERENCE_TYPES:
        return None
    body = _preference_body(item)
    fingerprint = _preference_fingerprint(body)
    if fingerprint is None:
        return None
    version = _preference_version_token(item, content=content, metadata=metadata)
    payload = {
        "kind": "preference_paraphrase.v1",
        "status": str(item.status or ""),
        "source": str(item.source or ""),
        "detail": " ".join(str(item.detail or "").split()),
        "version": version,
        "fingerprint": fingerprint,
    }
    return "pp:" + sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]


def memory_dedupe_identities(item: RecordEnvelope) -> tuple[str, ...]:
    """Identities that may collapse result slots within a scope/source partition.

    Exact content identity always participates. Preference near-paraphrase
    identity is additive and never replaces exact identity.
    """
    identities: list[str] = []
    if item.kind == "memory":
        identities.append("exact:" + memory_content_key(item))
        paraphrase = preference_paraphrase_key(item)
        if paraphrase:
            identities.append(paraphrase)
    return tuple(identities)


def _preference_body(item: RecordEnvelope) -> str:
    content = item.content if isinstance(item.content, dict) else {}
    parts: list[str] = []
    for value in (item.title, item.summary, item.detail, content.get("text")):
        if isinstance(value, str) and value.strip():
            normalized = " ".join(value.split())
            if normalized and normalized not in parts:
                parts.append(normalized)
    return "；".join(parts)


def _preference_version_token(
    item: RecordEnvelope,
    *,
    content: dict,
    metadata: dict,
) -> str:
    for source in (content, metadata, item.meta if isinstance(item.meta, dict) else {}):
        if not isinstance(source, dict):
            continue
        for key in ("version", "preference_version", "schema_version", "revision"):
            value = source.get(key)
            if value is None or value == "":
                continue
            return str(value).strip()
    return ""


def _preference_fingerprint(text: str) -> list | None:
    body = str(text or "").strip()
    if not body:
        return None
    default: set[str] = set()
    conditional: set[str] = set()
    for clause in _CLAUSE_SPLIT.split(body):
        clause = clause.strip()
        if not clause:
            continue
        atoms = set(_preference_atoms(clause))
        if not atoms:
            continue
        if any(marker in clause for marker in _CONDITIONAL_MARKERS) or (
            re.search(r"才[\u4e00-\u9fffA-Za-z]", clause) and "默认" not in clause
        ):
            conditional |= atoms
        elif "默认" in clause:
            default |= atoms
    steps: list[list[str]] = []
    for match in _STEP_RE.finditer(body):
        step = sorted(set(_preference_atoms(match.group(0))))
        if step:
            steps.append(step)
    atoms = sorted(set(_preference_atoms(body)))
    if not atoms:
        return None
    if default or conditional:
        return ["roles", sorted(default), sorted(conditional)]
    if steps:
        return ["steps", steps]
    if len(atoms) < 2:
        return None
    return ["bag", atoms]


def _preference_atoms(text: str) -> tuple[str, ...]:
    raw = _CLEAN_RE.sub(" ", str(text or "")).lower().strip()
    if not raw:
        return ()
    scrubbed = raw
    for marker in _MARKERS:
        scrubbed = scrubbed.replace(marker.lower() if marker.isascii() else marker, " ")
    chunks: list[str] = []
    index = 0
    while index < len(scrubbed):
        char = scrubbed[index]
        if char.isspace():
            chunks.append(" ")
            index += 1
            continue
        hit = None
        for word in _LEXICON:
            if scrubbed.startswith(word, index):
                hit = (word, len(word))
                break
        if hit is not None:
            word, width = hit
            if word in _DROP_MODIFIERS:
                chunks.append(" ")
            else:
                chunks.append(" " + _CANON.get(word, word) + " ")
            index += width
            continue
        ascii_match = _ASCII_ATOM.match(scrubbed, index)
        if ascii_match:
            chunks.append(" " + ascii_match.group(0) + " ")
            index = ascii_match.end()
            continue
        chunks.append(char)
        index += 1
    atoms: set[str] = set()
    for part in "".join(chunks).split():
        if not part or part in _DROP_MODIFIERS:
            continue
        if (
            re.fullmatch(r"[\u4e00-\u9fff]+", part)
            and part not in _CANON.values()
            and part not in _EXTRA_ATOMS
        ):
            if len(part) <= 4:
                atoms.add(part)
            else:
                for offset in range(0, len(part) - 1, 2):
                    atoms.add(part[offset : offset + 2])
                if len(part) % 2:
                    atoms.add(part[-2:])
            continue
        atoms.add(part)
    out: set[str] = set()
    for atom in atoms:
        token = atom
        negated = False
        if token[:1] in _NEGATION_PREFIX and len(token) > 1:
            negated = True
            token = token[1:]
        else:
            start = 0
            while True:
                found = raw.find(token, start)
                if found < 0:
                    break
                if found > 0 and raw[found - 1] in _NEGATION_PREFIX:
                    negated = True
                    break
                start = found + 1
        if len(token) < 1:
            continue
        if len(token) == 1 and token in _NEGATION_PREFIX:
            continue
        out.add(("!" + token) if negated else token)
    return tuple(sorted(out))
