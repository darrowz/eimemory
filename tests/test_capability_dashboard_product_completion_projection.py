"""Local dashboard return-contract regression tests, without project imports.

Only the two named function bodies are compiled unchanged from their ASTs.
Every project/runtime boundary used by them is an explicit in-memory stub.
These tests establish report values and call arguments, not readiness truth.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "eimemory/governance/capability/capability_dashboard.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
FUNCTION_NAMES = {"build_capability_dashboard_metrics", "build_product_completion_dashboard"}
FUNCTIONS = {node.name: node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTION_NAMES}
BASE_KEYS = {
    "recall_hit_rate", "user_correction_rate", "task_success_rate",
    "verified_live_task_success_rate", "verified_real_task_success_rate",
    "current_deployment_live_task_success_rate",
    "current_deployment_verified_real_task_success_rate",
    "patch_candidate_validity_rate", "patch_deployment_success_rate",
    "patch_promotion_success_rate", "auto_patch_success_rate", "rollback_count",
    "skill_reuse_count",
}
TOP_KEYS = {
    "ok", "report_type", "scope", "metrics", "metric_quality",
    "failure_blame_layers", "real_task_evidence", "persisted_record_id", "sample_counts",
}
EXTRA_KEYS = {"control_plane", "product_completion"}
DEFAULT = object()


@dataclass
class ScopeRef:
    tenant_id: str = "tenant"
    agent_id: str = "agent"
    workspace_id: str = "workspace"
    user_id: str = "user"

    @classmethod
    def from_dict(cls, value):
        return cls(**(value or {}))


@dataclass
class ReleaseIdentity:
    complete: bool = True
    commit: str = "a" * 40


def load_functions(namespace):
    """Compile only literal source functions; never execute module imports."""
    selected = [FUNCTIONS[name] for name in sorted(FUNCTION_NAMES)]
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


def make_harness(product=DEFAULT, error=None, use_local_builder=False):
    scope = ScopeRef()
    calls = {"builder": [], "persist": [], "semantic": [], "readiness": []}
    product = {
        "control_plane": {"ok": False, "status": "given", "axes": {"x": [1, 2]}},
        "product_completion": {"complete": False, "status": "given", "gaps": ["x"], "extra": {"a": 1}},
    } if product is DEFAULT else product

    def records(runtime, supplied_scope, kinds, limit):
        return {
            ("replay_result",): [{"hit": True}, {"hit": False}],
            ("learning_eval",): [{"task_success": True, "report_type": "eiskill_invocation"}],
            ("learning_playbook",): [{"report_type": "eiskill_registry_entry", "reuse_count": 3}],
        }.get(tuple(kinds), [])

    def builder(runtime, *, scope):
        calls["builder"].append((runtime, scope))
        if error is not None:
            raise error
        return product

    def semantic(*args):
        calls["semantic"].append({"args": args, "snapshot": deepcopy(args)})
        return "given-semantic-key"

    def append(runtime, **kwargs):
        calls["persist"].append({"runtime": runtime, "kwargs": kwargs, "snapshot": deepcopy(kwargs)})
        return SimpleNamespace(record_id="given-record-id")

    def readiness(*, scope, persist):
        calls["readiness"].append({"scope": scope, "persist": persist})
        if error is not None:
            raise error
        return product

    namespace = {
        "Any": Any, "ScopeRef": ScopeRef, "ReleaseIdentity": ReleaseIdentity, "asdict": asdict,
        "_records": records,
        "_capability": lambda record: "memory.recall",
        "_has_key": lambda record, key: key in record,
        "_field": lambda record, key: record.get(key),
        "_truthy": bool,
        "_verdict": lambda record: "",
        "_outcome_trace_records": lambda *args: [],
        "_record_id": lambda record: str(record.get("record_id") or ""),
        "_event_outcome_records": lambda *args: [],
        "_is_rehearsal": lambda record: False,
        "_outcome_success": lambda record: record.get("task_success") is True,
        "_verified_live_task_outcomes": lambda *args, **kwargs: [],
        "_verified_real_task_outcomes": lambda *args, **kwargs: [],
        "hermes_channel_real_task_evidence_enabled": lambda: False,
        "_current_release_identity_for_scope": lambda *args, **kwargs: None,
        "same_release_authority": lambda *args: False,
        "_lineage_eligible_real_task": lambda item: False,
        "_quality": lambda count, minimum=10: {"sample_count": count, "minimum": minimum, "sufficient": count >= minimum},
        "_rate": lambda numerator, denominator: round(numerator / denominator, 3) if denominator else 0.0,
        "_latest_patch_candidate_records": lambda records: [],
        "_valid_code_patch_candidate": lambda record: False,
        "_executed_code_patch_deployment": lambda record: False,
        "_verified_code_patch_promotion": lambda record: False,
        "_policy_rollback_records": lambda *args: [{"given": True}],
        "_int": int,
        "stable_semantic_key": semantic,
        "append_learning_record_once": append,
    }
    load_functions(namespace)
    if not use_local_builder:
        namespace["build_product_completion_dashboard"] = builder
    runtime = SimpleNamespace(build_l5_readiness_report=readiness)

    def run(option=DEFAULT, persist=False):
        kwargs = {"scope": scope, "persist": persist}
        if option is not DEFAULT:
            kwargs["include_product_completion"] = option
        return namespace["build_capability_dashboard_metrics"](runtime, **kwargs)

    return SimpleNamespace(run=run, calls=calls, product=product, scope=scope, runtime=runtime, namespace=namespace)


class DashboardProjectionTests(unittest.TestCase):
    def test_default_and_explicit_true_include_full_builder_values(self):
        for option in (DEFAULT, True):
            for persist in (False, True):
                with self.subTest(option="default" if option is DEFAULT else option, persist=persist):
                    h = make_harness()
                    report = h.run(option, persist)
                    self.assertEqual(set(report["metrics"]), BASE_KEYS | EXTRA_KEYS)
                    for key in EXTRA_KEYS:
                        self.assertIs(report["metrics"][key], h.product[key])
                    self.assertEqual(h.calls["builder"], [(h.runtime, h.scope)])

    def test_disabled_skips_builder_even_if_it_would_raise(self):
        for persist in (False, True):
            with self.subTest(persist=persist):
                h = make_harness(error=RuntimeError("must not run"))
                report = h.run(False, persist)
                self.assertEqual(set(report["metrics"]), BASE_KEYS)
                self.assertEqual(h.calls["builder"], [])
                self.assertEqual(len(h.calls["persist"]), int(persist))

    def test_builder_exception_propagates_before_persistence(self):
        for option in (DEFAULT, True):
            for persist in (False, True):
                with self.subTest(option="default" if option is DEFAULT else option, persist=persist):
                    error = RuntimeError("given builder failure")
                    h = make_harness(error=error)
                    with self.assertRaises(RuntimeError) as caught:
                        h.run(option, persist)
                    self.assertIs(caught.exception, error)
                    self.assertEqual(len(h.calls["builder"]), 1)
                    self.assertEqual(h.calls["persist"], [])
                    self.assertEqual(h.calls["semantic"], [])

    def test_none_builder_return_preserves_base_metrics(self):
        for persist in (False, True):
            with self.subTest(persist=persist):
                h = make_harness(product=None)
                report = h.run(True, persist)
                self.assertEqual(set(report["metrics"]), BASE_KEYS)
                self.assertEqual(len(h.calls["builder"]), 1)

    def test_existing_fields_and_numeric_metrics_are_unchanged(self):
        for persist in (False, True):
            with self.subTest(persist=persist):
                disabled = make_harness().run(False, persist)
                enabled = make_harness().run(True, persist)
                self.assertEqual(set(enabled), TOP_KEYS)
                self.assertEqual(len(BASE_KEYS), 13)
                self.assertEqual(len(TOP_KEYS), 9)
                self.assertEqual({k: enabled["metrics"][k] for k in BASE_KEYS}, disabled["metrics"])
                self.assertEqual({k: v for k, v in enabled.items() if k != "metrics"}, {k: v for k, v in disabled.items() if k != "metrics"})
                expected = {k: 0.0 for k in BASE_KEYS}
                expected.update(recall_hit_rate=0.5, task_success_rate=1.0, rollback_count=1, skill_reuse_count=3)
                self.assertEqual(disabled["metrics"], expected)
                self.assertEqual(enabled["persisted_record_id"], "given-record-id" if persist else "")

    def test_persistence_payload_and_semantic_arguments_do_not_change(self):
        enabled_h = make_harness()
        enabled = enabled_h.run(True, True)
        disabled_h = make_harness()
        disabled_h.run(False, True)
        enabled_call = enabled_h.calls["persist"][0]
        disabled_call = disabled_h.calls["persist"][0]
        self.assertEqual(enabled_call["snapshot"], disabled_call["snapshot"])
        self.assertEqual(enabled_call["kwargs"], enabled_call["snapshot"])
        self.assertEqual(enabled_h.calls["semantic"][0]["snapshot"], disabled_h.calls["semantic"][0]["snapshot"])
        self.assertEqual(enabled_h.calls["semantic"][0]["args"], enabled_h.calls["semantic"][0]["snapshot"])
        persisted_metrics = enabled_call["kwargs"]["content"]["metrics"]
        semantic_metrics = enabled_h.calls["semantic"][0]["args"][2]
        self.assertIs(persisted_metrics, semantic_metrics)
        self.assertIsNot(enabled["metrics"], persisted_metrics)
        self.assertEqual(set(persisted_metrics), BASE_KEYS)
        self.assertTrue(EXTRA_KEYS.isdisjoint(enabled_call["kwargs"]["meta"]))
        enabled["metrics"]["recall_hit_rate"] = "mutated return only"
        self.assertEqual(persisted_metrics["recall_hit_rate"], 0.5)

    def test_persistence_disabled_never_calls_append_or_semantic_key(self):
        for option in (DEFAULT, True, False):
            with self.subTest(option="default" if option is DEFAULT else option):
                h = make_harness()
                h.run(option, False)
                self.assertEqual(h.calls["persist"], [])
                self.assertEqual(h.calls["semantic"], [])

    def test_product_input_mapping_is_not_mutated(self):
        h = make_harness()
        before = deepcopy(h.product)
        h.run(True, True)
        self.assertEqual(h.product, before)

    def test_local_builder_has_exact_two_literal_return_shapes(self):
        returns = [node for node in ast.walk(FUNCTIONS["build_product_completion_dashboard"]) if isinstance(node, ast.Return)]
        self.assertEqual(len(returns), 2)
        for node in returns:
            with self.subTest(line=node.lineno):
                self.assertIsInstance(node.value, ast.Dict)
                self.assertTrue(all(isinstance(key, ast.Constant) for key in node.value.keys))
                self.assertEqual({key.value for key in node.value.keys}, EXTRA_KEYS)

    def test_local_builder_normal_return_with_given_readiness(self):
        given = {
            "control_plane_ok": False, "control_plane_status": "given-control",
            "axes": {"given": 7}, "product_l5_complete": True,
            "completion_status": "given-completion", "gaps": ["given-gap"],
            "code_evolution": {"qualifying_terminal_outcome": {"given": "outcome"}},
        }
        for persist in (False, True):
            with self.subTest(persist=persist):
                h = make_harness(product=given, use_local_builder=True)
                report = h.run(True, persist)
                self.assertEqual(report["metrics"].get("control_plane"), {"ok": False, "status": "given-control", "axes": {"given": 7}})
                self.assertEqual(report["metrics"].get("product_completion"), {"complete": True, "status": "given-completion", "gaps": ["given-gap"], "qualifying_terminal_outcome": {"given": "outcome"}})
                self.assertEqual(h.calls["readiness"], [{"scope": h.scope, "persist": False}])

    def test_local_builder_exception_return_with_given_readiness_error(self):
        for persist in (False, True):
            with self.subTest(persist=persist):
                h = make_harness(error=ValueError("given readiness failure"), use_local_builder=True)
                report = h.run(True, persist)
                self.assertEqual(report["metrics"].get("control_plane"), {"ok": False, "status": "unavailable", "error": "ValueError"})
                self.assertEqual(report["metrics"].get("product_completion"), {"complete": False, "status": "incomplete", "gaps": ["readiness_unavailable"]})
                self.assertEqual(h.calls["readiness"], [{"scope": h.scope, "persist": False}])
                self.assertEqual(len(h.calls["persist"]), int(persist))

    def test_local_builder_is_not_entered_when_disabled(self):
        h = make_harness(error=AssertionError("readiness must not run"), use_local_builder=True)
        self.assertEqual(set(h.run(False, True)["metrics"]), BASE_KEYS)
        self.assertEqual(h.calls["readiness"], [])


if __name__ == "__main__":
    unittest.main()
