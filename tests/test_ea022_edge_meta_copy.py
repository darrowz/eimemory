"""Stdlib-only AST regression; no project imports or dependency integration."""
import ast
import dataclasses
from pathlib import Path
import unittest


def load_memory_edge():
    path = Path(__file__).resolve().parents[1] / 'eimemory/models/memory_edges.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MemoryEdge')
    node.body = [n for n in node.body if not isinstance(n, ast.FunctionDef) or n.name == 'to_dict']
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    namespace = {'dataclass': dataclasses.dataclass, 'asdict': dataclasses.asdict}
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    return namespace['MemoryEdge']


@dataclasses.dataclass
class SyntheticScope:
    """Explicit substitute; real ScopeRef integration is not tested."""
    label: str = 'synthetic'


class EdgeMetaCopyTest(unittest.TestCase):
    def make_edge(self, meta):
        return load_memory_edge()('e', 'a', 'b', 'semantic', 0.5, '', SyntheticScope(), meta=meta)

    def test_nested_serialized_meta_is_detached(self):
        edge = self.make_edge({'nested': ({'values': [1]},)})
        output = edge.to_dict()
        output['meta']['nested'][0]['values'].append(2)
        self.assertEqual(edge.meta['nested'][0]['values'], [1])
        self.assertIsInstance(output['meta']['nested'], tuple)

    def test_none_and_ordinary_shapes_match_asdict(self):
        for meta in (None, {}, {'nested': ({'values': [1]},)}, {'nested': {'value': None}}):
            with self.subTest(meta=meta):
                edge = self.make_edge(meta)
                expected = dataclasses.asdict(edge)
                expected['meta'] = expected['meta'] or {}
                self.assertEqual(edge.to_dict(), expected)


if __name__ == '__main__':
    unittest.main()
