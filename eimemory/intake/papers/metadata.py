from __future__ import annotations

from copy import deepcopy
from typing import Any


def paper_metadata_placeholders() -> dict[str, Any]:
    return {
        "title": "",
        "authors": [],
        "abstract": "",
        "venue": "",
        "published_at": "",
        "doi": "",
        "arxiv_id": "",
        "canonical_url": "",
        "pdf_blob_ref": "",
        "normalized_text_ref": "",
        "enrichment_state": {
            "title": "pending",
            "authors": "pending",
            "abstract": "pending",
            "venue": "pending",
            "published_at": "pending",
        },
    }


def deep_merge_dicts(base: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = deepcopy(base)
    if not isinstance(extra, dict):
        return merged
    for key, value in extra.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[str(key)] = deep_merge_dicts(existing, value)
            continue
        merged[str(key)] = deepcopy(value)
    return merged


def build_paper_metadata(
    input_data: dict[str, Any] | None = None,
    upstream_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = deep_merge_dicts(paper_metadata_placeholders(), upstream_metadata)
    input_data = dict(input_data or {})
    for key in ("title", "abstract", "venue", "published_at", "doi", "arxiv_id", "canonical_url", "pdf_blob_ref", "normalized_text_ref"):
        value = input_data.get(key)
        if value is not None and value != "":
            metadata[key] = value
    authors = input_data.get("authors")
    if isinstance(authors, list):
        metadata["authors"] = [str(item) for item in authors]
    elif authors is not None and authors != "":
        metadata["authors"] = [str(authors)]
    return deepcopy(metadata)
