"""Pure preview/AST contracts only: no repository imports or real cycles/hooks."""
from __future__ import annotations

import ast
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parents[1]
LEARNING = ROOT / "eimemory/governance/learning/autonomous_learning.py"
CLOSED = ROOT / "eimemory/governance/learning/closed_loop.py"


@dataclass
class SyntheticScope:
    tenant_id: str = "preview-tenant"
    agent_id: str = "preview-agent"
    workspace_id: str = "preview-workspace"
    user_id: str = "preview-user"


class RuntimeAccessForbidden:
    def __getattribute__(self, name):
        raise AssertionError(f"preview accessed runtime: {name}")


def definitions(path):
    return {node.name: node for node in ast.parse(path.read_text()).body if isinstance(node, ast.FunctionDef)}


def pure_namespace():
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("a preview invoked an operational helper")

    namespace = {"__name__": __name__, "ScopeRef": SyntheticScope, "Mapping": Mapping, "Any": Any, "asdict": asdict}
    for name in (
        "collect", "collect_world_signals", "build_self_model", "generate_thoughts",
        "generate_learning_goals", "load_goal_registry", "build_capability_ledger",
        "attribute_capability_outcomes", "_legacy_self_model_capabilities",
        "_resolved_candidate_kind_and_patch", "_proposed_code_patch", "propose_code_patch",
        "run_learning_eval", "run_isolated_evaluator", "_gate_bundle_for_candidate",
        "compact_learning_records", "_network_enabled", "current_release_identity",
        "evaluate_result", "_ingest_feedback_memory", "_safe_rl_update",
    ):
        namespace[name] = forbidden
    nodes = [definitions(LEARNING)[name] for name in ("_as_int", "_run_autonomous_learning_dry_run")]
    nodes += [definitions(CLOSED)["_autonomy_preview_envelope"]]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<extracted-pure-preview>", "exec"), namespace)
    return namespace, calls


class AutonomyPreviewBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.ns, self.calls = pure_namespace()

    def preview(self, **kwargs):
        options = {"scope": SyntheticScope(), "apply": False, "full": True, "max_goals": 3}
        options.update(kwargs)
        return self.ns["_run_autonomous_learning_dry_run"](RuntimeAccessForbidden(), **options)

    def test_contradictory_flags_never_enable_effects(self):
        for apply, network, legacy, full in product((False, True), (None, False, True), (False, True), (False, True)):
            with self.subTest(apply=apply, network=network, legacy=legacy, full=full):
                report = self.preview(apply=apply, allow_network=network, legacy_compatibility=legacy, full=full)
                self.assertIsNone(report["ok"])
                self.assertEqual(report["status"], "not_run")
                self.assertFalse(report["apply"])
                self.assertEqual(report["requested_apply"], apply)
                self.assertFalse(report["executed"])
                self.assertTrue(report["planned"])
                self.assertEqual(report["plan"]["network_requested"], network)
                self.assertFalse(report["network_research"]["enabled"])
                self.assertIsNone(report["promotion"]["ok"])
                self.assertFalse(report["promotion"]["applied"])
                self.assertIsNone(report["regression_watch"]["regressed"])
        self.assertEqual(self.calls, [])

    def test_empty_plan_is_not_success_or_attempted_evaluation(self):
        report = self.preview()
        self.assertEqual(report["goal_count"], 0)
        self.assertEqual(report["selected_goal"], {})
        self.assertEqual(report["active_capability_ids"], [])
        self.assertEqual(report["eval_verdict"], "not_run")
        self.assertEqual(report["candidate_preview"]["patch"], {})
        self.assertEqual(report["attempted_candidate_count"], 0)
        self.assertIsNone(report["ok"])
        self.assertIsNone(report["ledger"]["ok"])
        self.assertEqual(self.calls, [])

    def test_original_dry_run_report_keys_remain(self):
        old_keys = {
            "ok", "loop_id", "loop_record_id", "scope", "dry_run", "apply", "full", "legacy_compatibility",
            "capability_selection", "active_capability_ids", "watch_signal_count", "thought_count", "goal_count",
            "selected_goal_id", "selected_goal", "research_task_count", "network_research", "research_task_ids",
            "research_note_id", "experiment_id", "eval_record_id", "eval_verdict", "candidate_id", "candidate_preview",
            "promotion", "regression_watch", "capability_score_id", "ledger", "retention", "activity_status",
            "activity_reason", "attempted_candidate_count",
        }
        self.assertTrue(old_keys.issubset(self.preview()))

    def test_inputs_are_not_mutated(self):
        scope = SyntheticScope()
        runtime_scope = {"tenant_id": "t", "agent_id": "a", "workspace_id": "w", "user_id": "u"}
        before = deepcopy((scope, runtime_scope))
        report = self.preview(scope=scope, runtime_scope=runtime_scope, apply=True, allow_network=True)
        self.assertEqual((scope, runtime_scope), before)
        report["plan"]["runtime_scope"]["tenant_id"] = "different"
        self.assertEqual((scope, runtime_scope), before)
        preview_before = deepcopy(report)
        wrapped = self.ns["_autonomy_preview_envelope"](report)
        self.assertEqual(report, preview_before)
        self.assertFalse(wrapped["apply"])
        self.assertEqual(self.calls, [])

    def test_planning_exception_does_not_fall_back_to_execution(self):
        class BrokenGoalCount:
            def __int__(self):
                raise RuntimeError("synthetic planning failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic planning failure"):
            self.preview(max_goals=BrokenGoalCount(), apply=True, allow_network=True, legacy_compatibility=True)
        self.assertEqual(self.calls, [])

    def test_goal_request_is_reported_without_claiming_effective_policy(self):
        for value, expected in ((-20, -20), (999, 999), ("invalid", 3), (None, 3)):
            report = self.preview(max_goals=value)
            self.assertEqual(report["plan"]["requested_max_goals"], expected)
            self.assertEqual(report["goal_count"], 0)

    def test_preview_envelope_never_returns_success_placeholders(self):
        source = {"ok": True, "dry_run": True, "apply": True, "promotion": {"ok": True, "applied": True},
                  "regression_watch": {"ok": True, "regressed": False}, "eval_verdict": "pass",
                  "ledger": {"ok": True}, "candidate_preview": {"ok": True}}
        before = deepcopy(source)
        wrapped = self.ns["_autonomy_preview_envelope"](source)
        self.assertEqual(source, before)
        for report in (wrapped, wrapped["cycle"], wrapped["feedback"], wrapped["memory"], wrapped["rl"], wrapped["promotion"], wrapped["ledger"]):
            self.assertIsNone(report["ok"])
            self.assertFalse(report["executed"])
            self.assertEqual(report["status"], "not_run")
        self.assertEqual(wrapped["memory"]["record_id"], "")
        self.assertEqual(wrapped["eval_verdict"], "not_run")
        self.assertEqual(self.calls, [])

    @staticmethod
    def legacy_preview_fixture():
        # Fixed input from the independent batch-1 review, not a generated matrix.
        return {
            "ok": True, "dry_run": True, "apply": True, "eval_verdict": "blocked",
            "activity_status": "active", "activity_reason": "evaluation_gate_failed",
            "attempted_candidate_count": 0,
            "network_research": {
                "enabled": True, "task_count": 1, "hypothesis_count": 1, "error_count": 0,
                "evidence_refs": ["synthetic-web-ref"],
                "output_gate": {
                    "ok": True, "decision": "actionable", "reason": "mapped_to_output_targets",
                    "landing_targets": ["replay"], "summary_record_id": "", "source_score_record_ids": [],
                },
            },
            "promotion": {"ok": True, "applied": False, "dry_run": True},
            "regression_watch": {"ok": True, "regressed": False, "record_id": ""},
        }

    def test_legacy_network_gate_is_not_successful_or_actionable(self):
        source = self.legacy_preview_fixture()
        before = deepcopy(source)
        result = self.ns["_autonomy_preview_envelope"](source)
        gate = result["network_research"]["output_gate"]
        self.assertIsNone(gate["ok"])
        self.assertEqual(gate["status"], "not_run")
        self.assertFalse(gate["executed"])
        self.assertEqual(gate["decision"], "not_run")
        self.assertEqual(gate["landing_targets"], [])
        self.assertEqual(source, before)

    def test_legacy_network_is_disabled_without_execution_evidence(self):
        source = self.legacy_preview_fixture()
        before = deepcopy(source)
        result = self.ns["_autonomy_preview_envelope"](source)
        network = result["network_research"]
        self.assertFalse(network["enabled"])
        self.assertIsNone(network["ok"])
        self.assertEqual(network["status"], "not_run")
        self.assertFalse(network["executed"])
        self.assertEqual(network["task_count"], 0)
        self.assertEqual(network["hypothesis_count"], 0)
        self.assertEqual(network["evidence_refs"], [])
        self.assertEqual(source, before)

    def test_legacy_activity_is_preview(self):
        source = self.legacy_preview_fixture()
        before = deepcopy(source)
        result = self.ns["_autonomy_preview_envelope"](source)
        for report in (result, result["cycle"]):
            self.assertEqual(report["activity_status"], "idle")
            self.assertEqual(report["activity_reason"], "dry_run_preview")
            self.assertEqual(report["attempted_candidate_count"], 0)
        self.assertEqual(source, before)

    def test_requested_plan_patch_and_opaque_values_are_preserved(self):
        class Opaque:
            def __deepcopy__(self, memo):
                raise AssertionError("opaque copy method called")
            def __iter__(self):
                raise AssertionError("opaque iteration called")
            def __getattr__(self, name):
                raise AssertionError("opaque attribute called")

        source = self.legacy_preview_fixture()
        settings = {"ok": True, "enabled": True, "nested": [{"decision": "actionable"}]}
        source["requested"] = deepcopy(settings)
        source["plan"] = {"status": "planned", "network": deepcopy(settings)}
        source["network_research"]["requested"] = deepcopy(settings)
        source["network_research"]["planned"] = deepcopy(settings)
        source["network_research"]["output_gate"]["requested"] = deepcopy(settings)
        source["candidate_preview"] = {"patch": deepcopy(settings)}
        before = deepcopy(source)
        opaque = Opaque()
        source["opaque"] = opaque
        result = self.ns["_autonomy_preview_envelope"](source)
        self.assertIs(result["opaque"], opaque)
        self.assertEqual({key: value for key, value in source.items() if key != "opaque"}, before)
        for key in ("requested", "plan"):
            self.assertEqual(result[key], before[key])
        self.assertEqual(result["network_research"]["requested"], settings)
        self.assertEqual(result["network_research"]["planned"], settings)
        self.assertEqual(result["network_research"]["output_gate"]["requested"], settings)
        self.assertEqual(result["candidate_preview"]["patch"], settings)
        self.assertEqual(self.calls, [])

    def test_pure_renderer_call_graph_contains_no_operational_helpers(self):
        function = definitions(LEARNING)["_run_autonomous_learning_dry_run"]
        allowed = {"asdict", "isinstance", "dict", "max", "min", "_as_int", "bool", "str"}
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                self.assertIsInstance(node.func, ast.Name)
                self.assertIn(node.func.id, allowed)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                self.assertNotEqual(node.value.id, "runtime")

    def test_cycle_preview_branch_precedes_environment_and_runtime_lookup(self):
        function = definitions(LEARNING)["run_autonomous_learning_cycle"]
        self.assertIsInstance(function.body[0], ast.Assign)  # pure scope normalization
        branch = function.body[1]
        self.assertIsInstance(branch, ast.If)
        self.assertEqual(ast.dump(branch.test), ast.dump(ast.Name(id="dry_run", ctx=ast.Load())))
        self.assertEqual(len(branch.body), 1)
        self.assertIsInstance(branch.body[0], ast.Return)
        call = branch.body[0].value
        self.assertEqual(call.func.id, "_run_autonomous_learning_dry_run")
        network = next(kw.value for kw in call.keywords if kw.arg == "allow_network")
        self.assertIsInstance(network, ast.Name)
        self.assertEqual(network.id, "allow_network")

    def test_closed_loop_preview_branch_returns_before_real_controller_and_feedback(self):
        function = definitions(CLOSED)["autonomy_cycle"]
        branch = function.body[0]
        self.assertIsInstance(branch, ast.If)
        self.assertEqual(ast.unparse(branch.test), "kwargs.get('dry_run')")
        self.assertIsInstance(branch.body[-1], ast.Return)
        self.assertEqual(branch.body[-1].value.func.id, "_autonomy_preview_envelope")
        for node in ast.walk(branch):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, {"runtime", "evaluate_result", "_ingest_feedback_memory", "_safe_rl_update"})
        self.assertIsInstance(function.body[1], ast.Assign)
        self.assertEqual(ast.unparse(function.body[1].value.func), "runtime.run_autonomy_cycle")
        self.assertIsInstance(function.body[2], ast.If)  # returned previews are also excluded
        self.assertIsInstance(function.body[2].body[0], ast.Return)


if __name__ == "__main__":
    unittest.main(verbosity=2)
