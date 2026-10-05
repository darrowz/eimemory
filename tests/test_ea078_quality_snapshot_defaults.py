"""Isolated stdlib AST regressions; no project imports or runtime integration."""
import ast
from collections.abc import Mapping
from copy import deepcopy
import math
from pathlib import Path
from typing import Any
import unittest


def load_snapshot_helpers():
    path = Path(__file__).resolve().parents[1] / "eimemory/living/schema.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {
        "default_living_memory_meta", "_merge_living_defaults",
        "refresh_living_quality_snapshot", "_apply_quality_snapshot",
        "_business_mapping", "_float_value",
    }
    constants = {"LIVING_MEMORY_META_KEY", "LIVING_MEMORY_SCHEMA_VERSION"}
    nodes = [node for node in tree.body if (
        isinstance(node, ast.FunctionDef) and node.name in names
    ) or (
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in constants
                for target in node.targets)
    )]
    assert {node.name for node in nodes if isinstance(node, ast.FunctionDef)} == names
    namespace = {"Mapping": Mapping, "deepcopy": deepcopy, "Any": Any}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class QualitySnapshotDefaultsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_snapshot_helpers()

    def refresh_score(self, field, previous, current, *, missing=False):
        living = {"quality_snapshot": {field: previous}}
        before = deepcopy(living)
        payload = {"quality_tier": "synthetic"} if field == "salience_score" else {"schema_version": "synthetic"}
        if not missing:
            payload[field] = current
        meta = {"quality": payload} if field == "salience_score" else {"scoring": {"memory_score_v1": payload}}
        result = self.helpers["refresh_living_quality_snapshot"](living, meta=meta)
        self.assertEqual(living, before)
        return result["quality_snapshot"][field]

    def test_missing_current_and_unusable_previous_recover_to_zero(self):
        for field in ("salience_score", "final_score"):
            for previous in (None, "bad", [], {}):
                with self.subTest(field=field, previous=previous):
                    self.assertEqual(self.refresh_score(field, previous, None, missing=True), 0.0)

    def test_invalid_current_and_unusable_previous_recover_to_zero(self):
        for field in ("salience_score", "final_score"):
            for previous in (None, "bad", [], {}):
                for current in (None, "invalid", [], {}):
                    with self.subTest(field=field, previous=previous, current=current):
                        self.assertEqual(self.refresh_score(field, previous, current), 0.0)

    def test_missing_or_invalid_current_retains_valid_previous(self):
        for field in ("salience_score", "final_score"):
            for previous, expected in ((0, 0.0), (0.625, 0.625), ("0.34567", 0.3457)):
                for current in (None, "invalid", [], {}):
                    with self.subTest(field=field, previous=previous, current=current):
                        self.assertEqual(self.refresh_score(field, previous, current), expected)
                self.assertEqual(self.refresh_score(field, previous, None, missing=True), expected)

    def test_valid_current_including_zero_overrides_previous(self):
        for field in ("salience_score", "final_score"):
            for previous in ("bad", 0.875):
                for current, expected in ((0, 0.0), ("0", 0.0), ("0.45678", 0.4568), (0.75, 0.75)):
                    with self.subTest(field=field, previous=previous, current=current):
                        self.assertEqual(self.refresh_score(field, previous, current), expected)

    def test_unrelated_metadata_matches_no_metadata_recovery(self):
        living = {"quality_snapshot": {"salience_score": "bad", "final_score": []}}
        refresh = self.helpers["refresh_living_quality_snapshot"]
        plain = refresh(living)["quality_snapshot"]
        updated = refresh(living, meta={"quality": {"quality_tier": "synthetic"}, "scoring": {"memory_score_v1": {"schema_version": "synthetic"}}})["quality_snapshot"]
        for field in ("salience_score", "final_score"):
            self.assertEqual(plain[field], 0.0)
            self.assertEqual(updated[field], plain[field])
        self.assertEqual(updated["quality_tier"], "synthetic")
        self.assertEqual(updated["scoring_schema_version"], "synthetic")

    def test_direct_helper_fallback_and_primary_precedence(self):
        helper = self.helpers["_float_value"]
        for bad in (None, "bad", [], {}):
            self.assertEqual(helper(bad, "also bad"), 0.0)
            self.assertEqual(helper(bad, "0.23456"), 0.2346)
            self.assertEqual(helper(bad, 0), 0.0)
            self.assertEqual(helper("0.65432", bad), 0.6543)
            self.assertEqual(helper(0, bad), 0.0)

    def test_direct_helper_preserves_range_and_nonfinite_semantics(self):
        helper = self.helpers["_float_value"]
        for value in (-12.5, 17.0, float("inf"), float("-inf")):
            self.assertEqual(helper(value, "bad"), value)
            self.assertEqual(helper("bad", value), value)
        self.assertTrue(math.isnan(helper(float("nan"), "bad")))
        self.assertTrue(math.isnan(helper("bad", float("nan"))))


if __name__ == "__main__":
    unittest.main()
