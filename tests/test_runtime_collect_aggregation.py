"""Tests for ``Runtime.collect_external_sources`` failure aggregation.

Bug C (R9): the top-level ``ok`` field used to be hard-coded to ``True``
regardless of per-source outcomes. Each source should report its own
``ok`` flag, and the top-level report should:

* be ``ok=False`` when any source returns ``ok=False``;
* be ``ok=True`` when the source list is empty (vacuous success);
* list the offending ``source_id`` in ``failed_sources``;
* keep per-source payloads intact in ``results``.

The CLI ``intake collect`` command must exit non-zero on a top-level
``ok=False`` so cron / pipeline callers see the failure.

These tests do **not** touch the network. A fake ``fetch_text`` is
passed in so the source connector reports ``ok=False`` when its
response cannot be parsed. A non-loopback https URL is used to make
the connector take the "fetch" path instead of the "local file" path.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from eimemory.api.runtime import Runtime


# ---------- helpers ----------

def _setup_runtime(tmp_path: Path) -> Runtime:
    """Build a Runtime rooted at ``tmp_path``."""
    return Runtime.create(root=tmp_path / "runtime")


def _add_source(runtime: Runtime, *, source_id: str, uri: str, source_kind: str = "rss") -> None:
    runtime.sources.add_source(
        {
            "source_id": source_id,
            "source_kind": source_kind,
            "title": f"Source {source_id}",
            "uri": uri,
            "enabled": True,
            "tags": ["test"],
            "metadata": {"frequency": "daily", "max_items": 5},
        }
    )


def _good_xml(item_id: str = "dia", title: str = "Test feed") -> str:
    """A minimal, valid RSS document with one item."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        '<title>Test feed</title>'
        f'<item><title>{title}</title>'
        f'<link>https://example.test/{item_id}</link>'
        f'<description>Description for {item_id} with enough text for intake.</description>'
        f'<guid>{item_id}</guid>'
        '</item>'
        '</channel></rss>'
    )


def _bad_xml() -> str:
    """Garbage that ET.fromstring will reject with ParseError."""
    return "this is not rss <><><"


# ---------- aggregation tests ----------

def test_collect_with_all_sources_ok(tmp_path: Path) -> None:
    """All sources succeed -> top-level ok=True, failed_sources is empty."""
    runtime = _setup_runtime(tmp_path)
    try:
        _add_source(runtime, source_id="src-a", uri="https://example.test/feed-a")
        _add_source(runtime, source_id="src-b", uri="https://example.test/feed-b")

        # Both URLs return the same valid XML payload; the connector
        # parses them and yields ok=True for each.
        def fake_fetch(_url: str) -> str:
            return _good_xml()

        report = runtime.collect_external_sources(
            source_kind="rss",
            fetch=True,
            fetch_text=fake_fetch,
        )

        assert report["ok"] is True
        assert report["source_count"] == 2
        assert report["error_count"] == 0
        assert report["failed_sources"] == []
        # every result payload must carry its own source_id
        source_ids = [r.get("source_id") for r in report["results"]]
        assert source_ids == ["src-a", "src-b"]
        assert all(r.get("ok") is True for r in report["results"])
    finally:
        runtime.close()


def test_collect_with_one_source_failing(tmp_path: Path) -> None:
    """One failing source -> top-level ok=False, failed_sources names it."""
    runtime = _setup_runtime(tmp_path)
    try:
        _add_source(runtime, source_id="src-good", uri="https://example.test/feed-good")
        _add_source(runtime, source_id="src-bad", uri="https://example.test/feed-bad")

        # 'src-bad' returns garbage XML that fails to parse; 'src-good'
        # returns valid XML.
        def fake_fetch(url: str) -> str:
            if "feed-bad" in url:
                return _bad_xml()
            return _good_xml()

        report = runtime.collect_external_sources(
            source_kind="rss",
            fetch=True,
            fetch_text=fake_fetch,
        )

        assert report["ok"] is False
        assert report["source_count"] == 2
        assert report["error_count"] == 1
        assert report["failed_sources"] == ["src-bad"]
        # the good source still shows up as ok=True
        per_source = {r["source_id"]: r for r in report["results"]}
        assert per_source["src-good"]["ok"] is True
        assert per_source["src-bad"]["ok"] is False
    finally:
        runtime.close()


def test_collect_with_all_sources_failing(tmp_path: Path) -> None:
    """All sources fail -> top-level ok=False, failed_sources lists every id."""
    runtime = _setup_runtime(tmp_path)
    try:
        _add_source(runtime, source_id="src-a", uri="https://example.test/feed-a")
        _add_source(runtime, source_id="src-b", uri="https://example.test/feed-b")

        def fake_fetch(_url: str) -> str:
            return _bad_xml()

        report = runtime.collect_external_sources(
            source_kind="rss",
            fetch=True,
            fetch_text=fake_fetch,
        )

        assert report["ok"] is False
        assert report["source_count"] == 2
        assert report["error_count"] == 2
        assert sorted(report["failed_sources"]) == ["src-a", "src-b"]
        assert all(r.get("ok") is False for r in report["results"])
    finally:
        runtime.close()


