"""Given-text line counts using only the target's isolated AST and stdlib."""
import ast
import copy
import difflib
from pathlib import Path
import unittest


SOURCE = (Path(__file__).resolve().parents[1]
          / "eimemory/adapters/hermes/code_implementation.py")


def isolated_counter():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    matches = [node for node in ast.walk(tree)
               if isinstance(node, ast.If)
               and isinstance(node.test, ast.Name)
               and node.test.id == "prior_content"]
    if len(matches) != 1:
        raise AssertionError("Expected exactly one prior_content counting block")
    block = matches[0]
    if (len(block.body) != 2
            or not isinstance(block.body[0], ast.Assign)
            or len(block.body[0].targets) != 1
            or not isinstance(block.body[0].targets[0], ast.Name)
            or block.body[0].targets[0].id != "diff_lines"
            or not isinstance(block.body[1], ast.AugAssign)
            or not isinstance(block.body[1].target, ast.Name)
            or block.body[1].target.id != "changed_lines"
            or len(block.orelse) != 1
            or not isinstance(block.orelse[0], ast.AugAssign)
            or not isinstance(block.orelse[0].target, ast.Name)
            or block.orelse[0].target.id != "changed_lines"):
        raise AssertionError("Unexpected counting block shape")
    isolated = ast.Module(body=[copy.deepcopy(block)], type_ignores=[])
    return compile(ast.fix_missing_locations(isolated), str(SOURCE), "exec")


COUNTER = isolated_counter()


def count_changes(prior_content, content, initial=0):
    scope = {"difflib": difflib, "prior_content": prior_content,
             "content": content, "changed_lines": initial}
    exec(COUNTER, scope)
    return scope["changed_lines"]


class GivenTextLineCountTests(unittest.TestCase):
    def test_ordinary_insert(self):
        self.assertEqual(count_changes("anchor", "anchor\nnew"), 1)

    def test_ordinary_delete(self):
        self.assertEqual(count_changes("anchor\nold", "anchor"), 1)

    def test_ordinary_replace(self):
        self.assertEqual(count_changes("old", "new"), 2)

    def test_add_content_starting_two_pluses(self):
        self.assertEqual(count_changes("anchor", "anchor\n++note"), 1)

    def test_remove_content_starting_two_minuses(self):
        self.assertEqual(count_changes("anchor\n--note", "anchor"), 1)

    def test_replace_prefixed_content(self):
        self.assertEqual(count_changes("--old", "++new"), 2)

    def test_header_like_content_is_still_content(self):
        self.assertEqual(count_changes("-- \nanchor", "++ \nanchor"), 2)

    def test_single_prefix_content(self):
        self.assertEqual(count_changes("-old", "+new"), 2)

    def test_identical_nonempty_text(self):
        self.assertEqual(count_changes("same\ntext", "same\ntext"), 0)

    def test_delete_to_empty_text(self):
        self.assertEqual(count_changes("old\ntext", ""), 2)

    def test_insert_blank_line(self):
        self.assertEqual(count_changes("anchor", "anchor\n\n"), 1)

    def test_line_ending_only_difference(self):
        self.assertEqual(count_changes("same\n", "same\r\n"), 0)

    def test_multiple_hunks(self):
        prior = "\n".join(["--old"] + [f"keep {i}" for i in range(20)] + ["end"])
        current = "\n".join(["start"] + [f"keep {i}" for i in range(20)] + ["++new"])
        self.assertEqual(count_changes(prior, current), 4)

    def test_accumulates_existing_count(self):
        self.assertEqual(count_changes("old", "new", initial=7), 9)

    def test_empty_prior_fallback_is_preserved(self):
        for content, expected in [("", 1), ("one", 1), ("one\ntwo", 1),
                                  ("one\ntwo\n", 2), ("++note", 1)]:
            with self.subTest(content=content):
                self.assertEqual(count_changes("", content), expected)


if __name__ == "__main__":
    unittest.main()
