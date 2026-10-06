"""EA-114: inspect source as AST; run only reviewed helpers on synthetic trees.

No project imports, real-checkout traversal, or allowlist writer execution.
EA114_SOURCE optionally selects a frozen before/after source for comparison.
"""

import ast
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
import unittest


SOURCE = Path(os.environ.get(
    "EA114_SOURCE",
    str(Path(__file__).resolve().parents[1] / "eimemory/core/wiring_audit.py"),
))
SKIP_NAMES = (
    ".git", ".worktrees", ".venv", "venv", "__pycache__",
    "node_modules", "dist", "build", ".mypy_cache", ".pytest_cache",
)
SELECTED_NAMES = {
    "_ENTRY_DECORATORS", "_SKIP_DIRS", "_decorator_name", "_is_entry",
    "_python_files", "_references", "unwired_public_functions",
    "relative_in_package", "load_wiring_allowlist",
}
# ast.dump() output differs across Python minor versions, so each reviewed
# shape (pre-fix, post-fix) is pinned once per dump format. The 3.13+ digests
# were computed from the same two sources (fd5cb5fc^ and fd5cb5fc).
REVIEWED_AST_HASHES = {
    # Python <= 3.12
    "e6446211a14dce80765a73cb406c1dc815aa636648b00c4bfb928e4304090852",
    "c22508e938822ec524e6b3d1b5e4cfe44092c26df0737a8c864bdf219602540e",
    # Python >= 3.13 (production runs 3.14)
    "159815281896e0fee5e38990ad6ebe67ba8c79a0a60caa04edeaefb312ed366d",
    "ed57818fa27625d0452fcfe7f0e06437525b76beddb52587395281a7bb0a90ef",
}


def load_preinspected_helpers():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in SELECTED_NAMES:
            selected.append(node)
        elif (
            isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in SELECTED_NAMES
        ):
            selected.append(node)
    if len(selected) != len(SELECTED_NAMES):
        raise AssertionError("Missing or duplicated reviewed helper/constant")
    module = ast.Module(body=selected, type_ignores=[])
    digest = hashlib.sha256(ast.dump(module, include_attributes=False).encode()).hexdigest()
    if digest not in REVIEWED_AST_HASHES:
        raise AssertionError("Selected source changed beyond the reviewed EA-114 shapes")
    namespace = {
        "ast": ast,
        "Path": Path,
        "__builtins__": {
            "any": any, "isinstance": isinstance, "getattr": getattr,
            "frozenset": frozenset, "int": int, "str": str, "bool": bool,
            "list": list, "tuple": tuple, "dict": dict, "set": set,
            "OSError": OSError, "UnicodeError": UnicodeError,
            "SyntaxError": SyntaxError, "ValueError": ValueError,
        },
    }
    exec(compile(module, str(SOURCE) + ":reviewed-helpers-only", "exec"), namespace)
    return namespace


