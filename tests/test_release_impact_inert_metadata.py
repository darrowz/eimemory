"""Ordinary local semantics only: no target imports, git, or release operations."""
from __future__ import annotations
import ast
from copy import deepcopy
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import tomllib
from typing import Any
import unittest


class FakePath:
    directory = True

    def __init__(self, value):
        self.value = str(value)

    def is_dir(self):
        return self.directory

    def __str__(self):
        return self.value


class InertReleaseImpactTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ.get('RELEASE_IMPACT_SOURCE', str(Path(__file__).resolve().parents[1] / 'eimemory/governance/release/release_impact.py')))
        tree = ast.parse(path.read_text(encoding='utf-8'))
        tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
        self.calls = []
        self.result = SimpleNamespace(stdout=b'', returncode=0)
        self.error = None

        def run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            if self.error:
                raise self.error
            return self.result

        self.subprocess = SimpleNamespace(run=run, DEVNULL=-3, SubprocessError=RuntimeError)
        self.ns = dict(ast=ast, deepcopy=deepcopy, json=json, Path=FakePath, re=re,
                       subprocess=self.subprocess, tomllib=tomllib, Any=Any)
        FakePath.directory = True
        exec(compile(tree, '<inert-release-impact>', 'exec'), self.ns)

    def pair(self, name, path, before, after):
        values = iter([before, after])
        self.ns['_git_bytes'] = lambda *args: next(values)
        return self.ns[name]('inert', path=path, ancestor='a', current='b')

    def test_json_boolean_number_changes(self):
        for before, after in [(True, 1), (False, 0), (1, True), (0.0, False)]:
            with self.subTest(before=before, after=after):
                self.assertFalse(self.pair('_integration_version_only_change', 'plugin.json',
                    json.dumps({'version': '1', 'nested': [{'value': before}]}).encode(),
                    json.dumps({'version': '2', 'nested': [{'value': after}]}).encode()))

    def test_toml_boolean_number_changes(self):
        for before, after in [('true', '1'), ('false', '0'), ('1', 'true')]:
            with self.subTest(before=before, after=after):
                self.assertFalse(self.pair('_version_metadata_only_change', 'pyproject.toml',
                    ('[project]\nversion="1"\n[tool.extra]\nvalues=['+before+']\n').encode(),
                    ('[project]\nversion="2"\n[tool.extra]\nvalues=['+after+']\n').encode()))

    def test_json_version_only_and_order(self):
        self.assertTrue(self.pair('_integration_version_only_change', 'plugin.json',
            b'{"version":"1","a":[true,1],"b":2}', b'{"b":2,"a":[true,1],"version":"2"}'))

    def test_json_nan_identity_preserved(self):
        # json.loads uses its shared NaN object; container equality accepts it.
        self.assertTrue(self.pair('_integration_version_only_change', 'plugin.json',
            b'{"version":"1","nested":[{"value":NaN}]}',
            b'{"version":"2","nested":[{"value":NaN}]}'))

    def test_numeric_representation_preserved(self):
        self.assertTrue(self.pair('_integration_version_only_change', 'plugin.json', b'{"x":1}', b'{"x":1.0}'))

    def test_json_shape_and_array_order(self):
        for before, after in [(b'{}', b'[]'), (b'{"a":1}', b'{"b":1}'), (b'{"a":[1,2]}', b'{"a":[2,1]}'), (b'{"a":[]}', b'{"a":[1]}')]:
            self.assertFalse(self.pair('_integration_version_only_change', 'plugin.json', before, after))

    def test_missing_invalid_and_utf8(self):
        for name, path in [('_version_metadata_only_change', 'pyproject.toml'), ('_integration_version_only_change', 'plugin.json')]:
            for before, after in [(None, b'{}'), (b'{}', None), (b'\xff', b'{}'), (b'[', b'{}')]:
                self.assertFalse(self.pair(name, path, before, after))

    def test_toml_only_version_and_other_change(self):
        self.assertTrue(self.pair('_version_metadata_only_change', 'pyproject.toml', b'[project]\nversion="1"\nname="x"', b'[project]\nversion="2"\nname="x"'))
        self.assertFalse(self.pair('_version_metadata_only_change', 'pyproject.toml', b'[project]\nname="x"', b'[project]\nname="y"'))

    def test_yaml_lines(self):
        self.assertTrue(self.pair('_integration_version_only_change', 'plugin.yaml', b'version: 1\nname: x  \n', b'version: 2\nname: x\n'))
        self.assertFalse(self.pair('_integration_version_only_change', 'plugin.yaml', b'version: 1\nname: x', b'version: 2\nname: y'))

    def test_python_plain_version(self):
        self.assertTrue(self.pair('_version_metadata_only_change', 'version.py', b'__version__ = "1"\n', b'__version__ = "2"\n'))
        self.assertTrue(self.pair('_version_metadata_only_change', 'version.py', b'__version__: str = "1"', b'__version__: str = "2"'))

    def test_python_nonplain_versions(self):
        for before, after in [(b'__version__ = x = "1"', b'__version__ = x = "2"'), (b'__version__ = "1"\n__version__="x"', b'__version__ = "2"\n__version__="x"'), (b'__version__ = f("1")', b'__version__ = f("2")'), (b'if x:\n __version__="1"', b'if x:\n __version__="2"'), (b'__version__: A="1"', b'__version__: B="2"')]:
            self.assertFalse(self.pair('_version_metadata_only_change', 'version.py', before, after))

    def test_path_matching(self):
        f = self.ns['_path_matches_rule']
        self.assertTrue(f('a/b', 'a'))
        self.assertTrue(f('a/b', 'a/'))
        self.assertTrue(f('a-file', 'a-'))
        self.assertTrue(f('a_file', 'a_'))
        self.assertFalse(f('ab', 'a'))
        self.assertTrue(f('a.py', 'a.py'))

    def test_changed_paths(self):
        self.ns['_git_bytes'] = lambda *a: b'z\0a\xff\0\0'
        self.assertEqual(self.ns['_changed_paths']('inert', 'a', 'b'), ['a\udcff', 'z'])
        self.ns['_git_bytes'] = lambda *a: None
        self.assertIsNone(self.ns['_changed_paths']('inert', 'a', 'b'))

    def test_git_result_and_exceptions(self):
        f = self.ns['_git_bytes']
        self.result = SimpleNamespace(stdout=b'\xff', returncode=0)
        self.assertEqual(f('inert', 'show', 'a'), b'\xff')
        self.assertEqual(self.calls[-1], (['git', '-C', 'inert', 'show', 'a'], dict(check=False, stdin=-3, capture_output=True, timeout=10)))
        self.result.returncode = 1
        self.assertIsNone(f('inert'))
        for error in [OSError('fake'), RuntimeError('fake')]:
            self.error = error
            self.assertIsNone(f('inert'))

    def test_summary_containers(self):
        self.ns['_changed_paths'] = lambda *a: ['a', 'b', 'c']
        self.ns['_domains_for_change'] = lambda *a, **kw: {'memory.recall'} if kw['path'] == 'a' else set()
        self.ns['_ignored_change'] = lambda *a, **kw: kw['path'] == 'b'
        self.assertEqual(self.ns['_release_change_summary']('x', ancestor='a', current='b'), (['a','b','c'], {'a':{'memory.recall'},'b':set(),'c':set()}, ['c']))
        self.ns['_changed_paths'] = lambda *a: None
        self.assertIsNone(self.ns['_release_change_summary']('x', ancestor='a', current='b'))

    def test_domain_special_branches(self):
        f = self.ns['_domains_for_change']
        self.ns['_version_metadata_only_change'] = lambda *a, **k: True
        self.assertEqual(f('x', path='pyproject.toml', ancestor='a', current='b'), set())
        self.ns['_version_metadata_only_change'] = lambda *a, **k: False
        self.assertEqual(f('x', path='eimemory/version.py', ancestor='a', current='b'), set(self.ns['DOMAINS']))
        self.ns['_integration_version_only_change'] = lambda *a, **k: True
        self.assertEqual(f('x', path='integrations/hermes/eimemory_hook/plugin.yaml', ancestor='a', current='b'), set())
        self.assertEqual(f('x', path='unmapped', ancestor='a', current='b'), set())

    def test_ignored_branches(self):
        f = self.ns['_ignored_change']
        self.ns['_version_metadata_only_change'] = lambda *a, **k: True
        self.ns['_integration_version_only_change'] = lambda *a, **k: True
        for p in ['.gitignore','docs/a','tests/a','.github/a','README.md','CHANGELOG.md','FAQ.md','CONTRIBUTING.md','LICENSE','pyproject.toml','integrations/hermes/eimemory/plugin.yaml']:
            self.assertTrue(f('x',path=p,ancestor='a',current='b'))
        self.assertFalse(f('x',path='unmapped',ancestor='a',current='b'))

    def test_release_inputs_errors(self):
        f = self.ns['release_impact']; error = self.ns['ReleaseImpactError']
        FakePath.directory = False
        with self.assertRaisesRegex(error, 'directory'): f('x', ancestor='a'*40, current='b'*40)
        FakePath.directory = True
        for a,b in [('bad','b'*40),('a'*40,'bad')]:
            with self.assertRaisesRegex(error, 'exact'): f('x', ancestor=a,current=b)
        self.ns['_git_bytes'] = lambda *a: None
        with self.assertRaisesRegex(error, 'unavailable'): f('x', ancestor='a'*40,current='b'*40)
        self.ns['_git_bytes'] = lambda *a: b''
        self.ns['_release_change_summary'] = lambda *a, **k: None
        with self.assertRaisesRegex(error, 'changed paths'): f('x', ancestor='a'*40,current='b'*40)

    def test_release_result_branches(self):
        self.ns['_git_bytes'] = lambda *a: b''
        for domains,unknown,reason in [(set(), [], 'lightweight_release'), ({'memory.recall'}, [], 'classified_production_change'), (set(), ['x'], 'unknown_production_change')]:
            self.ns['_release_change_summary'] = lambda *a, **k: (['x'], {'x':domains}, unknown)
            out=self.ns['release_impact']('x',ancestor='a'*40,current='B'*40)
            self.assertEqual(out['reason'], reason)
            self.assertEqual(out['requires_closure'],bool(domains or unknown))
            self.assertEqual(out['paths'][0]['classification'], 'classified' if domains else 'unknown_production' if unknown else 'ignored')
            json.dumps(out)


if __name__ == '__main__':
    unittest.main()
