"""Isolated local publication regression: stdlib/fakes only, no project imports.

Default target is the adjacent repository-shaped candidate. Set
CATALOG_SOURCE_PATH to an exact source snapshot for old/new comparison.
Only the listed AST nodes execute. These tests do not certify real digest,
normalizer, grader, validator, execute, bootstrap, or concurrency behavior.
"""
import ast
import copy
from dataclasses import dataclass, FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any, Mapping
import unittest


METHOD_RANGES = {
    '_require_mutable': (436, 443),
    '_locked_mutation': (445, 455),
    'register_executor': (458, 521),
    'register_case': (523, 535),
    'publish_into': (1042, (1065, 1068)),
}


class FakeContractError(Exception):
    pass


class FakeResolutionError(Exception):
    pass


@dataclass(frozen=True)
class FakeCase:
    case_id: str
    case_digest: str
    executor_id: str
    executor_revision: str
    executor_contract_digest: str


class EmptyGraders:
    def __init__(self):
        self.queries = 0

    def registrations(self):
        self.queries += 1
        return ()

    def register(self, **kwargs):
        raise AssertionError('nonempty grader behavior is outside this harness')


class NeverCalled:
    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError('fake executor/validator must never run')


def load_catalog(path):
    """Extract only explicitly allowed DTO/methods; every dependency is fake."""
    tree = ast.parse(Path(path).read_text(encoding='utf-8'), filename=str(path))
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    dto = classes['ExecutorRegistration']
    assert (dto.lineno, dto.end_lineno) == (343, 349)
    assert len(dto.decorator_list) == 1
    assert ast.unparse(dto.decorator_list[0]) == 'dataclass(frozen=True, slots=True)'
    assert all(isinstance(node, ast.AnnAssign) for node in dto.body)
    methods = {node.name: node for node in classes['CapabilityEvaluationCatalog'].body
               if isinstance(node, ast.FunctionDef)}
    selected = []
    for name, (start, end) in METHOD_RANGES.items():
        method = methods[name]
        allowed_ends = end if isinstance(end, tuple) else (end,)
        assert method.lineno == start and method.end_lineno in allowed_ends, name
        assert not method.decorator_list, name
        for node in ast.walk(method):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                assert (name == '_locked_mutation' and isinstance(node, ast.ImportFrom)
                        and node.module == 'contextlib' and node.level == 0
                        and [(alias.name, alias.asname) for alias in node.names]
                        == [('contextmanager', None)]), name
        selected.append(copy.deepcopy(method))
    # No original class constructor, decorators, bases or other body is used.
    cls = ast.ClassDef(name='CapabilityEvaluationCatalog', bases=[], keywords=[],
                       body=selected, decorator_list=[])
    module = ast.Module(body=[copy.deepcopy(dto), cls], type_ignores=[])
    ast.fix_missing_locations(module)
    operands = []

    def fake_digest(value):
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'))
        operands.append(json.loads(encoded))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def fake_normalize(value, *, field):
        if not isinstance(value, str) or not value.strip():
            raise FakeContractError(field)
        return value.strip()

    def fake_mapping(value, *, field):
        return dict(value)

    namespace = {
        '__name__': __name__, 'dataclass': dataclass, 'ProbeExecutor': object,
        'RecordedExecutionValidator': object, 'Mapping': Mapping, 'Any': Any,
        'CatalogCase': FakeCase, 'CapabilityContractError': FakeContractError,
        'CatalogResolutionError': FakeResolutionError,
        'normalize_opaque_id': fake_normalize, '_safe_mapping': fake_mapping,
        'contract_digest': fake_digest, 'CATALOG_SCHEMA_VERSION': 'fake-catalog-schema',
    }
    exec(compile(module, str(path), 'exec', dont_inherit=True), namespace)
    catalog = namespace['CapabilityEvaluationCatalog']

    def fake_init(self):
        self._cases = {}
        self._executors = {}
        self.graders = EmptyGraders()
        self._sealed = False
        self._mutation_lock = RLock()

    catalog.__init__ = fake_init
    return catalog, operands


