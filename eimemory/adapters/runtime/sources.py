"""Shared source partition defaults for runtime producers and effect consumers."""
import os

from eimemory.adapters.runtime.channel import normalize_runtime_channel
from eimemory.models.source_partitions import normalize_source_ids


def runtime_source_ids(channel, source_ids=None):
    channel = normalize_runtime_channel(channel)
    if source_ids is not None:
        return list(normalize_source_ids(source_ids))
    configured = [value.strip() for value in os.getenv("EIMEMORY_SOURCE_IDS", "").split(",") if value.strip()]
    sources = list(dict.fromkeys(configured)) or (["hermes"] if channel == "hermes" else ["default"])
    if channel == "hermes" and "hermes" not in sources:
        sources.append("hermes")
    return list(normalize_source_ids(sources))
