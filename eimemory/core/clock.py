from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    """Return the current instant in canonical RFC3339 UTC form.

    Capability v3 contracts accept UTC timestamps only.  Converting the UTC
    clock through ``astimezone()`` made this helper emit the host's local
    offset (for example ``+08:00`` on honxin), which caused live L5 projection
    to fail while explicit historical UTC projections continued to work.
    """

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