@contextmanager
def working_directory(path):
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def write_fixture(root, relative, text="pass\n"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestEA114WiringRootFilter(unittest.TestCase):
    def setUp(self):
        self.helpers = load_preinspected_helpers()
        self.temp = tempfile.TemporaryDirectory(prefix="ea114_synthetic_")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def files(self, root):
        return {path.relative_to(root).as_posix() for path in self.helpers["_python_files"](root)}

    def unwired(self, root):
        return self.helpers["unwired_public_functions"](root)

    def make_checkout(self, root):
        write_fixture(root, "eimemory/api.py", "def orphan():\n    return 1\n")
        write_fixture(root, "tests/caller.py")
        write_fixture(root, "notes.txt", "orphan\n")

    def assert_checkout_visible(self, root):
        self.assertEqual(self.files(root), {"eimemory/api.py", "tests/caller.py"})
        self.assertEqual(self.unwired(root), {"eimemory/api.py:orphan"})

    def test_each_excluded_ancestor_with_absolute_relative_and_dot_roots(self):
        self.assertEqual(set(SKIP_NAMES), set(self.helpers["_SKIP_DIRS"]))
        for name in SKIP_NAMES:
            root = self.base / name / "checkout"
            self.make_checkout(root)
            for form in ("absolute", "relative", "dot"):
                with self.subTest(ancestor=name, root_form=form):
                    if form == "absolute":
                        self.assert_checkout_visible(root)
                    elif form == "relative":
                        with working_directory(self.base):
                            self.assert_checkout_visible(Path(name) / "checkout")
                    else:
                        with working_directory(root):
                            self.assert_checkout_visible(Path("."))

    def test_checkout_root_can_itself_have_each_excluded_name(self):
        for name in SKIP_NAMES:
            root = self.base / "root_name_cases" / name
            self.make_checkout(root)
            with self.subTest(root_name=name):
                self.assert_checkout_visible(root)

    def test_nested_excluded_children_stay_ignored_under_each_root_form(self):
        root = self.base / "build" / "checkout"
        self.make_checkout(root)
        for name in SKIP_NAMES:
            write_fixture(root, f"{name}/caller.py", "orphan\n")
            write_fixture(root, f"eimemory/deep/{name}/hidden.py", "def hidden():\n    pass\n")
        for form in ("absolute", "relative", "dot"):
            with self.subTest(root_form=form):
                if form == "absolute":
                    self.assert_checkout_visible(root)
                elif form == "relative":
                    with working_directory(self.base):
                        self.assert_checkout_visible(Path("build/checkout"))
                else:
                    with working_directory(root):
                        self.assert_checkout_visible(Path("."))

    def test_exclusions_are_exact_components_not_substrings(self):
        root = self.base / "checkout"
        expected = set()
        for name in SKIP_NAMES:
            for relative in (f"{name}_keep/file.py", f"{name}.py"):
                write_fixture(root, relative)
                expected.add(relative)
        self.assertEqual(self.files(root), expected)

    def test_empty_or_missing_synthetic_root(self):
        self.assertEqual(self.files(self.base), set())
        self.assertEqual(self.unwired(self.base), set())
        self.assertEqual(self.files(self.base / "missing"), set())

    def test_name_attribute_import_and_same_file_references_remain_wired(self):
        root = self.base / "checkout"
        names = ("loaded", "attribute", "imported", "same_file", "shared", "orphan")
        source = "".join(f"def {name}():\n    pass\n" for name in names)
        source += "same_file\ndef self_only():\n    return self_only()\n"
        source += "def recursive():\n    return recursive()\n"
        write_fixture(root, "eimemory/api.py", source)
        write_fixture(root, "eimemory/other.py", "def shared():\n    pass\n")
        write_fixture(root, "tests/caller.py", "loaded\nobj.attribute\nfrom api import imported as alias\nshared\n")
        self.assertEqual(self.unwired(root), {
            "eimemory/api.py:recursive", "eimemory/api.py:self_only", "eimemory/api.py:orphan",
        })

    def test_private_nested_class_and_outside_package_definitions_ignored(self):
        root = self.base / "checkout"
        write_fixture(root, "eimemory/api.py", (
            "def _private():\n    pass\n"
            "class Container:\n    def method(self):\n        pass\n"
            "def outer():\n    def nested():\n        pass\n"
            "async def async_orphan():\n    pass\n"
        ))
        write_fixture(root, "tests/helper.py", "def test_helper():\n    pass\n")
        write_fixture(root, "eimemory_extra/helper.py", "def not_package():\n    pass\n")
        self.assertEqual(self.unwired(root), {
            "eimemory/api.py:outer", "eimemory/api.py:async_orphan",
        })

    def test_registration_decorators_still_own_entry_points(self):
        root = self.base / "checkout"
        source = ""
        for index, name in enumerate(sorted(self.helpers["_ENTRY_DECORATORS"])):
            for form, decorator in enumerate((name, name + "()", "registry." + name, "registry." + name + "()")):
                source += f"@{decorator}\ndef entry_{index}_{form}():\n    pass\n"
        source += "@unknown\ndef ordinary():\n    pass\n"
        write_fixture(root, "eimemory/api.py", source)
        self.assertEqual(self.unwired(root), {"eimemory/api.py:ordinary"})

    def test_package_syntax_error_raises_even_under_excluded_ancestor(self):
        root = self.base / "build" / "checkout"
        write_fixture(root, "eimemory/broken.py", "def (\n")
        with self.assertRaises(SyntaxError):
            self.unwired(root)

    def test_external_and_excluded_syntax_errors_remain_ignored(self):
        root = self.base / "checkout"
        self.make_checkout(root)
        write_fixture(root, "tests/broken.py", "def (\n")
        write_fixture(root, "eimemory/build/broken.py", "def (\n")
        self.assertEqual(self.unwired(root), {"eimemory/api.py:orphan"})

    def test_unicode_read_error_remains_skipped(self):
        root = self.base / "checkout"
        self.make_checkout(root)
        (root / "eimemory/unreadable.py").write_bytes(b"\xff\xfe")
        self.assertEqual(self.unwired(root), {"eimemory/api.py:orphan"})

    def test_oserror_remains_skipped_using_inert_path_stub(self):
        class UnreadablePath:
            def read_text(self, *, encoding):
                raise OSError("controlled read failure")

        self.helpers["_python_files"] = lambda root: [UnreadablePath()]
        self.assertEqual(self.unwired(self.base), set())

    def test_package_boundary_and_allowlist_reader_are_unchanged(self):
        root = self.base / "checkout"
        is_package = self.helpers["relative_in_package"]
        self.assertTrue(is_package(root / "eimemory/api.py", root))
        self.assertFalse(is_package(root / "eimemory_extra/api.py", root))
        self.assertFalse(is_package(self.base / "elsewhere/api.py", root))
        path = write_fixture(root, "allowlist.txt", " # comment\n\n a:b \na:b\n c:d\n")
        self.assertEqual(self.helpers["load_wiring_allowlist"](path), {"a:b", "c:d"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
