"""Four finite AST-only reader contract tests. No repository import/runtime."""
from __future__ import annotations
import ast
import copy
import json
import unittest
from pathlib import Path
from collections.abc import Mapping
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).parent
LOADED = []

def load(rel, names, cls=None, target=None):
    source = (ROOT / rel).read_text()
    owner = ast.parse(source)
    if cls:
        owner = next(n for n in owner.body if isinstance(n, ast.ClassDef) and n.name == cls)
    nodes = []
    for n in owner.body:
        matched = isinstance(n, ast.FunctionDef) and n.name in names
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            matched = any(isinstance(t, ast.Name) and t.id in names for t in targets)
        if matched:
            nodes.append(copy.deepcopy(n))
            LOADED.append({'file': rel, 'symbol': f'{cls + "." if cls else ""}{n.name if isinstance(n, ast.FunctionDef) else targets[0].id}', 'lines': [n.lineno, n.end_lineno]})
    assert len(nodes) == len(names), (rel, names)
    if cls:
        nodes = [ast.ClassDef(name=target or cls, bases=[], keywords=[], body=nodes, decorator_list=[])]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(ROOT / rel), 'exec'), globals())

load('eimemory/metadata.py', {'BUSINESS_META_KEY', 'RUNTIME_META_KEY', 'RUNTIME_META_ALIASES', 'TOP_LEVEL_BUSINESS_META_KEYS', 'split_metadata', 'normalize_metadata', 'business_metadata', 'runtime_metadata', '_dict_value', '_has_runtime_value'})
load('eimemory/contracts/recall_boundary.py', {'ACCEPTANCE_EVENT_MEMORY_TYPE', '_EVENT_TRACE_MEMORY_TYPE', '_EVENT_MEMORY_PROJECTION', 'is_operational_probe_task_type', 'effective_recall_memory_type', 'record_recall_memory_type'})
load('eimemory/api/memory.py', {'_record_filter_labels', '_record_recall_filter_block_reason'}, cls='MemoryAPI')
load('eimemory/storage/sqlite_store.py', {'_record_filter_labels', '_record_recall_filter_block_reason', '_preferred_modality_boost', '_source_weight', '_as_tuple'}, cls='SqliteRecordStore')

def rec(meta=None, content=None):
    return SimpleNamespace(meta=meta or {}, content=content or {}, kind='memory', source='connector', provenance={})

