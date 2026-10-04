"""Finite AST/inert snapshot contracts; no eimemory imports or live entrypoints."""
from __future__ import annotations

import ast
import __future__
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "eimemory/governance/learning/self_model.py"
STATE = ROOT / "eimemory/governance/learning/learning_state.py"
AUTONOMY = ROOT / "eimemory/governance/learning/autonomous_learning.py"
RUNTIME = ROOT / "eimemory/api/runtime.py"


@dataclass
class InertScope:
    tenant_id: str = "synthetic-tenant"
    agent_id: str = "synthetic-agent"
    workspace_id: str = "synthetic-workspace"
    user_id: str = "synthetic-user"

    @classmethod
    def from_dict(cls, value):
        return cls(**(value or {}))


class ForbiddenRuntime:
    def __getattribute__(self, name):
        raise AssertionError(f"unexpected runtime access: {name}")


def definitions(path):
    return {n.name: n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef)}


def add_functions(namespace, path, names):
    nodes = [definitions(path)[name] for name in names]
    for node in nodes:
        if any(isinstance(item, (ast.Import, ast.ImportFrom)) for item in ast.walk(node)):
            raise AssertionError(f"unexpected package import in extracted helper: {node.name}")
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec", flags=__future__.annotations.compiler_flag), namespace)


def add_constants(namespace, path, names=None):
    for node in ast.parse(path.read_text()).body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        if not isinstance(target, ast.Name) or (names is not None and target.id not in names):
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset":
            namespace[target.id] = frozenset(ast.literal_eval(value.args[0]))
        else:
            namespace[target.id] = ast.literal_eval(value)


class InertRecorder:
    """Records only arguments; mimics a keyed return without opening any store."""
    def __init__(self):
        self.records = {}
        self.calls = []
        self.return_transform = None

    def append(self, runtime, **kwargs):
        self.calls.append(deepcopy(kwargs))
        key = (kwargs["kind"], kwargs["loop_id"], kwargs["step_name"], kwargs["semantic_key"])
        if key not in self.records:
            self.records[key] = SimpleNamespace(
                record_id=f"inert-actual-{len(self.records) + 1}", kind=kwargs["kind"],
                status=kwargs["status"], scope=deepcopy(kwargs["scope"]),
                source=kwargs.get("source", "eimemory.autonomous_learning"), source_id="default",
                content=deepcopy(kwargs["content"]), meta=deepcopy(kwargs["meta"]),
            )
        record = deepcopy(self.records[key])
        return self.return_transform(record) if self.return_transform else record


def namespace(recorder):
    ns = {"asdict": asdict, "sha256": sha256, "json": json, "ScopeRef": InertScope,
          "DEFAULT_SOURCE_ID": "default", "Mapping": Mapping,
          "append_learning_record_once": recorder.append}
    add_constants(ns, MODEL, {"SELF_MODEL_SNAPSHOT_SCHEMA", "SELF_MODEL_SOURCE", "_SELF_MODEL_REPORT_FIELDS"})
    metadata = ROOT / "eimemory/metadata.py"
    add_constants(ns, metadata)
    add_functions(ns, metadata, ["business_metadata", "split_metadata", "_dict_value", "_has_runtime_value"])
    add_functions(ns, STATE, ["stable_semantic_key"])
    add_functions(ns, MODEL, ["_unpersisted_self_model", "_self_model_snapshot", "_validate_self_model_record", "persist_self_model"])
    # Execute only the builder's final report assignment/return, never the
    # builder, its readers, projections, evaluators, or imports.
    tail = definitions(MODEL)["build_self_model"].body[-2:]
    if not isinstance(tail[0], ast.Assign) or not isinstance(tail[1], ast.Return):
        raise AssertionError("self-model report tail changed; review extraction")
    function = ast.parse("def finish_report(runtime, model, scope_ref, loop_id, persist):\n    pass\n").body[0]
    function.body = tail
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "<report-tail-only>", "exec"), ns)
    return ns


