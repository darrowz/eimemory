"""Offline counterexamples for the 2026-09-12 audit; no production data is used.

Run from the repository root: python docs/audit/business-closure-2026-09-12/repro_intake.py
Assertions describe the defects at commit 73682bc, not desired behavior.
"""
from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from eimemory import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler.jobs import _run_paper_candidate_promotion


def main():
    scope = {"tenant_id": "audit", "agent_id": "main", "workspace_id": "demo"}
    body = (
        "Memory retrieval must validate source provenance before returning operational guidance. "
        "This paper describes a repeatable verification procedure for the runtime."
    )
    with TemporaryDirectory(prefix="eim-audit-intake-") as root:
        runtime = Runtime.create(root=root)
        try:
            def candidate(title, uri):
                record = RecordEnvelope.create(
                    kind="knowledge_candidate", title=title, summary=body, detail=body,
                    scope=ScopeRef.from_dict(scope), status="candidate",
                    content={"source_kind": "arxiv", "title": title, "uri": uri,
                             "summary": body, "content_excerpt": body, "decision": "candidate"},
                )
                runtime.store.append(record)
                return record

            record = candidate("Audit memory paper", "https://arxiv.org/abs/2601.00001")
            runtime.review_intake_candidate(
                record_id=record.record_id, decision="deprecate", reviewer="auditor", scope=scope,
            )
            current = runtime.store.get_by_id(record.record_id, scope=scope)
            assert current.status == "deprecated"
            report = runtime.promote_paper_candidate(current, scope=scope)
            after = runtime.store.get_by_id(record.record_id, scope=scope).status
            assert report["ok"] is True and after == "promoted"
            assert report["compiled_record_count"] > 0
            print(json.dumps({"finding": "deprecated_paper_is_promoted", "before": "deprecated",
                              "after": after, "compiled_records": report["compiled_record_count"]}))

            candidate("Second audit paper", "https://arxiv.org/abs/2601.00002")
            def fail_promote(*args, **kwargs):
                raise RuntimeError("injected transient parser failure")
            runtime.promote_paper_candidate = fail_promote
            direct = runtime.promote_collected_paper_candidates(scope=scope, auto=True)
            scheduled = _run_paper_candidate_promotion(runtime, scope=scope, candidate_records=[])
            assert direct["error_count"] == 1
            assert scheduled["ok"] is True and scheduled["error_count"] == 0
            assert scheduled["errors"] == [] and scheduled["promoted_count"] == 0
            print(json.dumps({"finding": "scheduler_discards_promotion_failure",
                              "direct_error_count": direct["error_count"],
                              "scheduled_ok": scheduled["ok"],
                              "scheduled_error_count": scheduled["error_count"],
                              "scheduled_errors": scheduled["errors"]}))
        finally:
            runtime.close()


if __name__ == "__main__":
    main()
