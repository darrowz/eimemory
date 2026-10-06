"""Isolated regression for the drop-in discovery predicate only.

Never executes/sources the complete discovery script or uses real find/systemctl.
The selected source fragment runs with shell-builtin fixtures and no usable PATH.
"""
from pathlib import Path
import os
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get(
    "RUNTIME_DISCOVERY_FRAGMENT_SOURCE",
    str(ROOT / "deploy" / "discover_python_runtime_units.sh"),
))


def selected_fragment():
    lines = SOURCE.read_text().splitlines()
    anchor = next(i for i, line in enumerate(lines)
                  if "# A leftover identity drop-in" in line)
    start = anchor - 1
    if lines[start].strip() == 'if [ -n "$_matching_dropins" ]; then':
        start -= 1
    fragment = "\n".join(lines[start:anchor])
    # Keep the executable boundary narrow even if the production script changes.
    assert len(lines[start:anchor]) in (1, 2)
    assert lines[start].lstrip().startswith((
        'if find "$dropin_dir" ', '_matching_dropins="$(find "$dropin_dir" ',
    ))
    assert fragment.endswith("; then")
    assert "systemctl" not in fragment
    return fragment


def run_fragment(output, status):
    preamble = r'''set -euo pipefail
find() {
    printf '%s' "$FIXTURE_OUTPUT"
    return "$FIXTURE_STATUS"
}
# Baseline's grep is also a builtin-only stub. Consume all fixture bytes;
# decide whether any nonempty line occurred, without an early pipe close.
grep() {
    local line found=1
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ -n "$line" ]]; then found=0; fi
    done
    return "$found"
}
dropin_dir=/fixture/example.service.d
'''
    script = preamble + selected_fragment() + r'''
    printf '%s\n' matched
fi
printf '%s\n' completed
'''
    return subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", script],
        env={"PATH": "/nonexistent", "LC_ALL": "C",
             "FIXTURE_OUTPUT": output, "FIXTURE_STATUS": str(status)},
        text=True, capture_output=True, timeout=5, check=False,
    )


class DropinPredicateRegression(unittest.TestCase):
    def test_matching_output_enters_branch(self):
        result = run_fragment("/fixture/10-python-runtime.conf\n", 0)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, "matched\ncompleted\n", ""))

    def test_empty_success_skips_branch(self):
        result = run_fragment("", 0)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, "completed\n", ""))

    def test_enumeration_error_without_output_preserves_status(self):
        result = run_fragment("", 7)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (7, "", ""))

    def test_enumeration_error_after_output_preserves_status(self):
        result = run_fragment("/fixture/10-python-runtime.conf\n", 23)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (23, "", ""))


if __name__ == "__main__":
    unittest.main()
