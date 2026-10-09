"""Offline regression of actual selected local control flow; no app imports."""
import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'eimemory/adapters/runtime/capability.py'
source = SOURCE.read_text(encoding='utf-8')

# Read/compile only the already-reviewed file. AST is test isolation, not coverage.
tree = ast.parse(source, filename=str(SOURCE))
names = {
    'ADAPTER_CAPABILITY_OUTCOME_SCHEMA_VERSION', 'MAX_OUTCOME_DIAGNOSTIC_BYTES',
    'MAX_ADVERTISEMENTS_PER_CALL', 'IMPLEMENTATION_FINGERPRINT_REVISIONS',
    '_OUTCOME_KEYS', '_SECRET_TEXT', '_SAFE_HOST_EVENT_TYPE',
    'AdapterCapabilityError', '_bounded_reason', '_safe_event_type',
    'UnsupportedCapabilityOutcome', 'NormalizedCapabilityOutcome',
}
selected = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
for node in tree.body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
        selected.append(node)
    elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
        selected.append(node)
    elif isinstance(node, ast.ClassDef) and node.name == 'AdapterCapabilityService':
        node.body = [child for child in node.body if isinstance(child, ast.FunctionDef) and child.name in {'normalize_capability_outcome', '_unsupported'}]
        selected.append(node)
module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))

# Only validation/storage dependencies are synthetic. The rejection selection,
# dispatch through both DTO helpers and reason bounding use actual source bodies.
namespace = {
    '__name__': '__main__', 'Mapping': Mapping, 'Sequence': Sequence,
    'dataclass': dataclass, 're': re, 'Any': object,
    'CapabilityContractError': type('CapabilityContractError', (ValueError,), {}),
    'normalize_opaque_id': lambda value, **kw: str(value),
    'require_timestamp': lambda value, **kw: str(value),
    'ensure_allowed': lambda value, **kw: value,
    '_safe_evidence_refs': lambda value, **kw: tuple(value),
    'sanitize_diagnostic_metadata': lambda value, **kw: dict(value),
    '_safe_summary': lambda value: str(value),
    'RUN_VERDICTS': {'pass', 'fail'},
    'contract_digest': lambda payload: sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
}
exec(compile(module, str(SOURCE), 'exec'), namespace)

class ReasonMappingTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.descriptor = {
            'capability_revision_id': 'code.implementation:v2',
            'environment_fingerprint': {'implementation_digest': 'actual'},
            'host_event_types': ['task.complete'],
        }
        self.binding_descriptor = {'implementation_digest': 'expected'}
        def list_advertisements(**kwargs):
            self.calls.append(('list', kwargs))
            return [{'descriptor': self.descriptor}]
        def binding_context(binding_id, **kwargs):
            self.calls.append(('binding', binding_id, kwargs))
            return {'descriptor': self.binding_descriptor}
        self.service = namespace['AdapterCapabilityService'].__new__(namespace['AdapterCapabilityService'])
        self.service.adapter_id = 'synthetic-adapter'
        self.service.provider_kind = 'synthetic-provider'
        self.service.runtime = SimpleNamespace(capabilities=SimpleNamespace(
            list_advertisements=list_advertisements, binding_context=binding_context))
        self.event = {'event_type': 'task.complete', 'capability_outcome': {
            'binding_id': 'synthetic-binding', 'capability_revision_id': 'code.implementation:v2',
            'event_id': 'synthetic-event', 'occurred_at': '2026-01-01T00:00:00Z',
            'verdict': 'pass', 'evidence_refs': ['synthetic-ref'],
        }}
        self.scope = {'synthetic': 'only'}

    def normalize(self):
        return self.service.normalize_capability_outcome(self.event, runtime_scope=self.scope)

    def test_mismatched_digest_preserves_specific_reason_through_real_chain(self):
        result = self.normalize()
        self.assertEqual([call[0] for call in self.calls], ['list', 'binding'])
        self.assertEqual(self.calls[0][1]['at_time'], '2026-01-01T00:00:00Z')
        self.assertEqual(self.calls[1][1], 'synthetic-binding')
        self.assertEqual(result['status'], 'unsupported')
        self.assertIs(result['ok'], False)
        self.assertEqual(result['event_type'], 'task.complete')
        self.assertEqual(result['reason'], 'implementation_digest_mismatch')

    def test_missing_expected_digest_uses_same_specific_reason(self):
        self.binding_descriptor.clear()
        self.assertEqual(self.normalize()['reason'], 'implementation_digest_mismatch')

    def test_matching_digest_still_normalizes(self):
        self.binding_descriptor['implementation_digest'] = 'actual'
        result = self.normalize()
        self.assertIs(result['ok'], True)
        self.assertEqual(result['status'], 'normalized')
        self.assertEqual(result['binding_id'], 'synthetic-binding')
        self.assertEqual(result['evidence_refs'], ['synthetic-ref'])

    def test_existing_rejection_and_unknown_reason_fallback_unchanged(self):
        self.binding_descriptor['implementation_digest'] = 'actual'
        self.descriptor['host_event_types'] = ['other.event']
        self.assertEqual(self.normalize()['reason'], 'unsupported_host_event')
        self.assertEqual(self.service._unsupported('task.complete', 'not-a-known-reason')['reason'], 'invalid_host_event')

if __name__ == '__main__':
    unittest.main(verbosity=2)
