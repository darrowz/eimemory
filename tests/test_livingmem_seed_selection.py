"""AST-isolated seed-selection regressions; no application imports or datasets."""

import ast
from pathlib import Path
import unittest


def load_case_record():
    source = Path(__file__).resolve().parents[1] / "eimemory/evaluation/livingmem.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_case_record"
    ]
    if len(functions) != 1:
        raise AssertionError("Expected exactly one _case_record function")
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0,
            ),
            *functions,
        ],
        type_ignores=[],
    )
    namespace = {}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace["_case_record"]


class LivingMemSeedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.select = load_case_record()
        self.first = object()
        self.second = object()
        self.seeds = [("first", self.first), ("second", self.second)]
        self.lookup = dict(self.seeds)

    def select_record(self, case, index=0):
        return self.select(
            case, index=index, seeded_records=self.seeds,
            records_by_seed_id=self.lookup,
        )

    def test_known_explicit_id_overrides_position_and_seed_index(self):
        for field in ("seed_id", "record_seed_id"):
            with self.subTest(field=field):
                self.assertIs(
                    self.select_record({field: "second", "seed_index": 0}),
                    self.second,
                )

    def test_unknown_explicit_id_does_not_fall_back(self):
        for field in ("seed_id", "record_seed_id"):
            for fallback in ({}, {"seed_index": 1}):
                with self.subTest(field=field, fallback=fallback):
                    self.assertIsNone(self.select_record({field: "missing", **fallback}))

    def test_primary_id_precedence_is_preserved(self):
        self.assertIs(
            self.select_record({"seed_id": "first", "record_seed_id": "second"}),
            self.first,
        )
        self.assertIsNone(
            self.select_record({"seed_id": "missing", "record_seed_id": "second"})
        )

    def test_empty_or_none_primary_id_uses_alias(self):
        for primary in ("", None):
            with self.subTest(primary=primary):
                self.assertIs(
                    self.select_record({"seed_id": primary, "record_seed_id": "second"}),
                    self.second,
                )

    def test_no_id_uses_seed_index(self):
        for empty_ids in ({}, {"seed_id": ""}, {"record_seed_id": None}):
            with self.subTest(empty_ids=empty_ids):
                self.assertIs(
                    self.select_record({**empty_ids, "seed_index": 1}), self.second,
                )

    def test_no_id_uses_position(self):
        for empty_ids in ({}, {"seed_id": None}, {"record_seed_id": ""}):
            with self.subTest(empty_ids=empty_ids):
                self.assertIs(self.select_record(empty_ids, index=1), self.second)

    def test_none_seed_index_uses_position(self):
        self.assertIs(self.select_record({"seed_index": None}, index=1), self.second)

    def test_unavailable_record_returns_none(self):
        self.assertIsNone(self.select_record({}, index=2))
        self.assertIsNone(self.select_record({"seed_index": 2}))
        self.assertIsNone(self.select_record({"seed_index": "invalid"}))
        self.assertIsNone(self.select(
            {}, index=0, seeded_records=[], records_by_seed_id={},
        ))


if __name__ == "__main__":
    unittest.main()
