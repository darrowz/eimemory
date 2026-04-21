from __future__ import annotations

from uuid import uuid4


_PREFIX_BY_KIND = {
    "memory": "mem",
    "multimodal_memory": "mmem",
    "task": "task",
    "unknown": "unk",
    "reflection": "ref",
    "feedback": "fb",
    "incident": "inc",
    "rule": "rule",
    "pattern": "pat",
    "snapshot": "snap",
    "replay_result": "replay",
    "outcome": "out",
}


def generate_record_id(kind: str) -> str:
    prefix = _PREFIX_BY_KIND.get(kind, "rec")
    return f"{prefix}_{uuid4().hex[:12]}"
