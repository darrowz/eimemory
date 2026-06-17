"""Tests for the sandbox network proxy (Task 4.2).

Covers the Karpathy Loop plan Task 4.2: a stdlib-only allow-list
checker and a response sanitization helper that strips active content
and caps huge bodies.
"""
from __future__ import annotations

from eimemory.governance.safety.network_proxy import (
    is_allowlisted,
    sanitize_response,
)


def test_sanitize_strips_script_tags() -> None:
    """HTML responses with <script> blocks lose the script, keep the rest."""
    html = "<html><script>alert('xss')</script><p>ok</p></html>"
    out = sanitize_response(html, content_type="text/html")
    assert "<script" not in out
    assert "<p>ok</p>" in out


def test_sanitize_strips_iframe_tags() -> None:
    """HTML responses with <iframe> blocks lose the iframe, keep the rest."""
    html = "<div><iframe src='evil'></iframe><p>ok</p></div>"
    out = sanitize_response(html, content_type="text/html")
    assert "<iframe" not in out
    assert "<p>ok</p>" in out


def test_sanitize_blocks_huge_responses() -> None:
    """Bodies larger than 10 MB are truncated and tagged."""
    big = "x" * (11 * 1024 * 1024)  # 11 MB
    out = sanitize_response(big, content_type="text/plain")
    assert len(out) <= 10 * 1024 * 1024 + 100
    assert "[TRUNCATED]" in out


def test_sanitize_keeps_short_responses_intact() -> None:
    """Plain-text bodies under the cap pass through unchanged."""
    body = "hello world"
    out = sanitize_response(body, content_type="text/plain")
    assert out == body


def test_allowlist_default() -> None:
    """Default allow-list admits the configured hosts and rejects others."""
    assert is_allowlisted("https://api.github.com/repos/foo/bar") is True
    assert is_allowlisted("https://raw.githubusercontent.com/x/y") is True
    assert is_allowlisted("https://arxiv.org/abs/1234") is True
    assert is_allowlisted("https://example.com/anything") is False


def test_allowlist_custom_overrides_default() -> None:
    """A custom allow-list replaces the default and rejects GitHub."""
    custom = ["internal.example"]
    assert is_allowlisted("https://internal.example/x", allowlist=custom) is True
    assert is_allowlisted("https://api.github.com/x", allowlist=custom) is False
