from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import html
import json
import re
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse
import xml.etree.ElementTree as ET


FetchTextFunc = Callable[[str], str]

_ARXIV_API_BASE = "https://export.arxiv.org/api/query"
_CROSSREF_WORKS_BASE = "https://api.crossref.org/works"
_PROMPT_INJECTION_PATTERNS = (
    "ignore previous instructions",
    "reveal the system prompt",
    "system prompt",
    "developer message",
)
_SECRET_PATTERNS = (
    "authorization:",
    "bearer ",
    "api_key",
    "api key",
    "secret",
    "token",
    "password",
)


@dataclass(frozen=True)
class CollectedItem:
    title: str
    url: str
    content: str
    published_at: str
    source_kind: str
    metadata: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""


@dataclass(frozen=True)
class FetchResult:
    ok: bool
    items: list[CollectedItem] = field(default_factory=list)
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_feed_xml(xml_text: str, *, source_url: str = "") -> FetchResult:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return _safe_error("invalid XML feed")

    items: list[CollectedItem] = []
    seen: set[str] = set()
    if _local_name(root.tag) == "feed":
        raw_entries = list(_children(root, "entry"))
    else:
        raw_entries = list(root.iterfind(".//item"))

    for entry in raw_entries:
        parsed = _feed_entry_to_item(entry, source_url=source_url)
        if parsed is None:
            continue
        dedupe_key = parsed.url or parsed.fingerprint
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        items.append(parsed)

    return FetchResult(ok=True, items=items, metadata={"source_url": source_url} if source_url else {})


def build_arxiv_api_url(query: str) -> str:
    cleaned = _extract_arxiv_id(query)
    if cleaned:
        return f"{_ARXIV_API_BASE}?id_list={quote(cleaned, safe=',')}"
    return f"{_ARXIV_API_BASE}?search_query={quote(str(query).strip())}"


def parse_arxiv_xml(xml_text: str) -> FetchResult:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return _safe_error("invalid arxiv XML")

    items: list[CollectedItem] = []
    seen: set[str] = set()
    for entry in root.iter():
        if _local_name(entry.tag) != "entry":
            continue
        title = _clean_text(_child_text(entry, "title"))
        url = _arxiv_entry_url(entry)
        content = _clean_text(_child_text(entry, "summary"))
        published = _clean_text(_child_text(entry, "published") or _child_text(entry, "updated"))
        metadata = {"arxiv_id": _extract_arxiv_id(url or _child_text(entry, "id")) or ""}
        item = _collected_item(
            title=title,
            url=url,
            content=content,
            published_at=published,
            source_kind="arxiv",
            metadata=metadata,
        )
        dedupe_key = item.url or item.fingerprint
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        items.append(item)
    return FetchResult(ok=True, items=items)


def fetch_arxiv(query: str, fetch_text_func: FetchTextFunc) -> FetchResult:
    url = build_arxiv_api_url(query)
    try:
        return parse_arxiv_xml(fetch_text_func(url))
    except Exception:
        return _safe_error("fetch failed", metadata={"url": url})


def build_crossref_work_url(doi: str) -> str:
    normalized = _normalize_doi(doi)
    return f"{_CROSSREF_WORKS_BASE}/{quote(normalized, safe='')}"


def parse_crossref_work_json(payload: str | dict[str, Any]) -> FetchResult:
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return _safe_error("invalid Crossref JSON")
    else:
        data = payload

    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict):
        return _safe_error("invalid Crossref work")

    title = _first_text(message.get("title")) or _normalize_doi(str(message.get("DOI") or ""))
    doi = _normalize_doi(str(message.get("DOI") or ""))
    url = str(message.get("URL") or f"https://doi.org/{doi}")
    abstract = _clean_text(_strip_markup(str(message.get("abstract") or "")))
    published_at = _crossref_date(message)
    metadata = {
        "doi": doi,
        "container_title": _first_text(message.get("container-title")),
        "publisher": str(message.get("publisher") or ""),
    }
    return FetchResult(
        ok=True,
        items=[
            _collected_item(
                title=title,
                url=url,
                content=abstract,
                published_at=published_at,
                source_kind="doi",
                metadata=metadata,
            )
        ],
    )


