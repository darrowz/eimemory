from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from eimemory.intake.papers.metadata import build_paper_metadata, deep_merge_dicts

VALID_SOURCE_KINDS = {"arxiv", "doi", "pdf", "url"}
DOI_HOSTS = {"doi.org", "dx.doi.org", "www.doi.org", "www.dx.doi.org"}
ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org"}


def detect_paper_source_kind(input_data: dict[str, Any]) -> str:
    source_kind = str(input_data.get("source_kind") or "").strip().lower()
    raw_url = str(input_data.get("canonical_url") or input_data.get("paper_url") or input_data.get("url") or "").strip()
    if source_kind in VALID_SOURCE_KINDS:
        return source_kind
    if input_data.get("arxiv_id"):
        return "arxiv"
    if input_data.get("doi"):
        return "doi"
    if input_data.get("pdf_file") or input_data.get("pdf_path"):
        return "pdf"
    if _extract_arxiv_id(raw_url):
        return "arxiv"
    if _extract_doi(raw_url):
        return "doi"
    if input_data.get("paper_url") or input_data.get("url"):
        return "url"
    raise ValueError("unable to detect paper source kind")


def normalize_paper_source_payload(input_data: dict[str, Any]) -> dict[str, Any]:
    input_data = dict(input_data or {})
    source_kind = detect_paper_source_kind(input_data)
    pdf_file = input_data.get("pdf_file") or input_data.get("pdf_path")
    pdf_path = str(Path(pdf_file)) if pdf_file else ""
    raw_url = str(input_data.get("canonical_url") or input_data.get("paper_url") or input_data.get("url") or "")
    canonical_url = canonicalize_identifier_url(raw_url)
    doi = canonicalize_doi(input_data.get("doi") or (_extract_doi(raw_url) if source_kind == "doi" else ""))
    arxiv_id = canonicalize_arxiv_id(input_data.get("arxiv_id") or (_extract_arxiv_id(raw_url) if source_kind == "arxiv" else ""))
    normalized = {
        "source_kind": source_kind,
        "title": str(input_data.get("title") or ""),
        "authors": [str(item) for item in (input_data.get("authors") or [])] if isinstance(input_data.get("authors"), list) else [],
        "abstract": str(input_data.get("abstract") or ""),
        "venue": str(input_data.get("venue") or ""),
        "published_at": str(input_data.get("published_at") or ""),
        "doi": doi,
        "arxiv_id": arxiv_id,
        "canonical_url": canonical_url,
        "pdf_path": pdf_path,
        "pdf_blob_ref": str(input_data.get("pdf_blob_ref") or pdf_path or ""),
        "normalized_text_ref": str(input_data.get("normalized_text_ref") or ""),
    }
    normalized["metadata"] = build_paper_metadata(normalized, upstream_metadata=input_data.get("metadata"))
    normalized["source_hash"] = build_paper_source_hash(normalized)
    normalized["provenance"] = build_paper_provenance(input_data)
    return normalized


def build_paper_provenance(input_data: dict[str, Any] | None = None) -> dict[str, Any]:
    input_data = dict(input_data or {})
    upstream = input_data.get("provenance")
    input_keys = {str(key) for key in input_data.keys()}
    upstream_dict = dict(upstream) if isinstance(upstream, dict) else {}
    upstream_input_keys = upstream_dict.get("input_keys")
    if isinstance(upstream_input_keys, (list, tuple, set)):
        input_keys.update(str(key) for key in upstream_input_keys)
    provenance = deep_merge_dicts(upstream_dict, {
        "input_keys": sorted(str(key) for key in input_data.keys()),
        "input_kind": "paper_intake",
    })
    provenance["input_keys"] = sorted(input_keys)
    return provenance


def normalize_paper_input(input_data: dict[str, Any]) -> dict[str, Any]:
    return normalize_paper_source_payload(input_data)


def build_paper_source_hash(payload: dict[str, Any]) -> str:
    pdf_identity = _pdf_identity(payload)
    fingerprint = {
        "source_kind": str(payload.get("source_kind") or ""),
        "arxiv_id": canonicalize_arxiv_id(payload.get("arxiv_id")),
        "doi": canonicalize_doi(payload.get("doi")),
        "canonical_url": canonicalize_identifier_url(payload.get("canonical_url")),
        "pdf_identity": pdf_identity,
    }
    digest = sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return digest


def _pdf_identity(payload: dict[str, Any]) -> str:
    if any(str(payload.get(key) or "").strip() for key in ("arxiv_id", "doi", "canonical_url")):
        return ""
    pdf_ref = str(payload.get("pdf_blob_ref") or payload.get("pdf_path") or "").strip()
    if not pdf_ref:
        return ""
    pdf_path = Path(pdf_ref)
    if pdf_path.is_file():
        digest = sha256()
        with pdf_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    return sha256(pdf_ref.encode("utf-8")).hexdigest()


def canonicalize_doi(value: Any) -> str:
    raw = unquote(str(value or "").strip())
    if not raw:
        return ""
    lower_raw = raw.lower()
    if lower_raw.startswith("doi:"):
        raw = raw[4:].strip()
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.netloc.lower() in DOI_HOSTS:
        raw = parsed.path.lstrip("/")
    return raw.strip().lstrip("/").lower()


def canonicalize_arxiv_id(value: Any) -> str:
    raw = unquote(str(value or "").strip())
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.netloc.lower() in ARXIV_HOSTS:
        path = parsed.path.strip("/")
        lower_path = path.lower()
        if lower_path.startswith("abs/"):
            raw = path[4:]
        elif lower_path.startswith("pdf/"):
            raw = path[4:]
            if raw.lower().endswith(".pdf"):
                raw = raw[:-4]
        else:
            raw = path
    lower_raw = raw.lower()
    if lower_raw.startswith("arxiv:"):
        raw = raw[6:]
    return raw.strip().lower()


def canonicalize_identifier_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    auth = ""
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth = f"{auth}:{parsed.password}"
        auth = f"{auth}@"
    netloc = f"{auth}{hostname}"
    if port:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))


def _extract_doi(value: Any) -> str:
    raw = unquote(str(value or "").strip())
    if not raw:
        return ""
    lower_raw = raw.lower()
    if lower_raw.startswith("doi:"):
        return canonicalize_doi(raw)
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.netloc:
        if parsed.netloc.lower() not in DOI_HOSTS:
            return ""
        return canonicalize_doi(raw)
    candidate = canonicalize_doi(raw)
    if "/" not in candidate:
        return ""
    return candidate


def _extract_arxiv_id(value: Any) -> str:
    raw = unquote(str(value or "").strip())
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.netloc and parsed.netloc.lower() not in ARXIV_HOSTS:
        return ""
    candidate = canonicalize_arxiv_id(raw)
    if not candidate:
        return ""
    return candidate
