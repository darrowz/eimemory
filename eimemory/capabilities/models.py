"""Backward-compatible re-export of sunk capability entity models (ARCH-01)."""
from __future__ import annotations

from eimemory.contracts.capability_models import *  # noqa: F403
from eimemory.contracts import capability_models as _impl

__all__ = [name for name in dir(_impl) if not name.startswith("_")]