class SelfModelSnapshotContractTests(unittest.TestCase):
    def setUp(self):
        self.recorder = InertRecorder()
        self.ns = namespace(self.recorder)
        self.scope = InertScope()
        self.model = {
            "schema_version": "autonomous_learning.v2", "scope": asdict(self.scope),
            "capability_scope": "global", "profile_key": "profile-a", "profile": {"revision": "r1"},
            "legacy_compatibility": False, "capability_view_digest": "view-a", "capability_ledger_digest": "ledger-a",
            "capability_evaluation_view": {"cases": [{"target": {"capability_id": "cap", "provider_binding_id": "binding-a"}}]},
            "capabilities": [{"capability": "cap", "score": 0.1}],
            "weaknesses": [{"kind": "test", "lesson": "Keep Case", "source_record_ids": ["source-a"]}],
            "metrics": {"replay_pass_rate": 0.2},
        }

    def snapshot(self, model=None, scope=None):
        return self.ns["_self_model_snapshot"](self.model if model is None else model, scope=scope or self.scope)

    def persist(self, model=None, scope=None, loop="think"):
        return self.ns["persist_self_model"](ForbiddenRuntime(), self.model if model is None else model, scope=scope or self.scope, loop_id=loop)

    def test_every_semantic_section_changes_fingerprint(self):
        baseline = self.snapshot()[1]
        replacements = {
            "weaknesses": [{"kind": "test", "lesson": "Changed", "source_record_ids": ["source-a"]}],
            "capabilities": [{"capability": "cap", "score": 0.9}],
            "metrics": {"replay_pass_rate": 0.9}, "profile_key": "profile-b", "profile": {"revision": "r2"},
            "legacy_compatibility": True, "capability_scope": "profile", "capability_view_digest": "view-b",
            "capability_ledger_digest": "ledger-b", "capability_evaluation_view": {"cases": [{"target": {"provider_binding_id": "binding-b"}}]},
        }
        for key, value in replacements.items():
            with self.subTest(key=key):
                self.assertNotEqual(self.snapshot({**self.model, key: value})[1], baseline)

    def test_complete_exact_scope_participates(self):
        baseline = self.snapshot()[1]
        for key in ("tenant_id", "agent_id", "workspace_id", "user_id"):
            with self.subTest(key=key):
                scope = replace(self.scope, **{key: "other"})
                self.assertNotEqual(self.snapshot({**self.model, "scope": asdict(scope)}, scope)[1], baseline)

    def test_mismatched_declared_scope_rejected_before_append(self):
        with self.assertRaisesRegex(ValueError, "scope mismatch"):
            self.persist(scope=replace(self.scope, user_id="other"))
        self.assertEqual(self.recorder.calls, [])

    def test_canonical_dictionary_order_preserves_content_case(self):
        reordered = {key: self.model[key] for key in reversed(self.model)}
        self.assertEqual(self.snapshot(reordered)[1], self.snapshot()[1])
        changed = deepcopy(self.model)
        changed["weaknesses"][0]["lesson"] = "keep case"
        self.assertNotEqual(self.snapshot(changed)[1], self.snapshot()[1])

    def test_report_wallclock_excluded_but_nested_evidence_time_preserved(self):
        expected = self.snapshot()[1]
        for key in self.ns["_SELF_MODEL_REPORT_FIELDS"]:
            with self.subTest(key=key):
                self.assertEqual(self.snapshot({**self.model, key: "volatile"})[1], expected)
        changed = deepcopy(self.model)
        changed["capabilities"][0]["latest"] = {"occurred_at": "one"}
        other = deepcopy(changed)
        other["capabilities"][0]["latest"]["occurred_at"] = "two"
        self.assertNotEqual(self.snapshot(changed)[1], self.snapshot(other)[1])

    def test_snapshot_detaches_input_and_envelope(self):
        before = deepcopy(self.model)
        payload, fingerprint = self.snapshot()
        self.assertEqual(self.model, before)
        self.model["weaknesses"][0]["lesson"] = "later mutation"
        self.assertEqual(payload, before)
        self.assertEqual(self.snapshot(payload)[1], fingerprint)

    def test_invalid_json_and_nonfinite_numbers_rejected_before_write(self):
        for value in (float("nan"), float("inf"), float("-inf"), object(), {1: "non-string key"}, ("tuple",)):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError):
                    self.persist({**self.model, "extra": value})
        self.assertEqual(self.recorder.calls, [])

    def test_same_content_retry_reuses_actual_returned_id(self):
        first = self.persist()
        second = self.persist()
        self.assertEqual(first, second)
        self.assertEqual(first["model_record_id"], "inert-actual-1")
        self.assertEqual(first["content_fingerprint"], self.snapshot()[1])
        self.assertEqual(len(self.recorder.records), 2)  # One model plus existing weakness behavior.

    def test_same_count_content_change_appends_without_migrating_old_record(self):
        first = self.persist()
        originals = deepcopy(self.recorder.records)
        changed = deepcopy(self.model)
        changed["capabilities"][0]["score"] = 0.8
        second = self.persist(changed)
        self.assertNotEqual(first["model_record_id"], second["model_record_id"])
        for key, value in originals.items():
            self.assertEqual(self.recorder.records[key].__dict__, value.__dict__)
        self.assertNotEqual(self.recorder.calls[0]["semantic_key"], self.ns["stable_semantic_key"](
            "self_model", self.scope.agent_id, self.scope.workspace_id, len(self.model["weaknesses"])))

    def test_persist_false_report_tail_never_accesses_runtime_or_recorder(self):
        result = self.ns["finish_report"](ForbiddenRuntime(), deepcopy(self.model), self.scope, "think", False)
        self.assertEqual(result["persistence"]["status"], "not_persisted")
        self.assertEqual(result["persistence"]["model_record_id"], "")
        self.assertEqual(result["persistence"]["weakness_record_ids"], [])
        self.assertEqual(self.recorder.calls, [])

    def test_persist_true_report_tail_matches_stored_content_without_self_reference(self):
        result = self.ns["finish_report"](ForbiddenRuntime(), deepcopy(self.model), self.scope, "think", True)
        actual = self.recorder.calls[0]["content"]
        self.assertNotIn("persistence", actual["model"])
        self.assertEqual(result["persistence"]["content_fingerprint"], actual["content_fingerprint"])
        result["weaknesses"][0]["lesson"] = "report-only edit"
        self.assertEqual(actual["model"]["weaknesses"][0]["lesson"], "Keep Case")

    def test_returned_identity_mismatches_fail_before_weakness_append(self):
        cases = {
            "record_id": "", "kind": "reflection", "status": "archived", "source": "foreign",
            "source_id": "foreign", "scope": replace(self.scope, user_id=""),
        }
        for attribute, value in cases.items():
            with self.subTest(attribute=attribute):
                recorder = InertRecorder()
                ns = namespace(recorder)
                def corrupt(record, attribute=attribute, value=value):
                    setattr(record, attribute, value)
                    return record
                recorder.return_transform = corrupt
                with self.assertRaisesRegex(ValueError, "identity mismatch"):
                    ns["persist_self_model"](ForbiddenRuntime(), self.model, scope=self.scope, loop_id="think")
                self.assertEqual(len(recorder.calls), 1)

    def test_returned_content_and_fingerprint_mismatches_fail(self):
        for field in ("body", "content_fingerprint", "metadata", "schema"):
            with self.subTest(field=field):
                recorder = InertRecorder()
                ns = namespace(recorder)
                def corrupt(record, field=field):
                    if field == "body":
                        record.content["model"]["metrics"] = {"replay_pass_rate": 99}
                    elif field == "metadata":
                        record.meta["content_fingerprint"] = "wrong"
                    elif field == "schema":
                        record.content["snapshot_schema"] = "wrong"
                    else:
                        record.content["content_fingerprint"] = "wrong"
                    return record
                recorder.return_transform = corrupt
                with self.assertRaisesRegex(ValueError, "content mismatch"):
                    ns["persist_self_model"](ForbiddenRuntime(), self.model, scope=self.scope, loop_id="think")
                self.assertEqual(len(recorder.calls), 1)

    def test_content_equality_does_not_hide_bool_number_fingerprint_difference(self):
        def corrupt(record):
            record.content["model"]["legacy_compatibility"] = 0  # False == 0 in Python.
            return record
        self.recorder.return_transform = corrupt
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            self.persist()

    def test_release_bound_and_shared_idempotency_call_contract_is_unchanged(self):
        self.persist()
        self.assertNotIn("release_bound_idempotency", self.recorder.calls[0])
        self.assertEqual(self.recorder.calls[0]["loop_id"], "think")
        self.assertEqual(self.recorder.calls[0]["step_name"], "self_model")
        # No changes are needed to the shared helper; this reader asserts its
        # existing indexed lookup still receives the caller's scope unchanged.
        finder = definitions(STATE)["find_record_by_idempotency"]
        lookup = next(n for n in ast.walk(finder) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "indexed_lookup")
        self.assertEqual(ast.unparse(next(k.value for k in lookup.keywords if k.arg == "scope")), "scope_ref")

    def test_builder_profile_legacy_and_blocked_report_contract(self):
        builder = definitions(MODEL)["build_self_model"]
        payload = next(n.value for n in builder.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "model" for t in n.targets))
        fields = {k.value: v for k, v in zip(payload.keys, payload.values)}
        self.assertEqual(ast.unparse(fields["profile_key"]), "profile_key")
        self.assertEqual(ast.unparse(fields["legacy_compatibility"]), "bool(legacy_compatibility)")
        blocked = next(n for n in ast.walk(builder) if isinstance(n, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "reason" for k in n.keys))
        fields = {k.value: v for k, v in zip(blocked.keys, blocked.values)}
        self.assertEqual(ast.unparse(fields["persistence"]), "_unpersisted_self_model()")

    def test_runtime_report_uses_actual_self_model_persistence(self):
        tree = ast.parse(RUNTIME.read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "generate_learning_thoughts")
        target = next(n for n in ast.walk(method) if isinstance(n, ast.Assign) and any(ast.unparse(t) == "report['self_model_persistence']" for t in n.targets))
        self.assertEqual(ast.unparse(target.value), "dict(self_model.get('persistence') or {})")

    def test_cycle_preview_and_deferred_persist_order_remain(self):
        cycle = definitions(AUTONOMY)["run_autonomous_learning_cycle"]
        branch = cycle.body[1]
        self.assertIsInstance(branch, ast.If)
        self.assertEqual(ast.unparse(branch.test), "dry_run")
        self.assertEqual(ast.unparse(branch.body[0].value.func), "_run_autonomous_learning_dry_run")
        calls = [n for n in ast.walk(cycle) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        persisted = [n for n in calls if n.func.id == "build_self_model" and any(k.arg == "persist" and isinstance(k.value, ast.Constant) and k.value.value is True for k in n.keywords)]
        self.assertEqual(len(persisted), 1)
        thought = next(n for n in calls if n.func.id == "generate_thoughts")
        self.assertGreater(persisted[0].lineno, thought.lineno)
        captured = next(n for n in ast.walk(cycle) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "self_model_persistence" for t in n.targets))
        self.assertGreater(captured.lineno, persisted[0].lineno)
        self.assertEqual(ast.unparse(captured.value), "dict(self_model.get('persistence') or {})")
        result = next(n.value for n in ast.walk(cycle) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "result" for t in n.targets) and isinstance(n.value, ast.Dict))
        fields = {k.value: v for k, v in zip(result.keys, result.values) if isinstance(k, ast.Constant)}
        self.assertEqual(ast.unparse(fields["self_model_persistence"]), "self_model_persistence")
        renderer = definitions(AUTONOMY)["_run_autonomous_learning_dry_run"]
        allowed = {"asdict", "isinstance", "dict", "max", "min", "_as_int", "bool", "str"}
        for node in ast.walk(renderer):
            if isinstance(node, ast.Call):
                self.assertIsInstance(node.func, ast.Name)
                self.assertIn(node.func.id, allowed)


if __name__ == "__main__":
    unittest.main()