class ReaderContract(unittest.TestCase):
    def setUp(self):
        self.api = MemoryAPI()
        self.sql = SqliteRecordStore()
        # Isolate reader/filter semantics, without recall classification runtime.
        self.api._record_recall_lane = lambda record: 'primary'
        self.sql._record_recall_lane = lambda record: 'primary'

    def assert_labels(self, record, organ, modality):
        self.assertEqual(self.api._record_filter_labels(record)['organs'], organ)
        labels = self.sql._record_filter_labels(record)
        self.assertEqual(labels['organs'], organ)
        self.assertEqual(labels['modalities'], modality)

    def test_flat_normalized_and_canonical_metadata_match(self):
        flat = {'memory_type': 'fact', 'organ': 'vision', 'modality': 'image'}
        for meta in [flat, normalize_metadata(flat), {'business_meta': {'memory_type': 'fact'}, 'runtime_meta': {'organ': 'vision', 'modality': 'image'}}]:
            with self.subTest(meta=meta):
                r = rec(meta)
                self.assert_labels(r, {'vision'}, {'image'})
                for reader in [self.api, self.sql]:
                    self.assertEqual(reader._record_recall_filter_block_reason(r, {'organs': ['cognition']}), 'organ:not_allowed')
                    self.assertEqual(reader._record_recall_filter_block_reason(r, {'organs': ['vision']}), '')
                self.assertEqual(self.sql._preferred_modality_boost(r, {'preferred_modalities': ['image']}), 0.18)
                self.assertEqual(self.sql._preferred_modality_boost(r, {'preferred_modalities': ['audio']}), 0.0)

    def test_formal_priority_and_truly_missing_fallback(self):
        content = {'organ': 'content-organ', 'modality': 'content-modality'}
        meta = {'runtime_meta': {'organ': 'nested-organ', 'modality': 'nested-modality'}, 'organ': 'legacy-organ', 'modality': 'legacy-modality'}
        # Existing split_metadata semantics: present legacy aliases override nested.
        self.assert_labels(rec(meta, content), {'legacy-organ'}, {'legacy-modality'})
        self.assert_labels(rec(normalize_metadata(meta), content), {'legacy-organ'}, {'legacy-modality'})
        self.assert_labels(rec({'runtime_meta': {'organ': 'nested-organ', 'modality': 'nested-modality'}}, content), {'nested-organ'}, {'nested-modality'})
        self.assert_labels(rec({}, content), {'content-organ'}, {'content-modality'})
        # Top-level null/whitespace aliases are omitted by the formal reader.
        self.assert_labels(rec({'organ': None, 'modality': '  '}, content), {'content-organ'}, {'content-modality'})
        # An omitted legacy alias does not erase a present nested value.
        self.assert_labels(rec({'organ': None, 'modality': '  ', 'runtime_meta': {'organ': 'nested-organ', 'modality': 'nested-modality'}}, content), {'nested-organ'}, {'nested-modality'})

    def test_present_null_empty_and_untyped_values_do_not_fallback(self):
        content = {'organ': 'content-organ', 'modality': 'content-modality'}
        # Current metadata contract does not type-check runtime values. Retain
        # existing label truthiness/string conversion; do not invent validation.
        for value in [None, '', '  ', False, 0, [], {}, ['vision'], {'name': 'vision'}, 3]:
            with self.subTest(value=value):
                meta = {'runtime_meta': {'organ': value, 'modality': value}}
                self.assertIn('organ', runtime_metadata(meta))
                expected = {str(value).strip()} if value and str(value).strip() else set()
                self.assert_labels(rec(meta, content), expected, expected)
        # A malformed runtime container has no keys in the current formal reader.
        self.assert_labels(rec({'runtime_meta': ['not-a-map']}, content), {'content-organ'}, {'content-modality'})

    def test_business_labels_filters_and_weights_are_unchanged(self):
        r = rec(normalize_metadata({'memory_type': 'fact', 'source_channel': 'business-channel', 'organ': 'vision', 'modality': 'image'}), {'memory_type': 'rule', 'source_channel': 'content-channel'})
        for reader in [self.api, self.sql]:
            labels = reader._record_filter_labels(r)
            self.assertEqual(labels['sources'], {'connector', 'business-channel'})
            self.assertEqual(labels['memory_types'], {'fact'})
            for filters, reason in [
                ({'blocked_sources': ['business-channel']}, 'source:blocked'),
                ({'allowed_sources': ['unrelated']}, 'source:not_allowed'),
                ({'allowed_memory_types': ['rule']}, 'memory_type:not_allowed'),
                ({'blocked_recall_lanes': ['primary']}, 'primary'),
                ({'allowed_recall_lanes': ['raw']}, 'recall_lane:not_allowed'),
            ]:
                self.assertEqual(reader._record_recall_filter_block_reason(r, filters), reason)
            self.assertEqual(reader._record_recall_filter_block_reason(r, {'allowed_sources': ['business-channel'], 'allowed_memory_types': ['fact'], 'organs': ['vision']}), '')
            # Genuinely absent organ labels keep the existing filter behavior.
            self.assertEqual(reader._record_recall_filter_block_reason(rec(), {'organs': ['vision']}), '')
        self.assertEqual(self.sql._source_weight(r, {'source_weights': {'business-channel': 1.4}}), 1.4)
        self.assertEqual(self.sql._preferred_modality_boost(r, {}), 0.0)

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ReaderContract)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sources = sorted({x['file'] for x in LOADED})
    manifest = {'batch': 'A', 'source_root': str(ROOT), 'case_count': result.testsRun, 'passed': result.wasSuccessful(), 'failures': len(result.failures), 'errors': len(result.errors), 'loaded_ast_symbols': LOADED, 'repository_modules_imported': [], 'real_runtime_executed': False, 'stubs': ['record SimpleNamespace', 'constant primary lane for label/filter isolation'], 'source_sha256': {rel: sha256((ROOT / rel).read_bytes()).hexdigest() for rel in sources}}
    (OUT / 'test-results.json').write_text(json.dumps(manifest, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