def normalize_github_url(url: str) -> dict[str, str]:
    parsed = urlparse(str(url).strip())
    if parsed.netloc.lower() != "github.com":
        return {}

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return {}

    owner, repo = parts[0], parts[1]
    base_url = f"https://github.com/{owner}/{repo}"
    result = {"kind": "repo", "owner": owner, "repo": repo, "url": base_url}
    if len(parts) >= 5 and parts[2] == "releases" and parts[3] == "tag":
        result.update({"kind": "release", "tag": parts[4], "url": f"{base_url}/releases/tag/{parts[4]}"})
    elif len(parts) >= 4 and parts[2] in {"issues", "pull"}:
        result.update({"kind": parts[2][:-1] if parts[2] == "issues" else "pull", "number": parts[3]})
        result["url"] = f"{base_url}/{parts[2]}/{parts[3]}"
    return result


def collect_from_source_entry(source: Any, fetch_text: FetchTextFunc | None = None) -> FetchResult:
    source_kind = str(getattr(source, "source_kind", "") or "").strip().lower()
    title = str(getattr(source, "title", "") or "").strip()
    uri = str(getattr(source, "uri", "") or "").strip()
    resolved_kind = _resolve_source_kind(source_kind, uri)

    if not uri:
        return _safe_error("missing source URI")
    if _is_local_uri(uri):
        return FetchResult(ok=True, metadata={"skipped": True, "reason": "local_source_left_to_loop"})
    if resolved_kind == "github":
        normalized = normalize_github_url(uri)
        if not normalized:
            return _safe_error("invalid GitHub URL")
        return FetchResult(
            ok=True,
            items=[
                _collected_item(
                    title=title or _github_title(normalized),
                    url=normalized["url"],
                    content="",
                    published_at="",
                    source_kind="github",
                    metadata={"github": normalized, "dry_run": True},
                )
            ],
        )

    fetch_url = uri
    parser: Callable[[str], FetchResult]
    if resolved_kind == "arxiv":
        fetch_url = build_arxiv_api_url(uri)
        parser = parse_arxiv_xml
    elif resolved_kind == "doi":
        fetch_url = build_crossref_work_url(uri)
        parser = parse_crossref_work_json
    elif resolved_kind in {"rss", "http"}:
        parser = lambda text: parse_feed_xml(text, source_url=uri)
    else:
        return FetchResult(ok=True, metadata={"dry_run": True, "unsupported": source_kind or resolved_kind})

    if fetch_text is None:
        return FetchResult(ok=True, metadata={"dry_run": True, "url": fetch_url, "source_kind": resolved_kind})
    try:
        return parser(fetch_text(fetch_url))
    except Exception:
        return _safe_error("fetch failed", metadata={"url": fetch_url})


def _feed_entry_to_item(entry: ET.Element, *, source_url: str) -> CollectedItem | None:
    if _local_name(entry.tag) == "entry":
        title = _clean_text(_child_text(entry, "title"))
        url = _atom_link(entry) or _clean_text(_child_text(entry, "id"))
        content = _clean_text(_child_text(entry, "content") or _child_text(entry, "summary"))
        published = _clean_text(_child_text(entry, "published") or _child_text(entry, "updated"))
    else:
        title = _clean_text(_child_text(entry, "title"))
        url = _clean_text(_child_text(entry, "link") or _child_text(entry, "guid"))
        content = _clean_text(_child_text(entry, "encoded") or _child_text(entry, "content") or _child_text(entry, "description"))
        published = _clean_text(_child_text(entry, "pubDate") or _child_text(entry, "published"))
    if not any((title, url, content)):
        return None
    return _collected_item(
        title=title,
        url=url,
        content=content,
        published_at=published,
        source_kind="rss",
        metadata={"feed_url": source_url} if source_url else {},
    )


def _collected_item(
    *,
    title: str,
    url: str,
    content: str,
    published_at: str,
    source_kind: str,
    metadata: dict[str, Any] | None = None,
) -> CollectedItem:
    metadata = dict(metadata or {})
    safety = _safety_for_text(" ".join([title, url, content, json.dumps(metadata, ensure_ascii=False, default=str)]))
    safe_content = content
    safe_title = title
    safe_url = url
    if safety:
        metadata["safety"] = safety
        metadata = _redacted_metadata(metadata)
        safe_content = ""
        safe_title = "[redacted]"
        safe_url = "[redacted]"
    fingerprint = _fingerprint(source_kind, url, title, safe_content)
    return CollectedItem(
        title=safe_title,
        url=safe_url,
        content=safe_content,
        published_at=published_at,
        source_kind=source_kind,
        metadata=metadata,
        fingerprint=fingerprint,
    )