class RegistrationTransferTests(unittest.TestCase):
    def setUp(self):
        target = os.environ.get('CATALOG_SOURCE_PATH')
        if target is None:
            target = Path(__file__).resolve().parents[1] / 'eimemory/evaluation/capability_catalog.py'
        self.catalog, self.operands = load_catalog(target)
        self.callbacks = []

    def tearDown(self):
        self.assertTrue(all(callback.calls == 0 for callback in self.callbacks))

    def callback(self):
        callback = NeverCalled()
        self.callbacks.append(callback)
        return callback

    def register(self, source, *, descriptor=False, validator=False, revision='v1'):
        return source.register_executor(
            executor_id='probe', revision=revision, handler=self.callback(),
            contract_descriptor={'purpose': 'probe'} if descriptor else None,
            recorded_execution_validator_id='validator' if validator else '',
            recorded_execution_validator=self.callback() if validator else None,
        )

    def case(self, source, registration):
        case = FakeCase('case', 'fake-case-digest', registration.executor_id,
                        registration.revision, registration.contract_digest)
        source.register_case(case)
        return case

    def check_transfer(self, *, descriptor=False, validator=False):
        source, destination = self.catalog(), self.catalog()
        registration = self.register(source, descriptor=descriptor, validator=validator)
        case = self.case(source, registration)
        before = copy.deepcopy(self.operands)
        source.publish_into(destination)
        transferred = destination._executors['probe']
        for field in ('executor_id', 'revision', 'contract_digest',
                      'recorded_execution_validator_id'):
            self.assertEqual(getattr(transferred, field), getattr(registration, field), field)
        self.assertIs(transferred.handler, registration.handler)
        self.assertIs(transferred.recorded_execution_validator,
                      registration.recorded_execution_validator)
        self.assertIs(destination._cases['case'], case)
        self.assertEqual(case.executor_contract_digest, transferred.contract_digest)
        # Publication must retain the immutable record, without digest recomputation.
        self.assertIs(transferred, registration)
        self.assertEqual(self.operands, before)
        self.assertEqual('descriptor' in before[0], descriptor)
        self.assertEqual('recorded_execution_validator_id' in before[0], validator)
        with self.assertRaises(FrozenInstanceError):
            transferred.revision = 'changed'

    def test_01_baseline(self):
        self.check_transfer()

    def test_02_validator_only(self):
        self.check_transfer(validator=True)

    def test_03_descriptor_only(self):
        self.check_transfer(descriptor=True)

    def test_04_combined(self):
        self.check_transfer(descriptor=True, validator=True)

    def test_05_sealed_destination(self):
        source, destination = self.catalog(), self.catalog()
        registration = self.register(source, descriptor=True, validator=True)
        self.case(source, registration)
        destination._sealed = True
        before = copy.deepcopy(self.operands)
        with self.assertRaisesRegex(FakeResolutionError, '^capability_catalog_sealed$'):
            source.publish_into(destination)
        self.assertEqual(destination._executors, {})
        self.assertEqual(destination._cases, {})
        self.assertEqual(source.graders.queries, 0)
        self.assertEqual(self.operands, before)

    def test_06_equivalent_destination_wins(self):
        source, destination = self.catalog(), self.catalog()
        registration = self.register(source, descriptor=True, validator=True)
        existing = self.register(destination, descriptor=True, validator=True)
        self.assertIsNot(registration.handler, existing.handler)
        self.assertIsNot(registration.recorded_execution_validator, existing.recorded_execution_validator)
        source_case = self.case(source, registration)
        destination_case = self.case(destination, existing)
        self.assertIsNot(source_case, destination_case)
        before = copy.deepcopy(self.operands)
        source.publish_into(destination)
        self.assertIs(destination._executors['probe'], existing)
        self.assertIs(destination._cases['case'], destination_case)
        self.assertEqual(self.operands, before)

    def test_07_conflicting_revision_or_digest(self):
        # Isolate each operand: revision differs with equal fake stored digest;
        # digest differs with equal revision. No earlier-entry rollback claim.
        for field in ('revision', 'contract_digest'):
            with self.subTest(field=field):
                source, destination = self.catalog(), self.catalog()
                registration = self.register(source)
                values = dict(executor_id=registration.executor_id,
                              revision=registration.revision,
                              contract_digest=registration.contract_digest,
                              handler=self.callback())
                values[field] = 'different'
                existing = type(registration)(**values)
                destination._executors['probe'] = existing
                with self.assertRaisesRegex(FakeResolutionError, '^conflicting executor registration: probe$'):
                    source.publish_into(destination)
                self.assertIs(destination._executors['probe'], existing)

    def test_08_self_publication(self):
        source = self.catalog()
        registration = self.register(source, descriptor=True, validator=True)
        case = self.case(source, registration)
        before = copy.deepcopy(self.operands)
        source.publish_into(source)
        self.assertEqual(len(source._executors), 1)
        self.assertEqual(len(source._cases), 1)
        self.assertIs(source._executors['probe'], registration)
        self.assertIs(source._cases['case'], case)
        self.assertEqual(self.operands, before)

    def test_09_repeated_publication(self):
        source, destination = self.catalog(), self.catalog()
        registration = self.register(source, descriptor=True, validator=True)
        case = self.case(source, registration)
        before = copy.deepcopy(self.operands)
        source.publish_into(destination)
        source.publish_into(destination)
        self.assertEqual(len(destination._executors), 1)
        self.assertEqual(len(destination._cases), 1)
        self.assertIs(destination._executors['probe'], registration)
        self.assertIs(destination._cases['case'], case)
        self.assertEqual(self.operands, before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
