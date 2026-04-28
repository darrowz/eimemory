from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import urljoin


_BLOCK_TAGS = {
    "article",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
_SKIP_TAGS = {
    "aside",
    "button",
    "canvas",
    "footer",
    "form",
    "header",
    "iframe",
    "input",
    "nav",
    "noscript",
    "script",
    "select",
    "style",
    "svg",
    "textarea",
}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_CANDIDATE_TAGS = {"article", "main", "section", "div", "body"}
_CONTENT_HINTS = ("article", "content", "entry", "main", "post", "rich_media")
_NOISE_HINTS = ("ad", "banner", "comment", "footer", "header", "login", "nav", "share", "sidebar", "signup")


@dataclass(frozen=True)
class FulltextDocument:
    ok: bool
    title: str = ""
    text: str = ""
    byline: str = ""
    date: str = ""
    canonical_url: str = ""
    images: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    quality_score: float = 0.0
    error: str = ""


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["_Node"] = field(default_factory=list)
    data: list[str] = field(default_factory=list)


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.meta: dict[str, str] = {}
        self.images: list[str] = []
        self.links: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {name.lower(): value or "" for name, value in attrs}
        node = _Node(tag=tag, attrs=attr_map)
        self.stack[-1].children.append(node)

        if tag == "meta":
            key = attr_map.get("property") or attr_map.get("name")
            if key and attr_map.get("content"):
                self.meta[key.lower()] = attr_map["content"]
        elif tag == "link":
            self.links.append(attr_map)
        elif tag == "img":
            image = attr_map.get("src") or attr_map.get("data-src") or attr_map.get("data-original")
            if image:
                self.images.append(image)

        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].data.append(data)


def extract_fulltext(url: str, payload: str | bytes | None, source_kind: str | None = None) -> FulltextDocument:
    return parse_fulltext_document(payload, url=url, source_kind=source_kind)


def parse_fulltext_document(
    payload: str | bytes | None,
    *,
    url: str = "",
    source_kind: str | None = None,
) -> FulltextDocument:
    source_url = str(url or "")
    if payload is None:
        return _empty_result(source_url, source_kind)

    html_text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
    if not html_text.strip():
        return _empty_result(source_url, source_kind)

    parser = _DocumentParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return FulltextDocument(
            ok=False,
            canonical_url=source_url,
            meta=_base_meta(source_url, source_kind),
            error="invalid html payload",
        )

    id_map = _nodes_by_attr(parser.root, "id")
    title = _pick_title(parser, id_map)
    byline = _first_non_empty(
        _node_text(id_map.get("js_name")),
        parser.meta.get("author"),
        parser.meta.get("article:author"),
        parser.meta.get("og:article:author"),
    )
    date = _first_non_empty(
        _node_text(id_map.get("publish_time")),
        parser.meta.get("article:published_time"),
        parser.meta.get("date"),
        parser.meta.get("pubdate"),
        parser.meta.get("publishdate"),
        parser.meta.get("weibo: article:create_at"),
    )
    canonical_url = _canonical_url(parser, source_url)
    raw_images = _unique([parser.meta.get("og:image", ""), parser.meta.get("twitter:image", ""), *parser.images])
    images = [urljoin(canonical_url or source_url, image) for image in raw_images if image]

    candidate = _best_candidate(parser.root, id_map=id_map, source_kind=source_kind)
    text = _normalize_text(_node_text(candidate)) if candidate is not None else ""
    quality_score = _quality_score(text=text, title=title, images=images)
    ok = bool(text) and quality_score >= 0.4

    meta = _base_meta(source_url, source_kind)
    meta.update(
        {
            "content_node": _node_label(candidate),
            "raw_meta": dict(parser.meta),
        }
    )

    return FulltextDocument(
        ok=ok,
        title=title,
        text=text,
        byline=byline,
        date=date,
        canonical_url=canonical_url or source_url,
        images=images,
        meta=meta,
        quality_score=quality_score,
        error="" if ok else "low quality content",
    )


def _empty_result(source_url: str, source_kind: str | None) -> FulltextDocument:
    return FulltextDocument(
        ok=False,
        canonical_url=source_url,
        meta=_base_meta(source_url, source_kind),
        error="empty payload",
    )


def _base_meta(source_url: str, source_kind: str | None) -> dict[str, Any]:
    meta: dict[str, Any] = {"source_url": source_url}
    if source_kind:
        meta["source_kind"] = source_kind
    return meta