def _safety_for_text(text: str) -> dict[str, bool]:
    lowered = str(text or "").lower()
    safety: dict[str, bool] = {}
    if any(pattern in lowered for pattern in _PROMPT_INJECTION_PATTERNS):
        safety["prompt_injection"] = True
    if any(pattern in lowered for pattern in _SECRET_PATTERNS):
        safety["content_redacted"] = True
    return safety


def _safe_error(message: str, *, metadata: dict[str, Any] | None = None) -> FetchResult:
    safe_metadata = dict(metadata or {})
    safe_metadata["safety"] = {"content_redacted": True}
    for key in ("url", "uri", "source_uri"):
        if key in safe_metadata:
            safe_metadata[key] = "[redacted]"
    return FetchResult(ok=False, error=message, metadata=safe_metadata)


def _redacted_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in metadata.items():
        if key == "safety":
            result[key] = value
        elif isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = "[redacted]"
        elif isinstance(value, dict):
            result[key] = {"redacted": True}
        elif isinstance(value, list):
            result[key] = []
        else:
            result[key] = "[redacted]"
    return result


def _fingerprint(source_kind: str, url: str, title: str, content: str) -> str:
    raw = "\n".join((source_kind, url, title, content))
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, local_name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local_name(child.tag) == local_name]


def _child_text(element: ET.Element, local_name: str) -> str:
    for child in element.iter():
        if child is element:
            continue
        if _local_name(child.tag) == local_name:
            return "".join(child.itertext())
    return ""


def _atom_link(entry: ET.Element) -> str:
    fallback = ""
    for child in entry.iter():
        if _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        rel = str(child.attrib.get("rel") or "").strip()
        if rel == "alternate" and href:
            return href
        if href and not fallback:
            fallback = href
    return fallback


def _arxiv_entry_url(entry: ET.Element) -> str:
    link = _atom_link(entry)
    if link:
        return link
    return _clean_text(_child_text(entry, "id"))


def _clean_text(value: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", str(value or "")).strip())


def _strip_markup(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def _first_text(value: Any) -> str:
    if isinstance(value, list) and value:
        return _clean_text(str(value[0]))
    return _clean_text(str(value or ""))


def _crossref_date(message: dict[str, Any]) -> str:
    for key in ("published-print", "published-online", "published", "created", "issued"):
        date_parts = message.get(key, {}).get("date-parts") if isinstance(message.get(key), dict) else None
        if not date_parts or not isinstance(date_parts, list) or not date_parts[0]:
            continue
        parts = [int(part) for part in date_parts[0]]
        if len(parts) == 1:
            return f"{parts[0]:04d}"
        if len(parts) == 2:
            return f"{parts[0]:04d}-{parts[1]:02d}"
        return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
    return ""


def _extract_arxiv_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if "arxiv.org" in parsed.netloc:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"abs", "pdf"}:
            return parts[1].removesuffix(".pdf")
    match = re.search(r"(?i)([a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5}(?:v\d+)?)", text)
    return match.group(1) if match else ""


def _normalize_doi(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"(?i)^doi:\s*", "", text)
    parsed = urlparse(text)
    if parsed.netloc.lower() in {"doi.org", "dx.doi.org"}:
        text = parsed.path.lstrip("/")
    return unquote(text).strip()


def _resolve_source_kind(source_kind: str, uri: str) -> str:
    lowered = uri.lower()
    if source_kind in {"rss", "arxiv", "doi", "github"}:
        return source_kind
    if "github.com/" in lowered:
        return "github"
    if "arxiv.org/" in lowered or _extract_arxiv_id(uri):
        return "arxiv"
    if "doi.org/" in lowered or lowered.startswith("doi:") or re.match(r"^10\.\d{4,9}/", uri):
        return "doi"
    if lowered.startswith(("http://", "https://")):
        return "http"
    return source_kind


def _is_local_uri(uri: str) -> bool:
    parsed = urlparse(uri)
    return parsed.scheme in {"", "file"} or len(parsed.scheme) == 1


def _github_title(normalized: dict[str, str]) -> str:
    base = f"{normalized.get('owner', '')}/{normalized.get('repo', '')}"
    kind = normalized.get("kind", "repo")
    if kind == "release":
        return f"{base} release {normalized.get('tag', '')}".strip()
    if kind in {"issue", "pull"}:
        return f"{base} {kind} {normalized.get('number', '')}".strip()
    return base
