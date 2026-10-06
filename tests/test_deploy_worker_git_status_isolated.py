"""Exercise only the approved worker git-status fragment with a fake git function."""

from pathlib import Path
import shutil
import subprocess
import unittest


SOURCE = Path(__file__).parent.parent / "deploy" / "eimemory-deploy-worker"
CANDIDATE = (
    'worktree_status="$(git status --porcelain=v1)" || exit "$?"\n'
    'test -z "$worktree_status"\n'
)
BASELINE = 'test -z "$(git status --porcelain=v1)"\n'
OUTPUTS = ("", "\n", "\n\n", " M file", " M file\n\n")
PREFIX = r'''set -e
fake_rc="$1"
fake_stdout="$2"
git() {
  if [[ "$#" != 2 || "$1" != status || "$2" != --porcelain=v1 ]]; then
    printf 'unexpected fake git arguments\n' >&2
    return 99
  fi
  printf '%s' "$fake_stdout"
  return "$fake_rc"
}
'''
SUFFIX = "printf 'REACHED\\n'\n"


def extract_status_check(data):
    """Fail closed if the exact two approved lines moved or changed."""
    expected = CANDIDATE.encode("ascii")
    lines = data.splitlines(keepends=True)
    if b"".join(lines[28:30]) != expected or data.count(expected) != 1:
        raise ValueError("Expected the unique approved git-status check at lines 29-30")
    return CANDIDATE


class GitStatusIsolatedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        if cls.bash is None:
            raise unittest.SkipTest("Installed Bash is required; no dependency is installed")
        cls.fragment = extract_status_check(SOURCE.read_bytes())

    def run_fragment(self, fragment, return_code, output):
        # No inherited environment, exported functions, BASH_ENV, or external PATH.
        result = subprocess.run(
            [self.bash, "--noprofile", "--norc", "-c", PREFIX + fragment + SUFFIX,
             "isolated-git-status", str(return_code), output],
            env={"PATH": "/nonexistent"},
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result

    def assert_result(self, fragment, return_code, output, expected):
        result = self.run_fragment(fragment, return_code, output)
        self.assertEqual(result.returncode, expected)
        self.assertEqual(result.stdout, "REACHED\n" if expected == 0 else "")
        self.assertEqual(result.stderr, "")

    def test_success_preserves_empty_dirty_and_trailing_newlines(self):
        for output in OUTPUTS:
            expected = 0 if output.rstrip("\n") == "" else 1
            for variant, fragment in (("baseline", BASELINE), ("candidate", self.fragment)):
                with self.subTest(variant=variant, output=repr(output)):
                    self.assert_result(fragment, 0, output, expected)

    def test_nonzero_status_is_preserved(self):
        for return_code in (1, 7, 128, 127):
            for output in OUTPUTS:
                baseline_exit = 0 if output.rstrip("\n") == "" else 1
                with self.subTest(variant="baseline", return_code=return_code, output=repr(output)):
                    self.assert_result(BASELINE, return_code, output, baseline_exit)
                with self.subTest(variant="candidate", return_code=return_code, output=repr(output)):
                    self.assert_result(self.fragment, return_code, output, return_code)

    def test_extractor_rejects_changed_moved_and_duplicate_fragments(self):
        approved = CANDIDATE.encode("ascii")
        valid = b"\n" * 28 + approved
        self.assertEqual(extract_status_check(valid), CANDIDATE)
        invalid_inputs = (
            b"",
            b"\n" + valid,
            valid.replace(b' || exit "$?"', b""),
            valid + approved,
            valid.replace(b"\n", b"\r\n"),
        )
        for invalid in invalid_inputs:
            with self.subTest(data=repr(invalid)):
                with self.assertRaises(ValueError):
                    extract_status_check(invalid)


if __name__ == "__main__":
    unittest.main()