def test_collect_failed_sources_listed_with_dedupe(tmp_path: Path) -> None:
    """failed_sources preserves the source order from the source registry."""
    runtime = _setup_runtime(tmp_path)
    try:
        _add_source(runtime, source_id="src-a", uri="https://example.test/feed-a")
        _add_source(runtime, source_id="src-b", uri="https://example.test/feed-b")
        _add_source(runtime, source_id="src-c", uri="https://example.test/feed-c")

        # src-b returns valid XML, src-a and src-c return garbage.
        def fake_fetch(url: str) -> str:
            if "feed-b" in url:
                return _good_xml()
            return _bad_xml()

        report = runtime.collect_external_sources(
            source_kind="rss",
            fetch=True,
            fetch_text=fake_fetch,
        )

        assert report["ok"] is False
        # Order is preserved (a, c). The healthy source 'src-b' is excluded.
        assert report["failed_sources"] == ["src-a", "src-c"]
        assert report["error_count"] == 2
    finally:
        runtime.close()


def test_collect_with_empty_sources_list(tmp_path: Path) -> None:
    """Empty source registry -> vacuous success (ok=True, no failures)."""
    runtime = _setup_runtime(tmp_path)
    try:
        report = runtime.collect_external_sources(source_kind="rss")

        assert report["ok"] is True
        assert report["source_count"] == 0
        assert report["error_count"] == 0
        assert report["failed_sources"] == []
        assert report["results"] == []
    finally:
        runtime.close()


def test_intake_collect_cli_returns_nonzero_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """``eimemory intake collect`` must exit non-zero when ok is False.

    Drives a real subprocess so we exercise the CLI exit code, not just
    the in-process helper. The runtime is rooted at a tmp dir and a
    source is added through the CLI; the fake fetch returns invalid XML
    so the connector reports ok=False end-to-end.
    """
    import os

    root = tmp_path / "runtime"
    feed_uri = "https://example.test/feed"

    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        "EIMEMORY_ROOT": str(root),
    }
    # Forward parent env so the spawned Python can resolve the eimemory
    # package installed in this checkout. Without this the subprocess
    # would get ModuleNotFoundError.
    for key, value in os.environ.items():
        env.setdefault(key, value)

    # Add a single source; we will point the runtime at a fake fetch
    # function via a tiny side-channel env var the test config picks up.
    # Easiest path: use the source registry as-is, but stub
    # ``eimemory.api.runtime._default_fetch_text`` at import time.
    src_dir = Path(__file__).resolve().parent
    runner_code = (
        "import json, os, sys\n"
        "from eimemory.api import runtime as rmod\n"
        f"rmod._default_fetch_text = lambda _url: 'definitely not rss'\n"
        "from eimemory.cli.main import main\n"
        "rc1 = main(['source', 'add', '--source-kind', 'rss', '--title', 'Bad', '--uri', %r])\n"
        "if rc1 != 0:\n"
        "    sys.exit(rc1)\n"
        "rc2 = main(['intake', 'collect', '--source-kind', 'rss', '--fetch'])\n"
        "sys.exit(rc2)\n" % feed_uri
    )
    runner_path = tmp_path / "runner.py"
    runner_path.write_text(runner_code, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(runner_path)],
        capture_output=True,
        env=env,
        timeout=30,
    )
    out = result.stdout.decode("utf-8", "replace")
    err = result.stderr.decode("utf-8", "replace")
    assert result.returncode != 0, (
        "intake collect should exit non-zero when sources fail. "
        f"returncode={result.returncode} stdout={out!r} stderr={err!r}"
    )
    # The collect output is the last JSON object the CLI printed. It is
    # pretty-printed (indent=2) so we cannot split on lines and just
    # parse the trailing block; instead, we extract the *last* top-level
    # JSON object by streaming parser. The CLI ends with the report so
    # the report is always the last top-level value.
    import json as _json

    decoder = _json.JSONDecoder()
    idx = 0
    payload: dict | None = None
    while idx < len(out):
        # Skip whitespace.
        while idx < len(out) and out[idx] in " \n\r\t":
            idx += 1
        if idx >= len(out):
            break
        if out[idx] != "{":
            # Skip non-object output (e.g. usage lines); the CLI prints
            # at most one object per command, so once we see a non-
            # object character, the last parsed object is the report.
            break
        try:
            obj, end = decoder.raw_decode(out, idx)
        except _json.JSONDecodeError:
            break
        payload = obj
        idx = end
    assert payload is not None, f"no JSON object in stdout: {out!r}"
    assert payload["ok"] is False, f"expected ok=False; got payload={payload!r}"
    assert payload["error_count"] >= 1
    assert payload["failed_sources"]
