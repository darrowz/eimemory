from __future__ import annotations

from datetime import datetime, timezone
import os
import time

import pytest

from eimemory.core import clock
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


def test_now_iso_requests_utc_and_formats_whole_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    observed_timezones = []

    class FixedDatetime:
        @staticmethod
        def now(tz):
            observed_timezones.append(tz)
            return datetime(2026, 1, 2, 0, 3, 4, 987654, tzinfo=timezone.utc)

    monkeypatch.setattr(clock, "datetime", FixedDatetime)

    assert now_iso() == "2026-01-02T00:03:04Z"
    assert observed_timezones == [timezone.utc]
