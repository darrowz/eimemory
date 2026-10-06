"""Inert regression: no target imports, real Git commands, or environment reads."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


class FakeRepoPath:
    def __truediv__(self, part):
        return self

    def exists(self):
        return True

    def __str__(self):
        return "/synthetic-repository"


class PromotionGitDiffExitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "eimemory/governance/promotion/promotion_git_ops.py"
        module = ast.parse(path.read_text(encoding="utf-8"))
        nodes = [node for node in module.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"_truthy", "_commit_repo_patch"}]
        cls.code = compile(ast.fix_missing_locations(ast.Module(
            body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes],
            type_ignores=[])), "<isolated promotion commit ordinary flow>", "exec")

    def exercise(self, returncode):
        commands = []
        subprocess_calls = []

        def run_commands(argv, **kwargs):
            commands.extend(argv)
            return {"ok": True, "reports": [{"phase": "synthetic"}]}

        def run(argv, **kwargs):
            subprocess_calls.append(argv)
            if argv == ["git", "diff", "--cached", "--quiet"]:
                return SimpleNamespace(returncode=returncode, stdout="", stderr="e" * 4100)
            if argv == ["git", "rev-parse", "HEAD"]:
                return SimpleNamespace(returncode=0, stdout="synthetic-sha\n", stderr="")
            raise AssertionError("Unexpected fake subprocess argv")

        namespace = {"subprocess": SimpleNamespace(run=run), "_run_patch_commands": run_commands}
        exec(self.code, namespace)
        result = namespace["_commit_repo_patch"](
            FakeRepoPath(), applied_paths=["synthetic.py"],
            patch={"commit_to_repo": True, "commit_message": "synthetic commit"},
            candidate=SimpleNamespace(record_id="synthetic-id"), timeout_seconds=1,
            automation_policy={"allow_commit": True},
        )
        return result, commands, subprocess_calls

    def test_zero_skips_commit(self):
        result, commands, calls = self.exercise(0)
        self.assertEqual(result["reason"], "no_staged_changes")
        self.assertEqual(len(commands), 1)
        self.assertEqual(len(calls), 1)

    def test_one_commits(self):
        result, commands, calls = self.exercise(1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["commit_sha"], "synthetic-sha")
        self.assertEqual(commands[-1], ["git", "commit", "-m", "synthetic commit"])
        self.assertEqual(len(calls), 2)

    def test_other_codes_preserve_error_without_commit(self):
        for code in (2, 128, -9):
            with self.subTest(returncode=code):
                result, commands, calls = self.exercise(code)
                self.assertFalse(result["ok"])
                self.assertEqual(result["reason"], "git_diff_failed")
                self.assertEqual(result["returncode"], code)
                self.assertEqual(result["stderr"], "e" * 4000)
                self.assertEqual(result["reports"], [{"phase": "synthetic"}])
                self.assertEqual(len(commands), 1)
                self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
