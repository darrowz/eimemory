"""Regression tests for f817cd2 audit fixes.

Run inside the real repository after applying the patch. These tests use the
real RecordEnvelope, RuntimeStore, projection writer and governance functions.
Storage cases use the real RuntimeStore. The outcome driver uses controlled
store/promoter doubles, and rollback cases inject an export failure. These tests
were syntax-checked, not executed against the complete repository in the audit
environment; see the accompanying validation report.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from eimemory.adapters.runtime import host_auth
from eimemory.governance.learning import rule_evolution
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.record_export import _scope_partition, export_record_markdown
from eimemory.storage.runtime_store import RuntimeStore


def make_memory(record_id: str, scope: ScopeRef | None = None) -> RecordEnvelope:
    record = RecordEnvelope.create(
        kind="memory",
        title="Audit stable configuration preference",
        summary="The user deliberately requires an explicit approval before publishing a release.",
        content={"text": "Require explicit approval before publishing any release."},
        scope=scope if scope is not None else ScopeRef(tenant_id="audit-isolated"),
        source="audit.regression",
        meta={"semantic_key": "audit:release:approval", "force_capture": True, "memory_type": "fact"},
    )
    record.record_id = record_id
    return record


class ProjectionRegressionTests(unittest.TestCase):
    def pair(self):
        return ScopeRef("tenant", "agent|workspace", "user", ""), ScopeRef("tenant", "agent", "workspace|user", "")

    def test_unambiguous_scope_partition(self):
        left, right = self.pair()
        self.assertNotEqual(_scope_partition(left), _scope_partition(right))

    def test_same_record_id_keeps_two_projections(self):
        left, right = self.pair()
        with tempfile.TemporaryDirectory() as root:
            a = export_record_markdown(root, make_memory("mem_audit_same", left))
            b = export_record_markdown(root, make_memory("mem_audit_same", right))
            self.assertIsNotNone(a)
            self.assertIsNotNone(b)
            self.assertNotEqual(a, b)
            self.assertTrue(a.exists())
            self.assertTrue(b.exists())

    def test_rejection_does_not_delete_other_scope(self):
        left, right = self.pair()
        with tempfile.TemporaryDirectory() as root:
            target = export_record_markdown(root, make_memory("mem_audit_same", left))
            rejected = make_memory("mem_audit_same", right)
            rejected.status = "rejected"
            export_record_markdown(root, rejected)
            self.assertTrue(target.exists())


class RuntimeOutboxRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RuntimeStore(self.temp.name)
        self.addCleanup(self.store.close)

    def test_supersede_enqueues_final_new_record_once(self):
        old = make_memory("mem_audit_old")
        new = make_memory("mem_audit_new")
        self.store.append(old)
        snapshots = []
        original_enqueue = self.store._enqueue_record_exports

        def capture(record):
            snapshots.append(json.loads(json.dumps(record.to_dict())))
            return original_enqueue(record)

        with patch.object(self.store, "_enqueue_record_exports", side_effect=capture):
            self.store.append_and_supersede(new, semantic_key="audit:release:approval")
        new_exports = [row for row in snapshots if row["record_id"] == new.record_id]
        self.assertEqual(len(new_exports), 1)
        persisted = self.store.get_by_id(new.record_id, scope=new.scope)
        self.assertIsNotNone(persisted)
        self.assertEqual(new_exports[0]["links"], persisted.to_dict()["links"])
        self.assertIn(old.record_id, [link["target_id"] for link in new_exports[0]["links"] if link["relation"] == "supersedes"])

    def test_first_record_still_has_an_export(self):
        new = make_memory("mem_audit_first")
        with patch.object(self.store, "_enqueue_record_exports", wraps=self.store._enqueue_record_exports) as enqueue:
            self.store.append_and_supersede(new, semantic_key="audit:release:approval")
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(enqueue.call_args.args[0].record_id, new.record_id)

    def test_export_failure_rolls_back_superseded_status(self):
        old = make_memory("mem_audit_old")
        new = make_memory("mem_audit_new")
        self.store.append(old)
        original_enqueue = self.store._enqueue_record_exports

        def fail_new(record):
            if record.record_id == new.record_id:
                raise OSError("injected_audit_export_failure")
            return original_enqueue(record)

        with patch.object(self.store, "_enqueue_record_exports", side_effect=fail_new):
            with self.assertRaisesRegex(OSError, "injected_audit_export_failure"):
                self.store.append_and_supersede(new, semantic_key="audit:release:approval")
        self.assertIsNone(self.store.get_by_id(new.record_id, scope=new.scope))
        self.assertEqual(self.store.get_by_id(old.record_id, scope=old.scope).status, "active")


class GovernanceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.rule = RecordEnvelope.create(
            kind="rule", title="Require explicit approval", summary="Require explicit approval",
            content={"response_policy": {"summary": "Require explicit approval"}},
            scope=ScopeRef(tenant_id="audit-isolated"), status="accepted",
        )
        self.dataset = [{"expected_text": ["Require explicit approval"]}]

    def legacy_lint(self):
        return RecordEnvelope.create(
            kind="replay_result", title="Historical text lint", scope=self.rule.scope,
            source="eimemory.rule_evolution_loop",
            meta={"target_rule_id": self.rule.record_id, "verdict": "pass", "pass_rate": 1.0,
                  "replay_source": "outcome_trace_suggested_replay"},
        )

    def test_lint_diagnostic_never_has_authoritative_pass(self):
        report = rule_evolution._outcome_candidate_replay_result(self.rule, dataset=self.dataset, spec={})
        self.assertEqual(report.meta["lint_verdict"], "pass")
        self.assertEqual(report.meta["verdict"], "diagnostic_only")
        self.assertIs(report.meta["promotion_eligible"], False)

    def test_allow_auto_promote_does_not_invoke_promoter(self):
        promoter = Mock()
        runtime = SimpleNamespace(
            store=SimpleNamespace(get_by_id=lambda record_id: self.rule, append=lambda record: record),
            evolution=SimpleNamespace(promote_rule=promoter),
        )
        reports, promoted = rule_evolution._replay_and_promote_outcome_rules(runtime, candidate_specs=[{
            "source_type": "diagnosis_pattern", "_created_rule_id": self.rule.record_id,
            "suggested_replay_dataset": self.dataset, "promotion_gate": {"allow_auto_promote": True},
        }])
        promoter.assert_not_called()
        self.assertEqual(promoted, [])
        self.assertEqual(len(reports), 1)

    def test_historical_lint_is_excluded_from_replay_and_pass_counts(self):
        report = self.legacy_lint()
        self.assertFalse(rule_evolution._is_actual_replay_result(report))
        self.assertFalse(rule_evolution._replay_result_counts_as_pass(report))
        self.assertIsNone(rule_evolution._latest_replay_for_rule(self.rule.record_id, [report]))

    def test_historical_lint_cannot_pass_generic_promotion(self):
        feedback = RecordEnvelope.create(
            kind="feedback", title="Accepted feedback", scope=self.rule.scope,
            meta={"decision": "accept", "target_ref": {"record_id": self.rule.record_id}},
        )
        candidates = rule_evolution._promotion_candidates(
            rules=[self.rule], feedback_records=[feedback], replay_results=[self.legacy_lint()],
            min_roi=0.0, roi_summary={"roi_signal": 1.0},
        )
        self.assertEqual(candidates, [])

    def test_legacy_baseline_replay_cannot_authorize_promotion(self):
        report = RecordEnvelope.create(
            kind="replay_result", title="Existing replay", scope=self.rule.scope, source="evolution.replay",
            meta={"target_rule_id": self.rule.record_id, "verdict": "pass", "pass_rate": 1.0},
        )
        self.assertFalse(rule_evolution._is_actual_replay_result(report))
        self.assertFalse(rule_evolution._replay_result_counts_as_pass(report))

    def test_scoped_real_task_replay_with_samples_still_accepted(self):
        report = RecordEnvelope.create(
            kind="replay_result", title="Real task replay", scope=self.rule.scope, source="real_task_replay",
            meta={"target_rule_id": self.rule.record_id, "verdict": "pass", "pass_rate": 1.0, "sample_size": 3},
        )
        self.assertTrue(rule_evolution._is_actual_replay_result(report))
        self.assertTrue(rule_evolution._replay_result_counts_as_pass(report))


@unittest.skipUnless(os.name == "posix", "POSIX credential filesystem contract")
class CredentialRegressionTests(unittest.TestCase):
    def test_fifo_is_rejected_without_waiting_for_writer(self):
        with tempfile.TemporaryDirectory() as root:
            fifo = Path(root) / "credential-fifo"
            os.mkfifo(fifo, 0o600)
            code = (
                "import runpy,sys; from pathlib import Path; "
                "ns=runpy.run_path(sys.argv[1]); print('ready',flush=True); "
                "assert ns['_read_private_file'](Path(sys.argv[2]),max_bytes=128)==b''"
            )
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", code, str(Path(host_auth.__file__).resolve()), str(fifo)],
                capture_output=True, text=True, timeout=3, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
