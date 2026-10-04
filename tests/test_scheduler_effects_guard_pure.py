"""Source-extracted pure scheduler guard tests; no package imports or real jobs.

Run directly with ``python -B tests/test_scheduler_effects_guard_pure.py``.
The local caller contains only the reviewed disabled/restart/reuse branches and
an in-memory fake. It never executes _run_l5_loop, Runtime, a cycle, env helpers,
lease readers, providers, evaluators, network, subprocesses or systemd.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
JOBS_PATH = ROOT / "eimemory/scheduler/jobs.py"
JOBS_TREE = ast.parse(JOBS_PATH.read_text(encoding="utf-8"))
FUNCTIONS = {node.name: node for node in JOBS_TREE.body if isinstance(node, ast.FunctionDef)}
GUARD_CONSTANT = "_AUTONOMOUS_LEARNING_UNSAFE_RESTART_REASONS"
HELPERS = {
    "_l5_upstream_execution_block_reason",
    "_is_reusable_autonomous_learning_report",
    "_sum_count_fields",
    "_coerce_non_negative_int",
}


def load_pure_helpers():
    selected = [node for node in JOBS_TREE.body if (
        isinstance(node, ast.ImportFrom) and node.module == "__future__"
    ) or (
        isinstance(node, ast.FunctionDef) and node.name in HELPERS
    ) or (
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == GUARD_CONSTANT for target in node.targets)
    )]
    assert len([node for node in selected if isinstance(node, ast.FunctionDef)]) == len(HELPERS)
    assert not any(isinstance(node, ast.Import) for item in selected for node in ast.walk(item))
    assert all(node.module == "__future__" for item in selected for node in ast.walk(item)
               if isinstance(node, ast.ImportFrom))
    namespace = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(JOBS_PATH), "exec"), namespace)
    return namespace


PURE = load_pure_helpers()
L5_WRAPPER = FUNCTIONS["_run_l5_loop"]
DISABLED_BRANCH = next(node for node in L5_WRAPPER.body if isinstance(node, ast.If)
                       and isinstance(node.test, ast.UnaryOp)
                       and isinstance(node.test.operand, ast.Name)
                       and node.test.operand.id == "enabled")
GUARD_ASSIGN = next(node for node in L5_WRAPPER.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "upstream_block_reason"
                            for target in node.targets))
GUARD_BRANCH = next(node for node in L5_WRAPPER.body if isinstance(node, ast.If)
                    and isinstance(node.test, ast.Name) and node.test.id == "upstream_block_reason")
REUSE_BRANCH = next(node for node in ast.walk(L5_WRAPPER) if isinstance(node, ast.If)
                    and isinstance(node.test, ast.Call) and isinstance(node.test.func, ast.Name)
                    and node.test.func.id == "_is_reusable_autonomous_learning_report")


def make_local_caller():
    # The only substituted call is our locally supplied counter function.
    template = ast.parse("""
def isolated_caller(autonomous_learning_report, fake_l5, *, enabled=True, required=False,
                    apply_changes=True, force=False):
    kwargs = {"apply": apply_changes, "force": force}
    return fake_l5(**kwargs)
