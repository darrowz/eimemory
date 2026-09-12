"""Reproduce learning-closure audit findings without touching operator data.

Run from any directory: python <repo>/docs/audit/business-closure-2026-09-12/repro_learning.py
All runtime databases and input files live in TemporaryDirectory. No network,
deployment, live root, or product code is modified. Assertions describe the
observed defects, so a repaired implementation is expected to fail them.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from eimemory.api.runtime import Runtime
from eimemory.capabilities import CapabilityBinding, CapabilityDefinition, CapabilityRevision
from eimemory.cli.main import dispatch
from eimemory.governance.capability_attribution import normalize_explicit_capability_outcomes
from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.promotion_manager import promote_candidate
from eimemory.governance.sandbox_lab import create_sandbox_experiment
from eimemory.models.records import ScopeRef


SCOPE = ScopeRef(tenant_id="audit", agent_id="audit", workspace_id="audit", user_id="audit")
SCOPE_DICT = {name: getattr(SCOPE, name) for name in ("tenant_id", "agent_id", "workspace_id", "user_id")}
STAMP = "2026-09-12T00:00:00+00:00"


@contextlib.contextmanager
def isolated_runtime():
    keys = ("EIMEMORY_CONFIG_DIR", "EIMEMORY_CONFIG_PATH", "EIMEMORY_ROOT", "OPENCLAW_LOOP_HOME")
    previous = {key: os.environ.get(key) for key in keys}
    with TemporaryDirectory(prefix="eimemory-closure-audit-") as directory:
        root = Path(directory)
        for key in keys:
            os.environ.pop(key, None)
        os.environ["EIMEMORY_ROOT"] = str(root / "runtime")
        os.environ["OPENCLAW_LOOP_HOME"] = str(root / "openclaw-loop")
        runtime = Runtime.create(root=root / "runtime")
        try:
            yield runtime, root
        finally:
            runtime.close()
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def trace_payload(trace_id: str, *, passed: bool) -> dict:
    return {
        "trace_id": trace_id,
        "idempotency_key": f"idem-{trace_id}",
        "task_type": "repo.edit",
        "input_summary": "Audit deterministic task assertion",
        "outcome": {"status": "success" if passed else "failed"},
        "verifier": {"passed": passed},
        "feedback": {"summary": "deterministic assertion result"},
    }


def duplicate_cli_reward() -> dict:
    with isolated_runtime() as (runtime, root):
        payload_path = root / "outcome.json"
        payload_path.write_text(json.dumps(trace_payload("duplicate", passed=True)), encoding="utf-8")
        parsed = SimpleNamespace(experience_command="outcome", json_path=str(payload_path))
        results = []
        for _ in range(2):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = dispatch("experience", parsed, runtime, SCOPE_DICT)
            assert exit_code == 0
            results.append(json.loads(output.getvalue()))
        first, retry = results
        raw = [r for r in runtime.store.list_records(kinds=["reflection"], scope=SCOPE, limit=100)
               if r.meta.get("report_type") == "outcome_trace"]
        transitions = runtime.store.list_records(kinds=["rl_transition"], scope=SCOPE, limit=100)
        values = [r["closed_loop"]["rl"]["policy_update"]["value"] for r in results]
        assert first["record_id"] == retry["record_id"] and retry["idempotent"] is True
        assert len(raw) == 1 and len(transitions) == 2 and values == [0.25, 0.5]
        return {"raw_count": len(raw), "retry_idempotent": retry["idempotent"],
                "rl_transition_count": len(transitions), "policy_values": values}


def passing_policy_eval() -> dict:
    # Same bounded, simulated passing evaluation fixture as test_promotion_watch;
    # this audit exercises observation after promotion, not the evaluator itself.
    return {
        "verdict": "pass",
        "scores": {"capability": 0.9, "safety": 1.0, "regression": 1.0, "cost": 0.8, "evidence": 1.0},
        "gate_bundle": {
            "evidence": [{"tier": "T0", "ref": "audit-event", "summary": "Audit fixture"}],
            "rollback": {"available": True, "executable": True},
            "canary": {"passed": True, "blast_radius": "single_scope"},
            "closed_loop": {"doctor": {"ok": True}, "smoke": {"ok": True}},
            "timeout_seconds": 300, "audit": {"enabled": True},
            "prompt_shadow_eval": {"passed": True}, "prompt_injection_check": {"passed": True},
        },
    }


def ignored_negative_observations() -> dict:
    with isolated_runtime() as (runtime, _):
        pattern_id = "audit-shadow"
        experiment_id = create_sandbox_experiment(
            runtime, scope=SCOPE, loop_id="audit", learning_goal_id="audit-goal",
            research_note_id="audit-note", candidate_kind="prompt_policy",
            candidate_patch={"id": pattern_id, "pattern": "post promotion hit sample",
                             "default_event_type": "tool_routing", "interpreted_intent": "Audit route",
                             "execution_policy": ["Use the audited route."],
                             "success_criteria": "Three observations without regression."},
        )
        candidate_id = distill_capability_candidate(
            runtime, scope=SCOPE, loop_id="audit", experiment_id=experiment_id,
            eval_result=passing_policy_eval(), promotion_target="prompt_policy",
            summary="Audit policy", target_capability="tool.routing",
        )
        promoted = promote_candidate(runtime, candidate_id=candidate_id, scope=SCOPE,
                                     loop_id="audit", eval_result=passing_policy_eval(), health={"ok": True})
        assert promoted["post_promotion_status"] == "shadow_observe"
        ignored = 0
        for index, passed in enumerate([False, True, False, True, False, True]):
            attribution = {"policy_suggestion_ids": [pattern_id]}
            event = runtime.record_event(
                {"id": f"audit-event-{index}", "policy_attribution": attribution,
                 "source": "codex.stop", "user_phrase": "post promotion hit sample",
                 "event_type": "tool_routing", "interpreted_intent": "Audit route",
                 "goal": "Audit observation", "confidence": 0.9}, scope=SCOPE_DICT,
            )
            outcome = runtime.record_outcome(
                event["id"], {"outcome": "good" if passed else "bad", "verifier": {"passed": passed},
                              "reason": "deterministic task assertion", "policy_attribution": attribution},
                scope=SCOPE_DICT,
            )
            ignored += int(not passed and "post_promotion_watch" not in outcome)
        row = runtime.store.sqlite.conn.execute("SELECT payload_json FROM intent_patterns WHERE id=?", (pattern_id,)).fetchone()
        pattern = json.loads(row[0])
        watch = pattern["post_promotion_watch"]
        assert ignored == 3 and pattern["status"] == "active"
        assert watch["observed_count"] == 3 and watch["failure_count"] == 0
        return {"actual_failures": 3, "actual_total": 6, "ignored_failures": ignored,
                "pattern_status": pattern["status"], "recorded_observations": watch["observed_count"],
                "recorded_failure_rate": watch["failure_rate"]}


def seed_capability(runtime: Runtime) -> dict:
    definition = CapabilityDefinition(
        capability_id="audit.execution", display_name="Audit execution", description="Bounded audit task",
        owner="audit", risk_tier="bounded_write", tags=("audit",), provenance={"source": "audit"}, created_at=STAMP,
    )
    revision = CapabilityRevision(
        revision_id="audit.execution:v1", capability_id=definition.capability_id,
        contract={"input_schema": {"type": "object"}, "output_schema": {"type": "object"},
                  "success_invariants": ["assertion_passes"], "failure_invariants": ["assertion_fails"],
                  "evidence_requirements": {"minimum_refs": 1}, "dependencies": [], "composition": [],
                  "risk_tier": "low", "side_effect_class": "none"},
        compatibility="incompatible", provenance={"source": "audit"}, created_at=STAMP,
    )
    binding = CapabilityBinding(
        binding_id="audit.binding:v1", capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id, provider_kind="module", provider_instance_id="audit-runtime",
        implementation_digest="a" * 64, operations=("run",), limits={"max_tasks": 1},
        environment_fingerprint={"runtime": "isolated"}, applicability={"scope": "global"},
        advertisement_evidence_refs=("artifact://audit/advertisement.json",), provenance={"source": "audit"}, created_at=STAMP,
    )
    def seed(repository):
        repository.register_definition(definition, scope=SCOPE)
        repository.register_revision(revision, scope=SCOPE)
        repository.register_binding(binding, scope=SCOPE)
    runtime.store.mutate_capabilities_atomically(seed)
    return {"capability_id": definition.capability_id, "capability_revision_id": revision.revision_id,
            "provider_binding_id": binding.binding_id, "idempotency_key": "audit-observation",
            "observed_at": STAMP, "evidence_refs": ["artifact://audit/evidence.json"],
            "environment_fingerprint": {"runtime": "isolated"}, "provenance": {"source": "audit"}}


def attributed_payload(attribution: dict, *, trace_id: str) -> dict:
    return {**trace_payload(trace_id, passed=True), "capability_attribution": attribution,
            "verifier": {"passed": True, "independent": True, "id": "audit-verifier",
                         "revision": "v1", "contract_digest": "b" * 64}}


def capability_projection_drift() -> dict:
    with isolated_runtime() as (runtime, _):
        attribution = seed_capability(runtime)
        initial = runtime.record_outcome_trace(attributed_payload(attribution, trace_id="normal"), scope=SCOPE)
        recovery = normalize_explicit_capability_outcomes(runtime, scope=SCOPE)
        assert initial["capability_dual_write"]["status"] == "aligned"
        assert recovery["ok"] is False and recovery["blocked"] == 1
        assert recovery["details"][0]["reason"] == "CapabilityIdempotencyConflict"
        result = {"initial": "aligned", "reconciliation": recovery}
    with isolated_runtime() as (runtime, _):
        attribution = seed_capability(runtime)
        first = runtime.record_outcome_trace(trace_payload("changed", passed=False), scope=SCOPE)
        retry = runtime.record_outcome_trace(attributed_payload(attribution, trace_id="changed"), scope=SCOPE)
        raw = runtime.store.get_by_id(first["record_id"], scope=SCOPE)
        observation = runtime.store.sqlite.conn.execute("SELECT verdict, provenance_json FROM capability_observations").fetchone()
        assert first["record_id"] == retry["record_id"] and retry["idempotent"] is True
        assert raw.content["payload"]["verifier"]["passed"] is False
        assert observation["verdict"] == "pass" and retry["capability_dual_write"]["status"] == "aligned"
        assert json.loads(observation["provenance_json"])["outcome_trace_record_id"] == first["record_id"]
        result["changed_retry"] = {"raw_outcome": raw.content["payload"]["outcome"],
                                   "observation_verdict": observation["verdict"], "dual_write": "aligned"}
    return result


if __name__ == "__main__":
    print(json.dumps({"duplicate_cli_reward": duplicate_cli_reward(),
                      "ignored_negative_observations": ignored_negative_observations(),
                      "capability_projection_drift": capability_projection_drift()}, ensure_ascii=False, indent=2))
