"""Shared helpers for encoding untrusted memory content into host prompts.

Memory text is always data, never instructions. Render paths that inject
recall/loadout/proactive content into a host LLM prompt must wrap payloads
through these helpers so markers stay consistent across surfaces.
"""

from __future__ import annotations

import json
from typing import Any, Mapping


UNTRUSTED_TRUST_ATTR = "untrusted-data"
LOADOUT_CONTEXT_TAG = "eimemory_loadout_context"
PROACTIVE_CONTEXT_TAG = "eimemory_proactive_context"


def safe_untrusted_json(value: Mapping[str, Any] | dict[str, Any]) -> str:
    """Serialize a mapping as JSON with angle-bracket / ampersand escaping."""
    return (
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def wrap_untrusted_block(body: str, *, tag: str = LOADOUT_CONTEXT_TAG) -> str:
    """Wrap already-rendered body text in an untrusted-data XML fence."""
    inner = str(body or "").rstrip()
    if not inner:
        return ""
    header = f'<{tag} trust="{UNTRUSTED_TRUST_ATTR}">\n'
    footer = f"</{tag}>"
    return f"{header}{inner}\n{footer}"


def wrap_untrusted_json_lines(
    items: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    *,
    tag: str = PROACTIVE_CONTEXT_TAG,
) -> str:
    """Encode each item as safe JSON lines inside an untrusted fence."""
    if not items:
        return ""
    lines = [f'<{tag} trust="{UNTRUSTED_TRUST_ATTR}">']
    for item in items:
        lines.append(safe_untrusted_json(item))
    lines.append(f"</{tag}>")
    return "\n".join(lines)