""")
    fn = template.body[0]
    fn.body = [copy.deepcopy(DISABLED_BRANCH), copy.deepcopy(GUARD_ASSIGN),
               copy.deepcopy(GUARD_BRANCH), fn.body[0], copy.deepcopy(REUSE_BRANCH), fn.body[-1]]
    ast.fix_missing_locations(template)
    namespace = {name: PURE[name] for name in HELPERS}
    exec(compile(template, "<isolated_scheduler_sequence>", "exec"), namespace)
    return namespace["isolated_caller"]


CALLER = make_local_caller()


class SchedulerEffectsGuardPureTests(unittest.TestCase):
    def invoke(self, report, **settings):
        calls = []

        def fake_l5(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "fake": True}

        result = CALLER(report, fake_l5, **settings)
        return result, calls

    def assert_blocked(self, report, reason="upstream_autonomous_learning_effects_unknown", **settings):
        before = copy.deepcopy(report)
        result, calls = self.invoke(report, **settings)
        self.assertEqual(calls, [])
        self.assertIs(result["ok"], False)
        self.assertIs(result["execution_attempted"], False)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocked_reason"], reason)
        self.assertEqual(result["l5_skipped_reason"], reason)
        self.assertEqual(result["upstream_step"], "autonomous_learning")
        self.assertNotIn("awaiting_evidence", result)
        self.assertNotIn("applied_count", result)
        self.assertEqual(report, before)
        return result

    def test_explicit_unknown_even_if_report_is_reusable(self):
        report = {"ok": True, "loop_id": "synthetic_loop", "effects_unknown": True}
        self.assertTrue(PURE["_is_reusable_autonomous_learning_report"](report))
        self.assert_blocked(report)

    def test_timeout_even_without_unknown_marker(self):
        self.assert_blocked({"timeout_exceeded": True}, "upstream_autonomous_learning_timeout")
        self.assert_blocked({"timeout_exceeded": True, "effects_unknown": False},
                            "upstream_autonomous_learning_timeout")

    def test_idle_lease_does_not_unlock(self):
        self.assert_blocked({"timeout_exceeded": True, "effects_unknown": True,
                             "lease_reread": {"state": "idle", "side_effects": "none"}})

    def test_busy_or_unknown_lease_does_not_unlock(self):
        for state in ("busy", "unknown"):
            with self.subTest(state=state):
                self.assert_blocked({"effects_unknown": True,
                                     "lease_reread": {"state": state, "side_effects": "unknown"}})

    def test_exact_existing_reason_codes_without_flags(self):
        codes = PURE[GUARD_CONSTANT]
        self.assertEqual(set(codes), {"scheduler_lease_effects_unknown_after_timeout",
                                    "scheduler_timeout_lease_not_reread",
                                    "autonomous_learning_timeout_exceeded"})
        for code, reason in codes.items():
            for key in ("blocked_reason", "learning_skipped_reason"):
                with self.subTest(code=code, key=key):
                    self.assert_blocked({key: code}, reason)

    def test_apply_false_and_force_true_do_not_bypass(self):
        for apply_changes, force in ((False, False), (False, True), (True, True)):
            with self.subTest(apply_changes=apply_changes, force=force):
                self.assert_blocked({"effects_unknown": True}, apply_changes=apply_changes, force=force)

    def test_committed_effects_preserved_at_original_location_once(self):
        upstream = {"ok": False, "effects_unknown": True, "timeout_exceeded": True,
                    "applied_count": 1, "loop_id": "synthetic_loop",
                    "promotions": [{"applied": True, "record_id": "synthetic_committed"}],
                    "candidate_ids": ["synthetic_candidate"], "eval_record_ids": ["synthetic_eval"],
                    "lease_reread": {"state": "idle", "side_effects": "possible"}}
        blocked = self.assert_blocked(upstream)
        assembled = {"autonomous_learning": upstream, "l5_loop": blocked}
        self.assertIs(assembled["autonomous_learning"], upstream)
        self.assertEqual(PURE["_sum_count_fields"](assembled, {"applied_count", "promoted_count"}), 1)
        self.assertEqual(upstream["promotions"][0]["record_id"], "synthetic_committed")

    def test_normal_reusable_result_keeps_existing_reuse(self):
        report = {"ok": True, "enabled": True, "loop_id": "synthetic_loop"}
        result, calls = self.invoke(report)
        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["autonomous_learning_report"], report)

    def test_missing_or_ordinary_nonreusable_reports_keep_fallback(self):
        for report in (None, {}, {"ok": False}, {"enabled": False}, {"ok": "false"},
                       {"error": "ordinary_failure"}, {"awaiting_evidence": True}):
            with self.subTest(report=report):
                _, calls = self.invoke(report)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("autonomous_learning_report", calls[0])

    def test_ordinary_failed_but_reusable_report_not_reclassified(self):
        report = {"ok": False, "loop_id": "synthetic_loop"}
        _, calls = self.invoke(report)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["autonomous_learning_report"], report)

    def test_exact_matching_does_not_guess_from_substrings(self):
        for report in ({"error": "timeout"}, {"blocked_reason": "autonomous_learning_timeout_exceeded:detail"},
                       {"blocked_reason": "prefix:scheduler_timeout_lease_not_reread"},
                       {"blocked_reason": ["scheduler_timeout_lease_not_reread"]},
                       {"effects_unknown": "true"}, {"timeout_exceeded": 1}):
            with self.subTest(report=report):
                self.assertEqual(PURE["_l5_upstream_execution_block_reason"](report), "")

    def test_disabled_l5_preserves_existing_behavior(self):
        for required in (False, True):
            with self.subTest(required=required):
                result, calls = self.invoke({"effects_unknown": True}, enabled=False, required=required)
                self.assertEqual(calls, [])
                self.assertEqual(result["ok"], not required)
                self.assertEqual(result["l5_skipped_reason"],
                                 "l5_loop_required_but_disabled" if required else "l5_loop_disabled")

    def test_source_guard_precedes_reuse_and_real_call(self):
        self.assertLess(DISABLED_BRANCH.lineno, GUARD_ASSIGN.lineno)
        self.assertLess(GUARD_ASSIGN.lineno, GUARD_BRANCH.lineno)
        self.assertLess(GUARD_BRANCH.end_lineno, REUSE_BRANCH.lineno)
        calls = [node for node in ast.walk(L5_WRAPPER) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "run_l5"]
        self.assertEqual(len(calls), 1)
        self.assertLess(GUARD_BRANCH.end_lineno, calls[0].lineno)
        # No apply/force policy can take precedence over the sequencing return.
        for node in L5_WRAPPER.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {"apply_changes", "force"}
                                                   for t in node.targets):
                self.assertLess(GUARD_BRANCH.end_lineno, node.lineno)

    def test_blocked_result_stays_failed_in_pure_contract(self):
        path = ROOT / "eimemory/scheduler/result_contract.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        self.assertFalse(any(isinstance(node, ast.Import) for node in tree.body))
        self.assertTrue(all(node.module == "__future__" for node in tree.body
                            if isinstance(node, ast.ImportFrom)))
        namespace = {}
        exec(compile(tree, str(path), "exec"), namespace)
        blocked = self.assert_blocked({"effects_unknown": True})
        steps = []
        result = namespace["_nightly_step"](steps, "l5_loop", lambda: blocked)
        self.assertIs(result, blocked)
        self.assertIs(steps[0]["execution_ok"], False)
        self.assertEqual(steps[0]["evaluation_status"], "failed")
        self.assertIs(namespace["_aggregate_nightly_ok"]({"l5_loop": blocked}, steps), False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
