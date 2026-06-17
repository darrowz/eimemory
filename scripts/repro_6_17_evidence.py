"""Step 5 of Task 1.3 — real reproduction of the 6/17 evidence.

Verifies that ``run_learning_eval`` returns verdict='fail' when eval_suite
contains mostly gate=blocked evidence, even if the candidate's own scores
look fine. Run as: ``python scripts/repro_6_17_evidence.py``.
"""
from __future__ import annotations

import tempfile

from eimemory.api.runtime import Runtime
from eimemory.governance.learning_eval import run_learning_eval


def main() -> int:
    realistic_sixteen_june_evidence = {
        "gates": [
            {"name": "recall_quality_gate", "outcome": "blocked", "blocked_reason": "recall_quality_gate_failed"},
            {"name": "replay_gate", "outcome": "blocked", "blocked_reason": "negative_replay_signal"},
            {"name": "safe_action_gate", "outcome": "blocked", "blocked_reason": "destructive_change"},
            {"name": "trusted_gate", "outcome": "blocked", "blocked_reason": "trusted_gate_reject"},
            {"name": "audit_gate", "outcome": "ok", "blocked_reason": ""},
            {"name": "gate_bundle_missing", "outcome": "blocked", "blocked_reason": "gate_bundle_missing"},
            {"name": "rollback_gate", "outcome": "blocked", "blocked_reason": "rollback_not_documented"},
        ],
        "scores": {
            "capability": 0.9,
            "safety": 1.0,
            "regression": 1.0,
            "cost": 0.85,
            "evidence": 0.9,
            "maintainability": 0.85,
            "confidence": 0.9,
        },
    }
    blocked_count = sum(1 for g in realistic_sixteen_june_evidence["gates"] if g["outcome"] == "blocked")
    total = len(realistic_sixteen_june_evidence["gates"])
    rate = blocked_count / total
    print(f"6/17 evidence: gate_blocked_rate={blocked_count}/{total}={rate:.3f}")

    tmp = tempfile.mkdtemp(prefix="eimemory_repro_6_17_")
    try:
        runtime = Runtime.create(root=tmp)
        result = run_learning_eval(
            runtime,
            {"candidate_id": "cand_6_17_evidence", "authority_tier": "L1", "source_record_ids": ["rec_1"]},
            scope={"agent_id": "hongtu"},
            loop_id="karpathy_loop_6_17_repro",
            eval_suite=realistic_sixteen_june_evidence,
        )
        # Best-effort close before printing
        try:
            if hasattr(runtime, "store") and hasattr(runtime.store, "close"):
                runtime.store.close()
        except Exception:
            pass
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"verdict: {result['verdict']}")
    print(f"ok: {result['ok']}")
    print(f"blocked_reasons: {result['blocked_reasons']}")
    print(f"record_id: {result.get('record_id', '<none>')}")
    assert result["verdict"] == "fail", f"FAIL: expected fail, got {result['verdict']}"
    assert any(r.startswith("gate_blocked_rate_exceeded") for r in result["blocked_reasons"]), (
        f"FAIL: expected gate_blocked_rate_exceeded in blocked_reasons, got {result['blocked_reasons']}"
    )
    print()
    print("=== STEP 5 RESULT: real 6/17 evidence reproduction produces verdict=fail ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
