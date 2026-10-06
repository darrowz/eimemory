"""Inert local recovery tests: AST-only target loading; no real target I/O."""
import ast
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import types
import unittest


SOURCE = Path(os.environ.get("EIMEMORY_PREPARE_SOURCE", Path(__file__).resolve().parents[1] / "deploy/prepare_qwen_reranker.py"))


class FakePath:
    def __init__(self, fs, name):
        self.fs, self._path = fs, str(name)

    def __str__(self):
        return self._path

    def __truediv__(self, other):
        return FakePath(self.fs, self._path.rstrip("/") + "/" + str(other))

    @property
    def name(self):
        return self._path.rsplit("/", 1)[-1]

    @property
    def parent(self):
        return FakePath(self.fs, self._path.rsplit("/", 1)[0])

    @property
    def suffix(self):
        return Path(self._path).suffix

    def with_suffix(self, suffix):
        return FakePath(self.fs, str(Path(self._path).with_suffix(suffix)))

    def exists(self):
        return self._path in self.fs.files or self._path in self.fs.dirs

    def is_file(self):
        return self._path in self.fs.files

    def is_symlink(self):
        return False

    def mkdir(self, mode=0o777, exist_ok=False):
        if self.exists() and not exist_ok:
            raise FileExistsError(self._path)
        self.fs.dirs.add(self._path)
        self.fs.modes[self._path] = mode

    def open(self, mode):
        if mode == "rb":
            return io.BytesIO(self.fs.files[self._path])
        assert mode == "xb", mode
        if self.exists():
            raise FileExistsError(self._path)
        self.fs.files[self._path] = b""
        fs, name = self.fs, self._path

        class Writer:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def write(self, value):
                if fs.write_error is not None:
                    error, fs.write_error = fs.write_error, None
                    raise error
                fs.files[name] += value
                return len(value)

        return Writer()

    def rename(self, other):
        target = str(other)
        if self.fs.rename_error is not None:
            error, self.fs.rename_error = self.fs.rename_error, None
            raise error
        assert not other.exists(), "tests never authorize replacement"
        if self._path in self.fs.files:
            self.fs.files[target] = self.fs.files.pop(self._path)
        else:
            assert self._path in self.fs.dirs
            for collection in [self.fs.files, self.fs.modes]:
                for old in list(collection):
                    if old == self._path or old.startswith(self._path + "/"):
                        collection[target + old[len(self._path):]] = collection.pop(old)
            for old in list(self.fs.dirs):
                if old == self._path or old.startswith(self._path + "/"):
                    self.fs.dirs.remove(old)
                    self.fs.dirs.add(target + old[len(self._path):])
        self.fs.renames.append((self._path, target))
        return other

    def chmod(self, mode):
        self.fs.modes[self._path] = mode

    def rglob(self, name):
        return [FakePath(self.fs, item) for item in sorted(self.fs.files) if item.startswith(self._path + "/") and item.rsplit("/", 1)[-1] == name]


class FakeIO:
    def __init__(self):
        self.files, self.dirs, self.modes = {}, set(), {}
        self.owned, self.cleaned, self.renames, self.urls, self.extractions = [], [], [], [], []
        self.responses, self.tar_outcomes = [], []
        self.write_error = self.rename_error = self.cleanup_error = None

    def path(self, name):
        return FakePath(self, name)

    def temporary_directory(self, *, dir, prefix):
        # Entire implementation is in memory; never invokes tempfile or the OS.
        name = str(dir) + "/" + prefix + str(len(self.owned) + 1)
        self.owned.append(name)
        self.dirs.add(name)
        fs = self

        class Temporary:
            def __init__(self):
                self.name = name

            def __enter__(self):
                return name

            def __exit__(self, *args):
                self.cleanup()
                return False

            def cleanup(self):
                if fs.cleanup_error is not None:
                    error, fs.cleanup_error = fs.cleanup_error, None
                    raise error
                fs.cleaned.append(name)
                for collection in [fs.files, fs.modes]:
                    for item in list(collection):
                        if item == name or item.startswith(name + "/"):
                            del collection[item]
                fs.dirs.difference_update({item for item in fs.dirs if item == name or item.startswith(name + "/")})
                return False

        return Temporary()

    def urlopen(self, url, timeout):
        self.urls.append((url, timeout))
        outcome = self.responses.pop(0)
        if isinstance(outcome, BaseException):
            class Broken(io.BytesIO):
                def read(self, size):
                    raise outcome
            return Broken()
        return io.BytesIO(outcome)

    def tar_open(self, archive):
        fs = self
        outcome = self.tar_outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome

        class Tar:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extractall(self, target, *, filter):
                fs.extractions.append((str(archive), str(target), filter))
                names, error = outcome
                for name in names:
                    fs.files[str(target / name)] = b"inert binary bytes"
                if error:
                    raise error

        return Tar()


