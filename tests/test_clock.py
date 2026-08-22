from __future__ import annotations

import os
import time

import pytest

from eimemory.core.clock import now_iso


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires POSIX timezone control")
def test_now_iso_is_utc_even_when_host_timezone_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    original_tz = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        time.tzset()

        timestamp = now_iso()

        assert timestamp.endswith("Z")
        assert "+08:00" not in timestamp
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        time.tzset()