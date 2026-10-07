"""Portable AST-isolated registry contracts; no project imports or storage I/O.

Run with standard unittest discovery from the repository.
The only expectation is rejection of generated-ID collisions.
Only the supplied source/test files are read. Fake commit is not a storage proof.
"""
from __future__ import annotations

import ast
import copy
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
import unittest

SOURCE_PATH = Path(__file__).resolve().parents[1] / 'eimemory' / 'intake' / 'registry.py'
SOURCE = SOURCE_PATH.read_text(encoding='utf-8')


def load_registry():
    tree = ast.parse(SOURCE, filename=str(SOURCE_PATH))
    constants = {'VALID_SOURCE_KINDS', 'VALID_SOURCE_FREQUENCIES',
                 'DEFAULT_SOURCE_FREQUENCY', 'DEFAULT_SOURCE_MAX_ITEMS'}
    helpers = {'_json_safe', '_normalize_tags', '_normalize_ordered_text_list',
               'normalize_source_strategy_metadata', '_default_source_id'}
    methods = {'add_source', '_locked_update', '_decode_sources', '_upsert'}
    selected = []
    for node in tree.body:
        names = ({node.target.id} if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                 else {t.id for t in node.targets if isinstance(t, ast.Name)} if isinstance(node, ast.Assign) else set())
        if names & constants or isinstance(node, ast.FunctionDef) and node.name in helpers:
            selected.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == 'SourceEntry':
            selected.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == 'SourceRegistry':
            node.body = [m for m in node.body if isinstance(m, ast.FunctionDef) and m.name in methods]
            assert {m.name for m in node.body} == methods
            selected.append(node)
    namespace = dict(Any=Any, Callable=Callable, Path=Path, date=date, datetime=datetime,
                     asdict=asdict, dataclass=dataclass, field=field, sha256=sha256,
                     __name__=__name__)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE_PATH), 'exec'), namespace)
    return namespace


class Harness:
    def __init__(self, records=()):
        self.ns = load_registry()
        self.Entry = self.ns['SourceEntry']
        self.Class = self.ns['SourceRegistry']
        self.raw = copy.deepcopy(list(records))
        self.commits = 0
        self.upserts = 0
        self.callback_active = False
        self.observed_sources = None
        self.before_callback = None
        self.reg = object.__new__(self.Class)
        self.reg.path = Path('memory-only-registry.json')
        self.reg._sources = self.Class._decode_sources(copy.deepcopy(self.raw))
        original_upsert = self.Class._upsert

        def spy(sources, entry):
            assert self.callback_active
            self.upserts += 1
            return original_upsert(sources, entry)
        self.reg._upsert = spy
        original_decode = self.Class._decode_sources

        def decode(raw):
            sources = original_decode(raw)
            if self.callback_active:
                self.observed_sources = sources
                self.before_callback = [e.to_dict() for e in sources]
            return sources
        self.reg._decode_sources = decode

        def fake_locked_json_update(path, callback, *, default, expected_type):
            assert path == self.reg.path and default == [] and expected_type is list
            self.callback_active = True
            try:
                result = callback(copy.deepcopy(self.raw))
            finally:
                self.callback_active = False
            self.raw = copy.deepcopy(result)
            self.commits += 1
            return copy.deepcopy(self.raw)
        self.ns['locked_json_update'] = fake_locked_json_update

    def add(self, **payload):
        return self.reg.add_source(payload)


