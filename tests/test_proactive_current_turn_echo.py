"""The request's own just-captured turn record is not recalled evidence."""
from __future__ import annotations

from tests.test_proactive_recall import BASE_SCOPE, _record, _service

QUERY = "中能洛阳控股有限公司的股东是谁，帮我查一下"


def test_fresh_capture_of_request_is_not_delivered(tmp_path):
    echo = _record(f"Hermes completed turn user: {QUERY} assistant: 好的")
    older = _record("中能洛阳控股由周口国控持股")
    older.time.created_at = older.time.updated_at = "2026-01-01T00:00:00Z"
    runtime, _engine, service = _service(tmp_path, [echo, older])
    decision = service.decide(channel="codex", scope=BASE_SCOPE, source_ids=["alpha"],
                              session_id="s", query_id="q", query=QUERY)
    delivered = [item["record_id"] for item in decision["items"]]
    assert echo.record_id not in delivered
    assert older.record_id in delivered
    runtime.close()


def test_old_record_quoting_request_is_kept(tmp_path):
    quoted = _record(f"Hermes completed turn user: {QUERY}")
    quoted.time.created_at = quoted.time.updated_at = "2026-01-01T00:00:00Z"
    runtime, _engine, service = _service(tmp_path, [quoted])
    decision = service.decide(channel="codex", scope=BASE_SCOPE, source_ids=["alpha"],
                              session_id="s", query_id="q", query=QUERY)
    assert [item["record_id"] for item in decision["items"]] == [quoted.record_id]
    runtime.close()


def test_fresh_durable_fact_restating_request_is_kept(tmp_path):
    fact = _record(QUERY)
    runtime, _engine, service = _service(tmp_path, [fact])
    decision = service.decide(channel="codex", scope=BASE_SCOPE, source_ids=["alpha"],
                              session_id="s", query_id="q", query=QUERY)
    assert [item["record_id"] for item in decision["items"]] == [fact.record_id]
    runtime.close()
