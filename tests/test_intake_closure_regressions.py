from __future__ import annotations

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler.jobs import _run_paper_candidate_promotion


SCOPE = {"tenant_id": "audit", "agent_id": "main", "workspace_id": "papers", "user_id": "alice"}


def _candidate(runtime, suffix="1"):
    text = "Memory retrieval must validate source provenance before returning operational guidance."
    record = RecordEnvelope.create(
        kind="knowledge_candidate", title=f"Memory paper {suffix}", summary=text, detail=text,
        scope=ScopeRef.from_dict(SCOPE), status="candidate",
        content={"source_kind": "arxiv", "title": f"Memory paper {suffix}",
                 "uri": f"https://arxiv.org/abs/2601.0000{suffix}",
                 "summary": text, "content_excerpt": text, "status": "candidate"},
    )
    runtime.store.append(record)
    return record


@pytest.mark.parametrize("status", ["deprecated", "merged", "promoted"])
@pytest.mark.parametrize("as_payload", [False, True])
def test_paper_promotion_respects_persisted_terminal_status(tmp_path, status, as_payload):
    with Runtime.create(root=tmp_path) as runtime:
        stale = _candidate(runtime)
        current = runtime.store.get_by_id(stale.record_id, scope=SCOPE)
        current.status = status
        runtime.store.append(current)

        request = {**stale.content, "record_id": stale.record_id} if as_payload else stale
        report = runtime.promote_paper_candidate(request, scope=SCOPE)

        assert report["ok"] is False
        assert runtime.store.get_by_id(stale.record_id, scope=SCOPE).status == status
        assert runtime.store.list_records(kinds=["paper_source", "claim_card", "knowledge_page"], scope=SCOPE) == []


@pytest.mark.parametrize("status", ["deprecated", "merged", "promoted"])
def test_paper_payload_cannot_promote_terminal_status(tmp_path, status):
    with Runtime.create(root=tmp_path) as runtime:
        candidate = _candidate(runtime)
        payload = {**candidate.content, "status": status}

        report = runtime.promote_paper_candidate(payload, scope=SCOPE)

        assert report["ok"] is False
        assert runtime.store.list_records(kinds=["paper_source"], scope=SCOPE) == []


@pytest.mark.parametrize("include_success", [False, True])
def test_scheduled_paper_promotion_preserves_candidate_failures(tmp_path, monkeypatch, include_success):
    with Runtime.create(root=tmp_path) as runtime:
        failed = _candidate(runtime)
        if include_success:
            _candidate(runtime, "2")
        original = runtime.promote_paper_candidate

        def promote(record, *, scope=None):
            if record.record_id == failed.record_id:
                raise RuntimeError("transient parser failure")
            return original(record, scope=scope)

        monkeypatch.setattr(runtime, "promote_paper_candidate", promote)
        report = _run_paper_candidate_promotion(runtime, scope=SCOPE, candidate_records=[])

        assert report["ok"] is False
        assert report["attempted_count"] == 1 + int(include_success)
        assert report["promoted_count"] == int(include_success)
        assert report["error_count"] == 1
        assert report["errors"] == [{"record_id": failed.record_id, "error_type": "RuntimeError"}]
        assert report["reasons"]["promotion_exception"] == 1