class CollisionTests(unittest.TestCase):
    def assert_collision(self, h, payload):
        previous_raw = copy.deepcopy(h.raw)
        previous_cache = [e.to_dict() for e in h.reg._sources]
        calls, commits = h.upserts, h.commits
        with self.assertRaisesRegex(ValueError, '^invalid source registry at memory-only-registry.json$') as caught:
            h.reg.add_source(payload)
        cause = caught.exception.__cause__
        self.assertIsInstance(cause, ValueError)
        self.assertTrue(str(cause).startswith('generated source_id collision: src_'))
        self.assertEqual(h.upserts, calls)
        self.assertEqual(h.commits, commits)
        self.assertEqual(h.raw, previous_raw)
        self.assertEqual([e.to_dict() for e in h.reg._sources], previous_cache)
        self.assertEqual([e.to_dict() for e in h.observed_sources], h.before_callback)

    def test_delimiter_collision_both_directions(self):
        pairs = [('alpha|beta', 'gamma', 'alpha', 'beta|gamma'),
                 ('alpha|', '', 'alpha', '|')]
        for u1, t1, u2, t2 in pairs:
            for first, second in [((u1, t1), (u2, t2)), ((u2, t2), (u1, t1))]:
                with self.subTest(first=first, second=second):
                    h = Harness()
                    h.add(source_kind='manual', uri=first[0], title=first[1])
                    self.assert_collision(h, dict(source_kind='manual', uri=second[0], title=second[1]))

    def test_all_falsy_and_blank_ids(self):
        for identifier in (None, False, 0, 0.0, '', ' ', '\t\n', [], {}):
            with self.subTest(identifier=identifier):
                h = Harness()
                h.add(source_kind='manual', uri='a|b', title='c')
                self.assert_collision(h, dict(source_id=identifier, source_kind='manual', uri='a', title='b|c'))

    def test_same_tuple_normalized_repeat_and_metadata(self):
        h = Harness()
        first = h.add(source_kind=' MANUAL ', uri=' a|b ', title=' c ', tags=['old'],
                      last_scanned_at='old-time', metadata={'scan_history': [{'ok': 1}], 'last_scan': {'n': 2}})
        second = h.add(source_kind='manual', uri='a|b', title='c', tags=['new'], metadata={'priority': 'high'})
        self.assertEqual(first.source_id, second.source_id)
        self.assertEqual(len(h.raw), 1)
        self.assertEqual(h.upserts, 2)
        self.assertEqual(h.raw[0]['metadata']['scan_history'], [{'ok': 1}])
        self.assertEqual(h.raw[0]['metadata']['last_scan'], {'n': 2})
        self.assertEqual(h.raw[0]['metadata']['priority'], 'high')
        self.assertEqual(h.raw[0]['last_scanned_at'], 'old-time')
        self.assertEqual(h.raw[0]['tags'], ['new'])

    def test_explicit_id_allows_changed_tuple(self):
        for kind in ('manual', 'url'):
            h = Harness()
            first = h.add(source_kind='manual', uri='a|b', title='c')
            h.add(source_id=' '+first.source_id+' ', source_kind=kind, uri='a', title='b|c')
            self.assertEqual(len(h.raw), 1)
            self.assertEqual(h.raw[0]['source_kind'], kind)
            self.assertEqual(h.raw[0]['uri'], 'a')
            self.assertEqual(h.raw[0]['title'], 'b|c')

    def test_explicit_nonempty_falsy_looking_strings(self):
        for identifier in ('0', 'False', 'None', 17, True):
            h = Harness()
            h.add(source_id=identifier, source_kind='manual', uri='before')
            h.add(source_id=identifier, source_kind='url', uri='after')
            self.assertEqual(len(h.raw), 1)
            self.assertEqual(h.raw[0]['source_id'], str(identifier))
            self.assertEqual(h.raw[0]['uri'], 'after')

    def test_explicit_different_id_inserts_colliding_tuple(self):
        h = Harness()
        h.add(source_kind='manual', uri='a|b', title='c')
        h.add(source_id='distinct', source_kind='manual', uri='a', title='b|c')
        self.assertEqual(len(h.raw), 2)

    def test_generated_different_id_inserts(self):
        h = Harness()
        one = h.add(source_kind='manual', uri='a', title='x')
        two = h.add(source_kind='manual', uri='a', title='y')
        self.assertNotEqual(one.source_id, two.source_id)
        self.assertEqual(len(h.raw), 2)

    def test_default_id_exact_algorithm_all_kinds_and_empty(self):
        h = Harness()
        for kind in ('paper', 'news', 'rss', 'url', 'manual'):
            for uri, title in (('', ''), ('a|', ''), ('a', '|'), ('ü', '汉字'), (' x ', ' y ')):
                entry = h.Entry(source_id='', source_kind=' '+kind.upper()+' ', uri=uri, title=title)
                expected = 'src_' + sha256('|'.join((kind, uri.strip(), title.strip())).encode()).hexdigest()[:12]
                self.assertEqual(entry.source_id, expected)

    def test_generated_collision_existing_explicit_id_and_kind(self):
        h = Harness()
        identifier = h.ns['_default_source_id']('manual', 'uri', 'title')
        h.add(source_id=identifier, source_kind='url', uri='uri', title='title')
        self.assert_collision(h, dict(source_kind='manual', uri='uri', title='title'))

    def test_guard_uses_locked_snapshot_not_stale_cache(self):
        h = Harness()
        h.add(source_kind='manual', uri='a|b', title='c')
        h.reg._sources = []
        self.assert_collision(h, dict(source_kind='manual', uri='a', title='b|c'))

    def test_later_duplicate_conflict_rejected_before_first_upsert(self):
        h = Harness()
        first = h.add(source_kind='manual', uri='a', title='b|c')
        other = h.Entry(source_id=first.source_id, source_kind='manual', uri='a|b', title='c').to_dict()
        h.raw.append(other)
        self.assert_collision(h, dict(source_kind='manual', uri='a', title='b|c', tags=['changed']))

    def test_explicit_metadata_replacement_and_empty_fallback_unchanged(self):
        h = Harness()
        h.add(source_id='fixed', source_kind='manual', title='prior', uri='old', tags=['old'],
              last_scanned_at='old', metadata={'scan_history': [1], 'last_scan': {'a': 1}})
        h.add(source_id='fixed', source_kind='manual', title='', uri='', tags=[],
              last_scanned_at='new', metadata={'scan_history': [], 'last_scan': {}})
        self.assertEqual(h.raw[0]['title'], 'prior')
        self.assertEqual(h.raw[0]['uri'], 'old')
        self.assertEqual(h.raw[0]['tags'], ['old'])
        self.assertEqual(h.raw[0]['last_scanned_at'], 'new')
        self.assertEqual(h.raw[0]['metadata']['scan_history'], [])
        self.assertEqual(h.raw[0]['metadata']['last_scan'], {})

    def test_source_id_payload_read_and_object_conversion_once(self):
        class Identifier:
            calls = 0
            def __str__(self):
                self.calls += 1
                return '  '
        class Payload(dict):
            reads = 0
            def get(self, key, default=None):
                if key == 'source_id':
                    self.reads += 1
                return super().get(key, default)
        h = Harness()
        identifier = Identifier()
        payload = Payload(source_id=identifier, source_kind='manual')
        h.reg.add_source(payload)
        self.assertEqual(payload.reads, 1)
        self.assertEqual(identifier.calls, 1)

    def test_invalid_kind_and_metadata_fail_before_callback(self):
        for payload in (dict(source_kind='wrong'), dict(source_kind='manual', metadata={'max_items': 0})):
            h = Harness()
            with self.assertRaises(ValueError):
                h.reg.add_source(payload)
            self.assertEqual(h.commits, 0)
            self.assertEqual(h.upserts, 0)
            self.assertIsNone(h.observed_sources)

    def test_direct_source_entry_id_semantics_unchanged(self):
        h = Harness()
        for identifier, expected in ((None, 'None'), (False, 'False'), (0, '0')):
            self.assertEqual(h.Entry(source_id=identifier, source_kind='manual').source_id, expected)


if __name__ == '__main__':
    unittest.main()
