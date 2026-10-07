"""Lazy standard-library envelope tests; no project imports or real dispatch."""
import ast
import copy
from pathlib import Path
import sys
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'eimemory/adapters/codex/mcp_server.py'


def extracted_server_type(source):
    tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
    names = {'handle_message', '_result', '_error'}
    matches = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [item for item in node.body if isinstance(item, ast.FunctionDef) and item.name in names]
            if {item.name for item in methods} == names:
                matches.append(methods)
    if len(matches) != 1:
        raise AssertionError('Expected exactly one approved envelope method group')
    methods = copy.deepcopy(matches[0])
    for method in methods:
        method.returns = None
        for argument in method.args.args:
            argument.annotation = None
    unit = ast.Module(body=[ast.ClassDef(name='Extracted', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    ast.fix_missing_locations(unit)
    namespace = {}
    exec(compile(unit, '<only-three-envelope-methods>', 'exec'), namespace)
    return namespace['Extracted']


class VersionIdTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_type = extracted_server_type(SOURCE)

    def setUp(self):
        self.server = self.server_type()
        self.calls = []
        def schemas():
            self.calls.append(('schemas',))
            return []
        def call_tool(name, arguments, request_id):
            self.calls.append(('call_tool', name, arguments, request_id))
            return {'ok': True}
        def tool_result(result, is_error):
            self.calls.append(('tool_result', result, is_error))
            return {'fixed': 'fake', 'isError': is_error}
        self.server._tool_schemas = schemas
        self.server._call_tool = call_tool
        self.server._tool_result = tool_result

    def invalid(self, request_id):
        return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32600, 'message': 'Invalid Request'}}

    def test_invalid_versions_with_valid_id(self):
        for version in ('1.0', '2', '', 2, 2.0, True, False, None, [], {}):
            with self.subTest(version=version):
                self.assertEqual(self.server.handle_message({'jsonrpc': version, 'id': 7, 'method': 'ping'}), self.invalid(7))
        self.assertEqual(self.calls, [])

    def test_missing_version_with_id(self):
        self.assertEqual(self.server.handle_message({'id': 7, 'method': 'ping'}), self.invalid(7))
        self.assertEqual(self.calls, [])

    def test_invalid_version_without_id_is_not_notification(self):
        for version in ('1.0', 2.0, True, None, [], {}):
            with self.subTest(version=version):
                self.assertEqual(self.server.handle_message({'jsonrpc': version, 'method': 'ping'}), self.invalid(None))
        self.assertEqual(self.calls, [])

    def test_missing_version_without_id(self):
        self.assertEqual(self.server.handle_message({'method': 'ping'}), self.invalid(None))
        self.assertEqual(self.calls, [])

    def test_invalid_ids_never_echo_invalid_shape(self):
        for request_id in (True, False, [], [1], {}, {'x': 1}):
            with self.subTest(request_id=request_id):
                self.assertEqual(self.server.handle_message({'jsonrpc': '2.0', 'id': request_id, 'method': 'ping'}), self.invalid(None))
        self.assertEqual(self.calls, [])

    def test_combined_invalid_id_and_version(self):
        self.assertEqual(self.server.handle_message({'jsonrpc': '1.0', 'id': [], 'method': 'ping'}), self.invalid(None))
        self.assertEqual(self.calls, [])

    def test_invalid_envelopes_do_not_reach_fixed_dispatch(self):
        for method in ('tools/list', 'tools/call'):
            for message in ({'method': method, 'id': 7}, {'jsonrpc': '2.0', 'id': True, 'method': method}, {'jsonrpc': '1.0', 'method': method}):
                with self.subTest(message=message):
                    self.assertEqual(self.server.handle_message(message), self.invalid(7 if message.get('id') == 7 else None))
        self.assertEqual(self.calls, [])

    def test_valid_ids_including_null_and_fractional_number(self):
        for request_id in ('abc', '', 0, -1, 7, 1.0, 1.5, None):
            with self.subTest(request_id=request_id):
                result = self.server.handle_message({'jsonrpc': '2.0', 'id': request_id, 'method': 'ping'})
                self.assertEqual(result, {'jsonrpc': '2.0', 'id': request_id, 'result': {}})
                self.assertIs(type(result['id']), type(request_id))
        self.assertEqual(self.calls, [])

    def test_valid_notification_stays_silent(self):
        self.assertIsNone(self.server.handle_message({'jsonrpc': '2.0', 'method': 'ping'}))
        self.assertEqual(self.calls, [])

    def test_valid_list_uses_only_fixed_schema(self):
        self.assertEqual(self.server.handle_message({'jsonrpc': '2.0', 'id': 'abc', 'method': 'tools/list'}), {'jsonrpc': '2.0', 'id': 'abc', 'result': {'tools': []}})
        self.assertEqual(self.calls, [('schemas',)])

    def test_valid_tool_uses_only_fixed_receivers(self):
        message = {'jsonrpc': '2.0', 'id': 1.5, 'method': 'tools/call', 'params': {'name': 'fixed', 'arguments': {}}}
        self.assertEqual(self.server.handle_message(message), {'jsonrpc': '2.0', 'id': 1.5, 'result': {'fixed': 'fake', 'isError': False}})
        self.assertEqual(self.calls, [('call_tool', 'fixed', {}, 1.5), ('tool_result', {'ok': True}, False)])

    def test_valid_tool_notification_preserves_fixed_dispatch(self):
        message = {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'fixed', 'arguments': {}}}
        self.assertIsNone(self.server.handle_message(message))
        self.assertEqual(self.calls, [('call_tool', 'fixed', {}, None), ('tool_result', {'ok': True}, False)])


if __name__ == '__main__':
    if '--source' in sys.argv:
        index = sys.argv.index('--source')
        SOURCE = Path(sys.argv[index + 1]).resolve()
        del sys.argv[index:index + 2]
    unittest.main()