def _pick_title(parser: _DocumentParser, id_map: dict[str, _Node]) -> str:
    return _clean_inline(
        _first_non_empty(
            _node_text(id_map.get("activity-name")),
            parser.meta.get("og:title"),
            parser.meta.get("twitter:title"),
            _node_text(_first_by_tag(parser.root, "h1")),
            _strip_title_suffix(_node_text(_first_by_tag(parser.root, "title"))),
        )
    )


def _canonical_url(parser: _DocumentParser, source_url: str) -> str:
    for link in parser.links:
        rel = link.get("rel", "").lower()
        href = link.get("href", "")
        if "canonical" in rel and href:
            return urljoin(source_url, href)
    return parser.meta.get("og:url") or source_url


def _best_candidate(root: _Node, *, id_map: dict[str, _Node], source_kind: str | None) -> _Node | None:
    if source_kind == "wechat" and id_map.get("js_content") is not None:
        return id_map["js_content"]
    if id_map.get("js_content") is not None:
        return id_map["js_content"]

    candidates = [node for node in _walk(root) if node.tag in _CANDIDATE_TAGS]
    if not candidates:
        return root
    return max(candidates, key=_candidate_score)


def _candidate_score(node: _Node) -> float:
    text = _normalize_text(_node_text(node))
    compact_len = len(re.sub(r"\s+", "", text))
    paragraph_count = sum(1 for child in _walk(node) if child.tag == "p" and len(_node_text(child).strip()) >= 10)
    link_text_len = sum(len(_node_text(child)) for child in _walk(node) if child.tag == "a")
    attrs = " ".join([node.tag, node.attrs.get("id", ""), node.attrs.get("class", "")]).lower()
    hint_bonus = 250 if any(hint in attrs for hint in _CONTENT_HINTS) else 0
    noise_penalty = 300 if any(hint in attrs for hint in _NOISE_HINTS) else 0
    return compact_len + paragraph_count * 80 + hint_bonus - link_text_len * 0.7 - noise_penalty


def _quality_score(*, text: str, title: str, images: list[str]) -> float:
    if not text:
        return 0.0
    compact_len = len(re.sub(r"\s+", "", text))
    paragraphs = [part for part in text.split("\n\n") if len(part.strip()) >= 10]
    score = min(0.45, compact_len / 320) + min(0.35, len(paragraphs) * 0.16)
    if title:
        score += 0.12
    if images:
        score += 0.05
    if compact_len < 45:
        score = min(score, 0.35)
    return round(min(1.0, score), 3)


def _node_text(node: _Node | None) -> str:
    if node is None or node.tag in _SKIP_TAGS:
        return ""

    parts: list[str] = []
    if node.data:
        parts.extend(node.data)
    for child in node.children:
        if child.tag in _SKIP_TAGS:
            continue
        if child.tag in _BLOCK_TAGS and parts and parts[-1] != "\n\n":
            parts.append("\n\n")
        parts.append(_node_text(child))
        if child.tag in _BLOCK_TAGS:
            parts.append("\n\n")
    return "".join(parts)


def _normalize_text(text: str) -> str:
    paragraphs = [_clean_inline(part) for part in re.split(r"\n{2,}", unescape(text))]
    paragraphs = [part for part in paragraphs if part]
    return "\n\n".join(paragraphs)


def _clean_inline(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", unescape(str(text))).strip()


def _first_non_empty(*values: str | None) -> str:
    for value in values:
        cleaned = _clean_inline(value)
        if cleaned:
            return cleaned
    return ""


def _strip_title_suffix(title: str) -> str:
    cleaned = _clean_inline(title)
    for separator in (" - ", " | ", " :: "):
        if separator in cleaned:
            return cleaned.split(separator, 1)[0].strip()
    return cleaned


def _first_by_tag(node: _Node, tag: str) -> _Node | None:
    for child in _walk(node):
        if child.tag == tag:
            return child
    return None


def _nodes_by_attr(root: _Node, attr: str) -> dict[str, _Node]:
    nodes: dict[str, _Node] = {}
    for node in _walk(root):
        value = node.attrs.get(attr)
        if value and value not in nodes:
            nodes[value] = node
    return nodes


def _walk(node: _Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        cleaned = _clean_inline(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return unique


def _node_label(node: _Node | None) -> str:
    if node is None:
        return ""
    identifier = node.attrs.get("id") or node.attrs.get("class")
    return f"{node.tag}#{identifier}" if identifier else node.tag
