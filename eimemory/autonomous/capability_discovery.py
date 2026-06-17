"""Discover new capabilities from weakness/incident clustering."""
from collections import Counter
from pathlib import Path
import json
from datetime import datetime, timedelta, timezone
import re

EXISTING_CAPABILITIES = {"code.implementation"}


def _bucket(summary: str) -> str:
    s = summary.lower()
    if re.search(r"recall|search|hit@|retriev", s):
        return "memory.recall_quality"
    if re.search(r"tool|mcp|function call", s):
        return "tool_use.efficiency"
    if re.search(r"govern|policy|permission|rbac", s):
        return "memory.governance"
    if re.search(r"embed|chunk|index", s):
        return "memory.embedding_quality"
    return ""


def _iter_weak(records_path: Path, days: int = 7):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with open(records_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") not in ("weakness", "incident"):
                continue
            occurred = r.get("time", {}).get("occurred_at", "")
            if not occurred:
                continue
            try:
                t = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
            except ValueError:
                continue
            if t < cutoff:
                continue
            yield r


def discover_new_capabilities(
    records_path: Path = Path("/var/lib/eimemory/records.jsonl"),
    min_count: int = 3,
) -> list[str]:
    """Cluster weaknesses/incidents, surface new capability names."""
    buckets: Counter = Counter()
    for r in _iter_weak(records_path):
        summary = (r.get("content", {}) or {}).get("summary", "")
        if not summary:
            continue
        cap = _bucket(summary)
        if cap:
            buckets[cap] += 1
    return [c for c, n in buckets.most_common() if n >= min_count and c not in EXISTING_CAPABILITIES]
