"""Host notices are not recall requests and never become empty decisions."""
from __future__ import annotations

import pytest

from eimemory.retrieval.proactive import _is_host_notification

from tests.test_proactive_recall import BASE_SCOPE, _record, _service


@pytest.mark.parametrize("query", [
    "[System note: The previous turn was interrupted by a gateway shutdown.]",
    "  [IMPORTANT: Background process proc_abc completed normally (exit code 0).]",
])
def test_host_notifications_bypass_retrieval(tmp_path, query):
    assert _is_host_notification(query)
    runtime, engine, service = _service(tmp_path, [_record("anything")])
    decision = service.decide(channel="codex", scope=BASE_SCOPE, source_ids=["alpha"],
                              session_id="s", query_id="q", query=query)
    assert engine.requests == []
    assert decision["items"] == []
    assert decision.get("bypassed") is True
    assert any(row["reason"] == "host_notification" for row in service.bypass_diagnostics())
    runtime.close()


@pytest.mark.parametrize("query", [
    "[The user sent an image~ Here's what I can see: a company page]",
    "What did the system note say yesterday?",
    "important: background process notes for the project",
])
def test_user_requests_are_not_host_notifications(query):
    assert not _is_host_notification(query)
