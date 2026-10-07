"""In-memory scanner regressions; never enumerate the real checkout."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path, PurePosixPath
import unittest


class _File:
    def __init__(self, relative, text="", error=None):
        self.relative = PurePosixPath(relative)
        self.text = text
        self.error = error
        self.reads = 0

    def relative_to(self, root):
        assert self in root.files
        return self.relative

    def read_text(self, *, encoding):
        assert encoding == "utf-8"
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.text


class _Root:
    def __init__(self, *files):
        self.files = files

    def rglob(self, pattern):
        assert pattern == "*.py"
        return iter(self.files)


class WiringReadErrorsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Lazy file loading avoids package initialization and CLI execution.
        source = Path(__file__).resolve().parents[1] / "eimemory/core/wiring_audit.py"
        spec = spec_from_file_location("_wiring_read_errors_under_test", source)
        cls.audit = module_from_spec(spec)
        spec.loader.exec_module(cls.audit)

    def _assert_package_error(self, error):
        file = _File("eimemory/broken.py", error=error)
        with self.assertRaises(type(error)) as raised:
            self.audit.unwired_public_functions(_Root(file))
        self.assertIs(raised.exception, error)
        self.assertEqual(file.reads, 1)

    def test_package_os_error_propagates(self):
        self._assert_package_error(OSError("unreadable source"))

    def test_package_permission_error_propagates(self):
        self._assert_package_error(PermissionError("denied source"))

    def test_package_unicode_error_propagates(self):
        self._assert_package_error(UnicodeError("invalid text"))

    def test_package_decode_error_propagates(self):
        self._assert_package_error(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"))

    def test_partial_findings_cannot_hide_package_read_error(self):
        valid = _File("eimemory/ok.py", "def unused():\n    pass\n")
        error = OSError("later unreadable source")
        broken = _File("eimemory/broken.py", error=error)
        with self.assertRaises(OSError) as raised:
            self.audit.unwired_public_functions(_Root(valid, broken))
        self.assertIs(raised.exception, error)
        self.assertEqual(valid.reads, 1)

    def test_outside_package_read_errors_remain_skipped(self):
        for relative in ("tests/broken.py", "eimemory_extra/broken.py", "eimemory.py"):
            for error in (OSError("missing"), UnicodeError("bad text"),
                          UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte")):
                with self.subTest(relative=relative, error=type(error).__name__):
                    broken = _File(relative, error=error)
                    valid = _File("eimemory/ok.py", "def unused():\n    pass\n")
                    self.assertEqual(self.audit.unwired_public_functions(_Root(broken, valid)),
                                     {"eimemory/ok.py:unused"})
                    self.assertEqual(broken.reads, 1)

    def test_package_syntax_error_still_propagates(self):
        with self.assertRaises(SyntaxError):
            self.audit.unwired_public_functions(_Root(_File("eimemory/broken.py", "def :")))

    def test_outside_package_syntax_error_still_skipped(self):
        broken = _File("tests/broken.py", "def :")
        valid = _File("eimemory/ok.py", "def unused():\n    pass\n")
        self.assertEqual(self.audit.unwired_public_functions(_Root(broken, valid)),
                         {"eimemory/ok.py:unused"})

    def test_successful_scan_and_external_caller_unchanged(self):
        source = _File("eimemory/ok.py", "def used():\n    pass\ndef unused():\n    pass\n")
        caller = _File("tests/caller.py", "used()\n")
        self.assertEqual(self.audit.unwired_public_functions(_Root(source, caller)),
                         {"eimemory/ok.py:unused"})

    def test_excluded_paths_are_not_read(self):
        ignored = _File("eimemory/.worktrees/broken.py", error=OSError("must not read"))
        self.assertEqual(self.audit.unwired_public_functions(_Root(ignored)), set())
        self.assertEqual(ignored.reads, 0)
