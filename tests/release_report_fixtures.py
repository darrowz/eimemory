"""Synthetic test data only. Never persist this as production evidence."""
from copy import deepcopy
from hashlib import sha256

from eimemory.governance.release.closure_contracts import (
    LIVE_ACCEPTANCE_CASE_IDS, LEGACY_RELEASE_CASE_IDS,
)


def receipt():
    return {"ok": True, "commit": "a" * 40, "version": "1.9.82",
            "release_path": "/opt/eimemory/releases/" + "a" * 40,
            "promotion_request_id": "receipt-1", "release_session_id": "receipt-1"}


def replay():
    capabilities = ("search.discovery", "research.synthesis", "operations.uumit", "device.control")
    rows = [{"case_id": case, "capability": capabilities[i // 3],
             "probe_id": f"probe-{i}", "probe_record_id": f"probe-{i}",
             "trace_id": f"trace-{i}", "trace_record_id": f"trace-record-{i}",
             "passed": True, "validator_passed": True, "trace_emitted": True,
             "persisted": True, "error": ""}
            for i, case in enumerate(LEGACY_RELEASE_CASE_IDS)]
    acceptance = {"ok": True, "all_passed": True, "case_count": len(rows),
                  "pass_count": len(rows), "failed_count": 0, "trace_count": len(rows),
                  "execution_id": "fixture-execution", "persisted": True,
                  "distinct_probe_sources": True, "distinct_trace_ids": True, "results": rows}
    packs = []
    for capability in capabilities:
        selected = [r for r in rows if r["capability"] == capability]
        packs.append({"capability": capability, "pass_rate": 1.0,
                      "cases": [{"case_id": r["case_id"], "threshold": 1.0} for r in selected],
                      "case_results": [{"case_id": r["case_id"], "verdict": "pass",
                                        "evidence_source_id": r["probe_id"],
                                        "probe_source_id": r["probe_record_id"],
                                        "trace_record_id": r["trace_record_id"], "trace_id": r["trace_id"]}
                                       for r in selected]})
    return {"ok": True, "legacy_compatibility": True, "capability_acceptance": acceptance,
            "capability_replay": {"ok": True, "packs": packs, "manifest_record_id": "manifest-1",
                                  "persisted_replay_count": len(rows),
                                  "persisted_replay_ids": [f"replay-{i}" for i in range(len(rows))]}}


def live():
    identity = receipt()
    rows = []
    for i, case in enumerate(LIVE_ACCEPTANCE_CASE_IDS):
        digest = sha256(f"fixture-observation-{i}".encode()).hexdigest()
        rows.append({"case_id": case, "task_type": f"live.acceptance.{case}", "passed": True,
                     "trace_persisted": True, "record_id": f"live-{i}", "observation_digest": digest,
                     "trace_id": f"live-acceptance:{identity['commit']}:{case}:{digest[:12]}"})
    return {"ok": True, "case_count": len(rows), "pass_count": len(rows), "fail_count": 0,
            "distinct_task_types": len(rows), "deployment": identity, "cases": rows}


def accumulating_report():
    identity = receipt()
    binding = {"release_commit": identity["commit"], "release_version": identity["version"],
               "deployment_receipt_id": identity["promotion_request_id"],
               "release_session_id": identity["release_session_id"]}
    pending = {"ok": True, "status": "bootstrap_data_pending", "record_id": "bootstrap-pending-current",
               "release_identity": binding}
    return {"report_type": "l5_release_closure", "ok": True, "closure_complete": False,
            "data_accumulating": True, "deployment": deepcopy(identity), "deployment_receipt": identity,
            "storage_migrations": {"ok": True}, "blocked_stage": "", "blocked_reason": "",
            "production_recall_gate": {"ok": False, "status": "data_accumulating", "bootstrap": deepcopy(pending)},
            "bootstrap_pending_verification": deepcopy(pending), "replay_bootstrap": replay(),
            "live_acceptance": live(),
            "channel_acceptance": {"ok": True, "record_id": "channel-current", "evidence_class": "external_channel_receipt"},
            "closure_rehearsal": {"ok": True, "closure_complete": False, "data_accumulating": True,
                                  "bootstrap_pending_verification": deepcopy(pending)},
            "readiness": {"ok": True, "schema_version": "l5_readiness.v2", "current_stage": "L4.5",
                          "readiness_score": 0.8, "release_identity": deepcopy(binding)}}


def complete_report():
    report = accumulating_report()
    report.update(closure_complete=True, data_accumulating=False)
    report["production_recall_gate"] = {"ok": True, "status": "accepted"}
    report["production_recall_strict_state"] = {"ok": True, "status": "strict_activated", "candidate_commit": "a" * 40}
    report["closure_rehearsal"] = {"ok": True, "closure_complete": True, "data_accumulating": False}
    report["readiness"].update(current_stage="L5", readiness_score=1.0)
    return report


def wait_report():
    report = complete_report()
    report.update(ok=False, closure_complete=False, data_accumulating=False,
                  blocked_stage="channel_acceptance", blocked_reason="current_release_channel_receipt_not_found")
    report["channel_acceptance"] = {"ok": False, "error": "current_release_channel_receipt_not_found"}
    report["pending_checkpoint"] = {"ok": True, "status": "waiting_for_channel_acceptance"}
    return report


def promotion_health_receipt():
    """Operator-supplied synthetic release health; exercise the real gate.

    Exact commit/receipt/session identify this test release. ``fresh`` is the
    explicit collector freshness marker accepted by the promotion contract.
    This data is never production deployment or health evidence.
    """
    return {**receipt(), "fresh": True}
