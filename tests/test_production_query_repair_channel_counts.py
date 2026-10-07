"""Offline regression for production-query report channel attribution.

Only AST-selected container/report statements run. The production module is
never imported; no store, validator, repair entrypoint or SQL call runs.
The validated-is-None call is taken unchanged from the production AST, so
removing its channel keyword makes the known-channel regression fail.

Standard unittest discovery uses the sibling eimemory source. Old and new
snapshots run identical test bytes with the same repository-relative layout.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE_PATH = Path(__file__).resolve().parents[1] / "eimemory/evaluation/production_query_repair.py"


def _assigned_name(node, name):
    return (isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name)


def _one(nodes, label):
    if len(nodes) != 1:
        raise AssertionError(f"Expected one {label}, found {len(nodes)}")
    return nodes[0]


def _run_nodes(nodes, namespace):
    module = ast.fix_missing_locations(ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
              *copy.deepcopy(nodes)], type_ignores=[]))
    exec(compile(module, str(SOURCE_PATH), "exec"), namespace)


class ReportProjection:
    """Expose only source-selected report mechanics on supplied containers."""

    def __init__(self):
        tree = ast.parse(SOURCE_PATH.read_bytes(), filename=str(SOURCE_PATH))
        funcs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        graph = funcs["_preflight_production_query_graph"]
        public = funcs["repair_production_query_channel_scopes"]
        self.helper = funcs["_add_conflict"]
        self.constants = [node for node in tree.body
                          if _assigned_name(node, "_MAX_CONFLICTS") or _assigned_name(node, "REPAIR_SCHEMA")]
        if len(self.constants) != 2 or any(not isinstance(n.value, ast.Constant) for n in self.constants):
            raise AssertionError("Report constants must remain two literals")
        self.initial = _one([n for n in public.body if _assigned_name(n, "result")], "initial report")
        self.channel = _one([n for n in ast.walk(graph) if _assigned_name(n, "channel")], "channel text")
        self.bucket = _one([n for n in ast.walk(graph) if _assigned_name(n, "channel_counts")], "channel bucket")
        self.scanned = _one([n for n in ast.walk(graph) if isinstance(n, ast.AugAssign)
                            and isinstance(n.target, ast.Subscript)
                            and isinstance(n.target.value, ast.Name)
                            and n.target.value.id == "channel_counts"
                            and isinstance(n.target.slice, ast.Constant)
                            and n.target.slice.value == "scanned"], "scanned increment")
        selected = _one([n for n in ast.walk(graph) if isinstance(n, ast.If)
                         and isinstance(n.test, ast.Compare)
                         and isinstance(n.test.left, ast.Name) and n.test.left.id == "validated"
                         and len(n.test.ops) == 1 and isinstance(n.test.ops[0], ast.Is)
                         and len(n.test.comparators) == 1
                         and isinstance(n.test.comparators[0], ast.Constant)
                         and n.test.comparators[0].value is None], "validated-is-None branch")
        self.call = _one([n for n in selected.body if isinstance(n, ast.Expr)
                          and isinstance(n.value, ast.Call)
                          and isinstance(n.value.func, ast.Name)
                          and n.value.func.id == "_add_conflict"], "actual conflict call")
        # Retain the actual predicate and actual call; exclude its SQL sibling
        # and all repair/quarantine alternatives, whose effects are out of scope.
        self.branch = ast.If(test=copy.deepcopy(selected.test), body=[copy.deepcopy(self.call)], orelse=[])
        self.cleanup = _one([n for n in public.body if isinstance(n, ast.If)
                             and isinstance(n.test, ast.UnaryOp) and isinstance(n.test.op, ast.Not)
                             and isinstance(n.test.operand, ast.Subscript)
                             and isinstance(n.test.operand.value, ast.Name)
                             and n.test.operand.value.id == "result"
                             and isinstance(n.test.operand.slice, ast.Constant)
                             and n.test.operand.slice.value == "ok"], "blocked report cleanup")
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "_add_conflict"]
        if len(calls) != 6:
            raise AssertionError("Review changed conflict-call topology")
        self.other_calls = [n for n in calls if n is not self.call.value]
        # Fail closed if selected report code grows an external action.
        self._check_report_nodes([self.initial, self.channel, self.bucket, self.scanned,
                                  self.branch, self.cleanup, self.helper])

    @staticmethod
    def _check_report_nodes(nodes):
        for root in nodes:
            for node in ast.walk(root):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    raise AssertionError("Imports are forbidden inside selected report code")
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        if node.func.id not in {"str", "dict", "len", "_add_conflict"}:
                            raise AssertionError(f"Unexpected report callee {node.func.id}")
                    elif isinstance(node.func, ast.Attribute):
                        if node.func.attr not in {"get", "setdefault", "append", "update", "values"}:
                            raise AssertionError(f"Unexpected report method {node.func.attr}")
                    else:
                        raise AssertionError("Unexpected dynamic report callee")

    def namespace(self, **values):
        namespace = dict(values)
        _run_nodes([*self.constants, self.helper], namespace)
        return namespace

    def new_result(self):
        namespace = self.namespace()
        _run_nodes([self.initial], namespace)
        return namespace["result"]

    def record(self, result, channel, *, record_type="label", record_id="given-id",
               reason="given-error", validated=None):
        namespace = self.namespace(result=result, boundary={"channel": channel},
                                   record_type=record_type, record=SimpleNamespace(record_id=record_id),
                                   reason=reason, validated=validated)
        _run_nodes([self.channel, self.bucket, self.scanned, self.branch], namespace)

    def other_conflicts(self, result):
        namespace = self.namespace(result=result, record_type="label",
                                   record=SimpleNamespace(record_id="unattributed"), reason="given-error")
        for call in self.other_calls:
            self._check_report_nodes([call])
            _run_nodes([ast.Expr(value=copy.deepcopy(call))], namespace)

    def blocked_cleanup(self, result):
        _run_nodes([self.cleanup], self.namespace(result=result))


class ProductionQueryRepairChannelCountsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.projection = ReportProjection()

    def setUp(self):
        self.result = self.projection.new_result()

    def test_actual_validated_none_call_attributes_known_channel(self):
        self.projection.record(self.result, "alpha", record_id="label-a")
        self.assertEqual(self.result["conflict_count"], 1)
        self.assertEqual(self.result["by_type"]["label"]["conflicts"], 1)
        self.assertEqual(self.result["by_channel"]["alpha"]["scanned"], 1)
        self.assertEqual(self.result["by_channel"]["alpha"]["conflicts"], 1)
        self.assertEqual(self.result["conflicts"], [dict(record_type="label", record_id="label-a", reason="given-error")])

    def test_multiple_channels_preserve_existing_counters_and_isolate_updates(self):
        previous = dict(scanned=7, repaired=3, already_correct=2, quarantined=1, conflicts=4)
        self.result["by_channel"]["alpha"] = previous
        self.projection.record(self.result, "alpha")
        self.projection.record(self.result, "beta")
        self.projection.record(self.result, "alpha")
        self.assertIs(self.result["by_channel"]["alpha"], previous)
        self.assertEqual(previous, dict(scanned=9, repaired=3, already_correct=2, quarantined=1, conflicts=6))
        self.assertEqual(self.result["by_channel"]["beta"], dict(scanned=1, repaired=0, already_correct=0, quarantined=0, conflicts=1))
        self.assertEqual(self.result["conflict_count"], 3)

    def test_unattributed_call_sites_keep_all_channels_unchanged(self):
        self.result["by_channel"]["alpha"] = dict(scanned=1, repaired=0, already_correct=0, quarantined=0, conflicts=3)
        before = copy.deepcopy(self.result["by_channel"])
        self.result["blocked_reason"] = "given-preflight-error"
        self.projection.other_conflicts(self.result)
        self.assertEqual(self.result["conflict_count"], 5)
        self.assertEqual(self.result["by_channel"], before)
        self.assertEqual(self.result["by_type"]["label"]["conflicts"], 4)
        self.assertEqual(self.result["by_type"]["preflight"]["conflicts"], 1)
        self.assertEqual([n.keywords for n in self.projection.other_calls], [[], [], [], [], []])

    def test_unknown_and_falsey_channel_values_are_not_force_attributed(self):
        for value in ("unknown", None, "", False, 0):
            self.projection.record(self.result, value)
        self.assertEqual(self.result["conflict_count"], 5)
        self.assertEqual(self.result["by_channel"], {"unknown": dict(scanned=5, repaired=0, already_correct=0, quarantined=0, conflicts=0)})

    def test_detail_sample_cap_does_not_cap_total_or_channel_counts(self):
        for index in range(53):
            self.projection.record(self.result, "alpha" if index % 2 == 0 else "beta", record_id=str(index))
        self.assertEqual(len(self.result["conflicts"]), 50)
        self.assertEqual(self.result["conflict_count"], 53)
        self.assertEqual(self.result["by_type"]["label"]["conflicts"], 53)
        self.assertEqual(self.result["by_channel"]["alpha"]["conflicts"], 27)
        self.assertEqual(self.result["by_channel"]["beta"]["conflicts"], 26)
        self.assertEqual(self.result["conflicts"][-1]["record_id"], "49")

    def test_blocked_report_cleanup_preserves_conflicts_and_resets_changed_counts(self):
        self.projection.record(self.result, "alpha")
        self.result.update(blocked_reason="given-error", repaired_count=2, quarantined_count=3,
                           status_projection_repaired_count=1, repaired_record_ids=["r"],
                           quarantined_record_ids=["q"], status_projection_repaired_record_ids=["s"],
                           receipt_id="receipt", quarantine_reasons={"reason": 3})
        for counters in [*self.result["by_type"].values(), *self.result["by_channel"].values()]:
            counters.update(repaired=2, quarantined=3)
        self.projection.blocked_cleanup(self.result)
        self.assertEqual(self.result["conflict_count"], 1)
        self.assertEqual(self.result["by_channel"]["alpha"]["conflicts"], 1)
        self.assertEqual(self.result["by_type"]["label"]["conflicts"], 1)
        self.assertEqual(self.result["by_channel"]["alpha"]["scanned"], 1)
        for key in ("repaired_count", "quarantined_count", "status_projection_repaired_count"):
            self.assertEqual(self.result[key], 0)
        for key in ("repaired_record_ids", "quarantined_record_ids", "status_projection_repaired_record_ids"):
            self.assertEqual(self.result[key], [])
        self.assertEqual(self.result["receipt_id"], "")
        self.assertEqual(self.result["quarantine_reasons"], {})
        for counters in [*self.result["by_type"].values(), *self.result["by_channel"].values()]:
            self.assertEqual((counters["repaired"], counters["quarantined"]), (0, 0))

    def test_non_none_validated_skips_conflict_projection(self):
        self.projection.record(self.result, "alpha", validated=object())
        self.assertEqual(self.result["conflict_count"], 0)
        self.assertEqual(self.result["conflicts"], [])
        self.assertEqual(self.result["by_channel"]["alpha"]["conflicts"], 0)
        self.assertEqual(self.result["by_channel"]["alpha"]["scanned"], 1)

    def test_exact_channel_text_and_empty_reason_fallback_are_preserved(self):
        self.projection.record(self.result, " Alpha ", reason="")
        self.assertEqual(set(self.result["by_channel"]), {" Alpha "})
        self.assertEqual(self.result["by_channel"][" Alpha "]["conflicts"], 1)
        self.assertEqual(self.result["conflicts"][0]["reason"], "evidence_authority_unverifiable")


if __name__ == "__main__":
    unittest.main()
