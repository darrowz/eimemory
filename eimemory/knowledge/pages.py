from __future__ import annotations

import hashlib


def stable_compiled_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def stable_page_id(page_type: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    prefix = "kpg" if page_type == "paper" else "ktop"
    return f"{prefix}_{digest}"


def summarize_claims(claims: list[str], *, max_items: int = 3) -> str:
    selected = [claim.strip() for claim in claims if claim.strip()][:max_items]
    return " ".join(selected)
