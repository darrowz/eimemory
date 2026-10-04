"""EA-006 standalone synthetic checks; never imports or executes project modules."""
import ast
from pathlib import Path
import unittest


LABEL_PATH = Path(__file__).resolve().parents[1] / "eimemory/scoring/labels.py"
FUNCTIONS = {
    "_band_label", "relevance_label", "freshness_label", "confidence_label",
    "salience_label", "reuse_label", "lifecycle_label",
}


def extracted_labels():
    tree = ast.parse(LABEL_PATH.read_text(encoding="utf-8"))
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in FUNCTIONS]
    assert {n.name for n in selected} == FUNCTIONS
    allowed = {
        ast.FunctionDef, ast.arguments, ast.arg, ast.If, ast.Return, ast.Compare,
        ast.Name, ast.Load, ast.Constant, ast.GtE, ast.JoinedStr, ast.FormattedValue,
        ast.Call, ast.keyword, ast.BoolOp, ast.Or, ast.Attribute,
    }
    for function in selected:
        assert not function.decorator_list
        for node in ast.walk(function):
            assert type(node) in allowed, type(node).__name__
            if isinstance(node, ast.Call):
                assert not node.keywords or all(k.arg in {"low", "medium", "high"} for k in node.keywords)
                if isinstance(node.func, ast.Name):
                    assert node.func.id in {"str", "_band_label"}
                else:
                    assert isinstance(node.func, ast.Attribute)
                    assert node.func.attr in {"strip", "lower"}
                    assert not node.args and not node.keywords
            if isinstance(node, ast.Attribute):
                assert node.attr in {"strip", "lower"}
    # Only proven pure label functions are compiled; no imports or module body.
    namespace = {"__builtins__": {"str": str, "float": float}}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(LABEL_PATH), "exec"), namespace)
    return namespace


class EA006LabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.labels = extracted_labels()

    def test_lifecycle_defaults_and_normalization(self):
        fn = self.labels["lifecycle_label"]
        for value in ("", " ", "\t\n", "  \t  "):
            with self.subTest(value=value):
                self.assertEqual(fn(value), "lifecycle.candidate")
        for tier in ("core", "confirmed", "candidate", "rejected"):
            self.assertEqual(fn("  " + tier.upper() + "  "), "lifecycle." + tier)

    def test_ordinary_numeric_band_boundaries(self):
        for name in ("confidence", "salience", "reuse"):
            fn = self.labels[name + "_label"]
            for value, suffix in ((0.0, "low"), (0.3999, "low"), (0.4, "medium"),
                                  (0.7499, "medium"), (0.75, "high"), (1.0, "high")):
                self.assertEqual(fn(value), name + "." + suffix)
        for value, suffix in ((0.0, "none"), (0.2499, "none"), (0.25, "partial"),
                              (0.7499, "partial"), (0.75, "high"), (1.0, "high")):
            self.assertEqual(self.labels["relevance_label"](value), "relevance." + suffix)
        for value, suffix in ((0.0, "stale"), (0.3999, "stale"), (0.4, "stable"),
                              (0.7499, "stable"), (0.75, "recent"), (1.0, "recent")):
            self.assertEqual(self.labels["freshness_label"](value), "freshness." + suffix)


if __name__ == "__main__":
    unittest.main()
