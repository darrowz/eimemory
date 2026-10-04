"""Finite scope-wiring checks with a deliberately inert source-extraction harness.

The harness extracts only the already-read _materialize_catalog_bindings body,
removes its one adapter import, and supplies handwritten recording objects.
It compiles/executes that extracted function, never imports project modules,
and never runs a real catalog, adapter, provider, Runtime, database, or binding
mutation. These assertions establish construction behavior, not product safety,
activation, acceptance, transactionality, or migration behavior.
"""
import argparse
import ast
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import unittest

NAME = '_materialize_catalog_bindings'
SOURCE = Path(__file__).resolve().parents[1] / 'eimemory/governance/capability/capability_incubation.py'
BASELINE_SOURCE = None
STAMP = '2026-10-03T00:00:00Z'


def parsed_function(path):
    tree = ast.parse(Path(path).read_text(), filename=str(path))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == NAME)


def extract(path, namespace):
    node = deepcopy(parsed_function(path))
    imports = [item for item in ast.walk(node) if isinstance(item, (ast.Import, ast.ImportFrom))]
    assert len(imports) == 1
    imp = imports[0]
    assert isinstance(imp, ast.ImportFrom)
    assert imp.module == 'eimemory.adapters.runtime.capability'
    assert [(alias.name, alias.asname) for alias in imp.names] == [('AdapterCapabilityService', None)]
    assert imp in node.body
    node.body.remove(imp)
    node.returns = None
    for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
        arg.annotation = None
    namespace['__builtins__'] = {name: value for name, value in (
        ('dict', dict), ('str', str), ('list', list), ('tuple', tuple),
        ('set', set), ('len', len), ('isinstance', isinstance),
    )}
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    # Source-derived orchestration in a sealed inert namespace, not module import.
    exec(compile(module, '<inert-materialize-catalog-bindings>', 'exec'), namespace)
    return namespace[NAME]


@dataclass
class Scope:
    tenant_id: str = 'tenant-a'
    agent_id: str = 'agent-a'
    workspace_id: str = 'workspace-a'
    user_id: str = 'user-a'


def exercise(path, capability_scope, probe_passed=True):
    events = []
    plan = {'checked_at': STAMP, 'work_items': [{
        'capability_id': 'sample.capability', 'revision_ids': ['revision-a'],
        'reasons': ['active_provider_binding_missing'],
    }]}
    context = {'revisions': [{
        'entity_id': 'revision-a', 'descriptor': {'contract_digest': 'a' * 64},
    }]}
    selector = {'binding_ids': ['binding.sample.a'], 'operations_all': ['inspect']}
    before = deepcopy((plan, context, selector))
    scope = Scope()
    scope_before = deepcopy(scope)

    class Binding:
        def __init__(self, **kwargs):
            self.fields = deepcopy(kwargs)
            self.fields.setdefault('scope', 'global')
            self.__dict__.update(kwargs)
            self.scope = self.fields['scope']

    class Case:
        case_id = 'case-a'
        executor_contract_digest = 'b' * 64
        binding_selector = selector

        def to_artifact(self):
            return {'case_id': self.case_id}

    class Catalog:
        def list_cases(self, **kwargs):
            events.append(('list_cases', deepcopy(kwargs)))
            return [Case()]

        def execute(self, artifact, **kwargs):
            assert kwargs['runtime'] is runtime
            events.append(('inert_probe', deepcopy(artifact), kwargs['evidence_ref']))
            return {'passed': probe_passed}

    class Capabilities:
        def incubation_context(self, capability_id, **kwargs):
            events.append(('context', capability_id, deepcopy(kwargs)))
            return context

        def bind(self, binding, **kwargs):
            events.append(('inert_binding', deepcopy(binding.fields), deepcopy(kwargs)))

    class Adapter:
        def __init__(self, value, **kwargs):
            assert value is runtime
            events.append(('inert_adapter_constructor', deepcopy(kwargs)))

        def advertise_capabilities(self, payload, **kwargs):
            events.append(('inert_advertisement', deepcopy(payload), deepcopy(kwargs)))

    runtime = SimpleNamespace(capabilities=Capabilities())
    namespace = {
        'Mapping': Mapping, 'sha256': sha256, 'asdict': asdict,
        'CapabilityBinding': Binding, 'AdapterCapabilityService': Adapter,
        'CAPABILITY_INCUBATION_SCHEMA': 'capability.incubation.v1',
        'now_iso': lambda: STAMP,
        '_plus_days': lambda stamp, days: '2026-10-05T00:00:00Z',
    }
    extract(path, namespace)(runtime, plan=plan, catalog=Catalog(), scope=scope,
                             capability_scope=capability_scope)
    assert (plan, context, selector) == before
    assert scope == scope_before
    return events


def one(events, kind):
    matches = [event for event in events if event[0] == kind]
    assert len(matches) == 1
    return matches[0]


class IncubationScopeInert(unittest.TestCase):
    def test_global_binding_and_advertisement_match(self):
        events = exercise(SOURCE, 'global')
        self.assertEqual(one(events, 'inert_binding')[1]['scope'], 'global')
        self.assertEqual(one(events, 'inert_advertisement')[1]['capability_scope'], 'global')

    def test_non_global_binding_and_advertisement_match(self):
        events = exercise(SOURCE, 'team-a')
        self.assertEqual(one(events, 'inert_binding')[1]['scope'], 'team-a')
        self.assertEqual(one(events, 'inert_advertisement')[1]['capability_scope'], 'team-a')

    def test_exact_runtime_scope_preserved_across_recorded_calls(self):
        events = exercise(SOURCE, 'team-a')
        self.assertEqual(one(events, 'context')[2]['runtime_scope'], Scope())
        self.assertEqual(one(events, 'inert_binding')[2]['runtime_scope'], Scope())
        self.assertEqual(one(events, 'inert_advertisement')[2]['runtime_scope'], asdict(Scope()))

    def test_inputs_remain_unchanged(self):
        # exercise asserts deep equality of plan, context, selector, and scope.
        for logical_scope in ('global', 'team-a'):
            with self.subTest(scope=logical_scope):
                exercise(SOURCE, logical_scope)

    def test_failed_inert_probe_does_not_reach_binding_or_advertisement(self):
        events = exercise(SOURCE, 'team-a', probe_passed=False)
        self.assertFalse(any(event[0] in {'inert_binding', 'inert_advertisement'} for event in events))

    def test_constructor_explicitly_uses_requested_scope(self):
        calls = [node for node in ast.walk(parsed_function(SOURCE)) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'CapabilityBinding']
        self.assertEqual(len(calls), 1)
        values = [kw.value for kw in calls[0].keywords if kw.arg == 'scope']
        self.assertEqual([ast.unparse(value) for value in values], ['capability_scope'])




if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=SOURCE)
    parser.add_argument('--baseline-source', type=Path)
    args = parser.parse_args()
    SOURCE = args.source
    BASELINE_SOURCE = args.baseline_source
    unittest.main(argv=['test_capability_incubation_scope_inert.py'], verbosity=2)
