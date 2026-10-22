"""Expanded proactive recall falls back to the literal request when empty."""
from __future__ import annotations

from eimemory.models.records import RecallBundle
from eimemory.retrieval.proactive import ProactiveRecallService

from tests.test_proactive_recall import BASE_SCOPE, FixedRecallEngine, _record, _service


class LiteralOnlyEngine(FixedRecallEngine):
    """Returns records only for queries without appended context entities."""

    def recall(self, request) -> RecallBundle:
        if "Context entities:" in request.query:
            self.requests.append(request)
            return RecallBundle(items=[], rules=[], reflections=[], confidence=0.0,
                                next_action_hint="", explanation={"retrieval_status": "no_evidence"})
        return super().recall(request)


def _with_history(service):
    service.complete_turn(
        channel="codex", scope=BASE_SCOPE, source_ids=["alpha"], session_id="s",
        turn_id="t0", user_summary="Project Zephyr background job", assistant_summary="Done",
    )


def test_literal_fallback_recovers_evidence_starved_by_context(tmp_path):
    record = _record("The deployment model is gpt-6-luna")
    runtime, _engine, service = _service(tmp_path, [record])
    engine = LiteralOnlyEngine([record])
    runtime.memory.recall_engine = engine
    engine.bind(runtime.memory)
    _with_history(service)
    decision = service.decide(channel="codex", scope=BASE_SCOPE, source_ids=["alpha"],
                              session_id="s", query_id="q1", query="Which model is in use")
    queries = [request.query for request in engine.requests]
    assert len(queries) == 2
    assert "Context entities:" in queries[0]
    assert queries[1] == "Which model is in use"
    assert [item["record_id"] for item in decision["items"]] == [record.record_id]
    runtime.close()


def test_no_literal_fallback_when_expanded_pass_has_evidence(tmp_path):
    record = _record("Project Zephyr uses PostgreSQL")
    runtime, engine, service = _service(tmp_path, [record])
    _with_history(service)
    service.decide(channel="codex", scope=BASE_SCOPE, source_ids=["alpha"],
                   session_id="s", query_id="q1", query="Recall the storage decision")
    assert len(engine.requests) == 1
    assert "zephyr" in engine.requests[0].query.casefold()
    runtime.close()


def test_context_cannot_impose_requested_attribute():
    query = "1.15.2 / c32b00285"
    expanded = ProactiveRecallService._recall_query(query, ["部署 验收 已完成"])
    assert expanded == query
    plain = ProactiveRecallService._recall_query("continue the migration", ["Project Zephyr"])
    assert "Context entities:" in plain