def load_target(fs):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    allowed_constants = {"BUILD", "MODEL_REPO", "MODEL_REVISION", "MODEL_DIGEST", "ARCHIVE_DIGEST"}
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {"download", "main", "temporary_directory"}:
            selected.append(node)
        elif isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id in allowed_constants for t in node.targets):
            ast.literal_eval(node.value)
            selected.append(node)
    assert {"download", "main"} <= {node.name for node in selected if isinstance(node, ast.FunctionDef)}
    namespace = {"contextmanager": contextmanager, "sha256": hashlib.sha256, "json": json, "Path": fs.path,
                 "tempfile": types.SimpleNamespace(TemporaryDirectory=fs.temporary_directory),
                 "urllib": types.SimpleNamespace(request=types.SimpleNamespace(urlopen=fs.urlopen)),
                 "tarfile": types.SimpleNamespace(open=fs.tar_open), "print": lambda *a, **k: None}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeIO()
        self.ns = load_target(self.fs)
        self.target = self.fs.path("/fake/model.gguf")
        self.data = b"small inert payload"
        self.digest = hashlib.sha256(self.data).hexdigest()

    def download(self, **overrides):
        args = {"url": "inert://never-network", "target": self.target, "expected": self.digest, "size": len(self.data)}
        args.update(overrides)
        return self.ns["download"](**args)

    def assert_clean(self):
        self.assertEqual(self.fs.cleaned, self.fs.owned)
        for owner in self.fs.owned:
            self.assertFalse(any(p == owner or p.startswith(owner + "/") for p in self.fs.files.keys() | self.fs.dirs))

    def test_download_success_hash_mode(self):
        self.fs.responses = [self.data]
        self.assertIsNone(self.download())
        self.assertEqual(self.fs.files[str(self.target)], self.data)
        self.assertEqual(self.fs.modes[str(self.target)], 0o440)
        self.assertEqual(self.fs.urls, [("inert://never-network", 60)])
        self.assert_clean()

    def test_existing_target_keeps_digest_contract(self):
        self.fs.files[str(self.target)] = self.data
        self.assertIsNone(self.download())
        self.assertEqual(self.fs.urls, [])
        self.fs.files[str(self.target)] = b"wrong"
        with self.assertRaisesRegex(RuntimeError, "existing_artifact_digest_mismatch"):
            self.download()
        self.assertEqual(self.fs.files[str(self.target)], b"wrong")

    def test_two_download_failures_then_retry(self):
        marker = OSError("inert read failure")
        self.fs.responses = [marker, self.data + b"overflow", self.data]
        with self.assertRaises(OSError) as caught:
            self.download()
        self.assertIs(caught.exception, marker)
        self.assertFalse(self.target.exists())
        with self.assertRaisesRegex(RuntimeError, "artifact_size_exceeded"):
            self.download()
        self.assertFalse(self.target.exists())
        self.download()
        self.assertEqual(self.fs.files[str(self.target)], self.data)
        self.assert_clean()
        self.assertEqual(len(self.fs.owned), len(set(self.fs.owned)))

    def test_short_or_wrong_digest_cleanup_and_retry(self):
        for bad in [self.data[:-1], b"x" * len(self.data)]:
            with self.subTest(bad=bad):
                self.setUp()
                self.fs.responses = [bad, self.data]
                with self.assertRaisesRegex(RuntimeError, "download_artifact_mismatch"):
                    self.download()
                self.assertFalse(self.target.exists())
                self.download()
                self.assert_clean()

    def test_write_and_rename_errors_preserve_original(self):
        for field in ["write_error", "rename_error"]:
            with self.subTest(field=field):
                self.setUp()
                marker = OSError("inert " + field)
                setattr(self.fs, field, marker)
                self.fs.responses = [self.data, self.data]
                with self.assertRaises(OSError) as caught:
                    self.download()
                self.assertIs(caught.exception, marker)
                self.assertFalse(self.target.exists())
                self.download()
                self.assert_clean()

    def test_cleanup_failure_preserves_download_exception(self):
        marker = OSError("original download failure")
        self.fs.cleanup_error = OSError("secondary cleanup failure")
        self.fs.responses = [marker, self.data]
        with self.assertRaises(OSError) as caught:
            self.download()
        self.assertIs(caught.exception, marker)
        self.assertFalse(self.target.exists())
        self.download()
        self.assertEqual(self.fs.files[str(self.target)], self.data)

    def test_cleanup_failure_preserves_extract_exception(self):
        final, _, reports = self.setup_main()
        marker = OSError("original extraction failure")
        self.fs.cleanup_error = OSError("secondary cleanup failure")
        self.fs.tar_outcomes = [(["llama-server"], marker), (["llama-server"], None)]
        with self.assertRaises(OSError) as caught:
            self.ns["main"]()
        self.assertIs(caught.exception, marker)
        self.assertFalse(self.fs.path(final).exists())
        self.assertEqual(reports, [])
        self.ns["main"]()
        self.assertEqual(reports[0][0]["server"], final + "/llama-server")

    def test_cleanup_failure_after_success_is_not_silenced(self):
        marker = OSError("cleanup after publication")
        self.fs.cleanup_error = marker
        self.fs.responses = [self.data]
        with self.assertRaises(OSError) as caught:
            self.download()
        self.assertIs(caught.exception, marker)
        self.assertEqual(self.fs.files[str(self.target)], self.data)
        self.assertEqual(self.fs.modes[str(self.target)], 0o440)

    def test_preexisting_partial_not_deleted_or_overwritten(self):
        old = str(self.target.with_suffix(self.target.suffix + ".partial"))
        self.fs.files[old] = b"historical partial"
        self.fs.responses = [self.data]
        self.download()
        self.assertEqual(self.fs.files[old], b"historical partial")
        self.assert_clean()

    def setup_main(self):
        calls, reports = [], []
        self.ns["download"] = lambda *args: calls.append(tuple(map(str, args)))
        self.ns["print"] = lambda text, **kwargs: reports.append((json.loads(text), kwargs))
        root = "/var/lib/eimemory-reranker/qwen3"
        return root + "/" + self.ns["BUILD"], calls, reports

    def test_extract_two_failures_then_retry(self):
        final, calls, reports = self.setup_main()
        first, second = OSError("first inert extraction"), OSError("second inert extraction")
        self.fs.tar_outcomes = [([], first), (["llama-server"], second), (["llama-server", "support.dat"], None)]
        for error in [first, second]:
            with self.assertRaises(OSError) as caught:
                self.ns["main"]()
            self.assertIs(caught.exception, error)
            self.assertFalse(self.fs.path(final).exists())
            self.assertEqual(reports, [])
        self.ns["main"]()
        self.assertTrue(self.fs.path(final).exists())
        self.assertEqual(self.fs.modes[final], 0o750)
        self.assertIn(final + "/support.dat", self.fs.files)
        self.assertTrue(all(row[2] == "data" for row in self.fs.extractions))
        self.assertEqual(len(calls), 6)
        self.assertEqual(calls[0][2:], (self.ns["ARCHIVE_DIGEST"], "16734586"))
        self.assertEqual(calls[1][2:], (self.ns["MODEL_DIGEST"], "639153184"))
        report, options = reports[0]
        self.assertEqual(report["server"], final + "/llama-server")
        self.assertEqual(set(report), {"server", "model", "model_repo", "model_revision", "model_digest", "build", "archive_digest"})
        self.assertEqual(report["model_digest"], self.ns["MODEL_DIGEST"])
        self.assertEqual(report["archive_digest"], self.ns["ARCHIVE_DIGEST"])
        self.assertEqual(options, {"flush": True})
        self.assert_clean()

    def test_missing_or_duplicate_server_does_not_publish(self):
        for names in [[], ["one/llama-server", "two/llama-server"]]:
            with self.subTest(names=names):
                self.setUp()
                final, _, reports = self.setup_main()
                self.fs.tar_outcomes = [(names, None)]
                with self.assertRaisesRegex(RuntimeError, "server_binary_missing"):
                    self.ns["main"]()
                self.assertFalse(self.fs.path(final).exists())
                self.assertEqual(reports, [])
                self.assert_clean()

    def test_tar_open_failure_then_retry(self):
        final, _, reports = self.setup_main()
        marker = OSError("inert tar open failure")
        self.fs.tar_outcomes = [marker, (["llama-server"], None)]
        with self.assertRaises(OSError) as caught:
            self.ns["main"]()
        self.assertIs(caught.exception, marker)
        self.assertFalse(self.fs.path(final).exists())
        self.ns["main"]()
        self.assertEqual(reports[0][0]["server"], final + "/llama-server")
        self.assert_clean()

    def test_existing_complete_directory_reports_final_path(self):
        final, _, reports = self.setup_main()
        self.fs.dirs.add(final)
        self.fs.files[final + "/llama-server"] = b"existing inert server"
        self.ns["main"]()
        self.assertEqual(reports[0][0]["server"], final + "/llama-server")
        self.assertEqual(self.fs.files[final + "/llama-server"], b"existing inert server")
        self.assertEqual(self.fs.extractions, [])
        self.assertEqual(self.fs.owned, [])

    def test_existing_final_directory_is_untouched(self):
        final, _, reports = self.setup_main()
        self.fs.dirs.add(final)
        self.fs.files[final + "/old.dat"] = b"historical data"
        with self.assertRaisesRegex(RuntimeError, "server_binary_missing"):
            self.ns["main"]()
        self.assertEqual(self.fs.files[final + "/old.dat"], b"historical data")
        self.assertIn(final, self.fs.dirs)
        self.assertEqual(self.fs.extractions, [])
        self.assertEqual(self.fs.owned, [])
        self.assertEqual(reports, [])


if __name__ == "__main__":
    unittest.main()
