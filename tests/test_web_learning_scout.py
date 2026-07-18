from __future__ import annotations

from urllib.error import URLError

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.web_learning import scout_web_learning


def test_web_learning_scout_emits_hypotheses_without_applying_policy(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    report = scout_web_learning(
        runtime,
        scope=scope,
        evidence=[
            {
                "url": "https://example.com/rag-rerank",
                "title": "Hybrid retrieval and reranking",
                "text": "Production RAG systems often reduce noisy retrieval by using hybrid retrieval and reranking.",
            }
        ],
    )

    assert report["ok"] is True
    assert report["hypothesis_count"] == 1
    assert report["hypotheses"][0]["source"] == "web_scout"
    assert report["hypotheses"][0]["risk_level"] == "medium"
    assert "candidate_policy" in report["hypotheses"][0]
    assert report["hypotheses"][0]["replay_hints"]
    assert report["hypotheses"][0]["source_url"] == "https://example.com/rag-rerank"

    assert runtime.search_policy("hybrid retrieval", scope=scope)["policy_suggestions"] == []

    reflections = runtime.store.list_records(kinds=["reflection"], scope=scope, limit=5)
    assert len(reflections) == 1
    assert reflections[0].source == "eimemory.web_learning_scout"
    assert reflections[0].meta["report_type"] == "web_learning_scout"
    assert report["hypotheses"][0]["evidence_record_id"] == reflections[0].record_id


def test_web_learning_scout_does_not_leak_candidate_policies_into_policy_search(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    scout_web_learning(
        runtime,
        scope=scope,
        evidence=[
            {
                "url": "https://example.com/openclaw",
                "title": "OpenClaw replay hints",
                "text": "Prefer restart-after-checking logs and status when OpenClaw is stuck.",
            }
        ],
    )

    assert runtime.search_policy("OpenClaw stuck", scope=scope)["policy_suggestions"] == []


def test_web_learning_scout_records_fetch_errors_without_crashing(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    def failing_open(*_args, **_kwargs):
        raise URLError("simulated fetch failure")

    monkeypatch.setattr("eimemory.governance.web_learning.safe_urlopen", failing_open)

    report = scout_web_learning(
        runtime,
        scope=scope,
        urls=["https://example.com/unstable"],
    )

    assert report["errors"]
    assert report["errors"][0]["url"] == "https://example.com/unstable"
    assert "simulated fetch failure" in report["errors"][0]["detail"]
    # ok true/false is acceptable when fetch fails, but report must contain errors and still return safely.
    assert isinstance(report["ok"], bool)
    assert runtime.search_policy("hybrid retrieval", scope=scope)["policy_suggestions"] == []


def test_web_learning_scout_uses_urlopen_timeout_keyword(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    captured: dict = {}

    class FakeHeaders:
        def get_content_type(self) -> str:
            return "text/plain"

        def get_content_charset(self):
            return "utf-8"

        def get(self, _key: str, _default=None):
            return ""

    class FakeResponse:
        headers = FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb) -> None:
            return None

        def read(self, _size: int) -> bytes:
            return b"Hybrid retrieval reduces noisy memory recall."

    def fake_open(_url: str, *, timeout: int, headers: dict):
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("eimemory.governance.web_learning.safe_urlopen", fake_open)

    report = scout_web_learning(runtime, scope=scope, urls=["https://example.com/retrieval"], timeout_seconds=7)

    assert report["ok"] is True
    assert report["hypothesis_count"] == 1
    assert captured["headers"]["User-Agent"] == "eimemory.web-learning/1.0"
    assert captured["timeout"] == 7


def test_web_learning_scout_blocks_private_network_urls(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    report = scout_web_learning(
        runtime,
        scope=scope,
        urls=["http://127.0.0.1/internal"],
    )

    assert report["errors"]
    assert report["errors"][0]["error"] == "UnsafeURL"
    assert "unsafe fetch URL host" in report["errors"][0]["detail"]
    assert report["hypothesis_count"] == 0


@pytest.mark.parametrize("url", ["http://2130706433/internal", "http://127.0.0.1.nip.io/internal"])
def test_web_learning_scout_blocks_private_network_aliases(tmp_path, monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}

    def fake_getaddrinfo(*_args, **_kwargs):
        return [(None, None, None, "", ("127.0.0.1", 80))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    report = scout_web_learning(runtime, scope=scope, urls=[url])

    assert report["errors"]
    assert "unsafe fetch URL host" in report["errors"][0]["detail"]
    assert report["hypothesis_count"] == 0
