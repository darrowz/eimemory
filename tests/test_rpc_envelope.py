"""Standard-library-only envelope regression; never imports the project module."""
from pathlib import Path
import ast
import copy
import sys
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'eimemory/adapters/codex/mcp_server.py'


def extracted_server_type(source):
    tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
    selected = []
    names = {'handle_message', '_result', '_error'}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name in names]
            if {n.name for n in methods} == names:
                selected.append(methods)
    if len(selected) != 1:
        raise AssertionError('Expected exactly one class with the three allowed methods')
    methods = copy.deepcopy(selected[0])
    for method in methods:
        method.returns = None
        for arg in method.args.args:
            arg.annotation = None
    unit = ast.Module(body=[ast.ClassDef(name='Extracted', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    ast.fix_missing_locations(unit)
    namespace = {}
    exec(compile(unit, '<three-approved-methods-only>', 'exec'), namespace)
    return namespace['Extracted']


class EnvelopeTest(unittest.TestCase):
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
            return {'ok': True, 'fixed': 'fake'}
        def tool_result(result, is_error):
            self.calls.append(('tool_result', result, is_error))
            return {'fixed_result': result, 'isError': is_error}
        self.server._tool_schemas = schemas
        self.server._call_tool = call_tool
        self.server._tool_result = tool_result

    def error(self, request_id, code, message):
        return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}

    def test_unknown_prefixed_requests(self):
        for request_id in (7, 0, None, 'abc'):
            with self.subTest(request_id=request_id):
                self.assertEqual(self.server.handle_message({'jsonrpc':'2.0','id':request_id,'method':'notifications/not_known'}), self.error(request_id,-32601,'Method not found'))
        self.assertEqual(self.calls, [])

    def test_missing_method_with_id(self):
        self.assertEqual(self.server.handle_message({'jsonrpc':'2.0','id':7}),self.error(7,-32600,'Invalid Request'))
        self.assertEqual(self.calls, [])

    def test_missing_method_without_id_is_invalid_not_notification(self):
        self.assertEqual(self.server.handle_message({'jsonrpc':'2.0'}),self.error(None,-32600,'Invalid Request'))
        self.assertEqual(self.calls, [])

    def test_nonstring_methods(self):
        for method in (None, True, False, 0, 1, [], {}):
            for has_id in (True, False):
                with self.subTest(method=method,has_id=has_id):
                    message={'jsonrpc':'2.0','method':method}
                    if has_id:
                        message['id']=7
                    self.assertEqual(self.server.handle_message(message),self.error(7 if has_id else None,-32600,'Invalid Request'))
        self.assertEqual(self.calls, [])

    def test_true_notifications_unknown_or_ping_stay_silent(self):
        for method in ('notifications/not_known','not_known','ping',''):
            with self.subTest(method=method):
                self.assertIsNone(self.server.handle_message({'jsonrpc':'2.0','method':method}))
        self.assertEqual(self.calls, [])

    def test_unknown_and_empty_string_methods(self):
        for method in ('not_known',''):
            with self.subTest(method=method):
                self.assertEqual(self.server.handle_message({'jsonrpc':'2.0','id':7,'method':method}),self.error(7,-32601,'Method not found'))
        self.assertEqual(self.calls, [])

    def test_ping_id_presence_including_null(self):
        for request_id in (7, 0, None, 'abc'):
            with self.subTest(request_id=request_id):
                self.assertEqual(self.server.handle_message({'jsonrpc':'2.0','id':request_id,'method':'ping'}),{'jsonrpc':'2.0','id':request_id,'result':{}})
        self.assertEqual(self.calls, [])

    def test_list_request_uses_only_fixed_schema_fake(self):
        self.assertEqual(self.server.handle_message({'jsonrpc':'2.0','id':7,'method':'tools/list'}),{'jsonrpc':'2.0','id':7,'result':{'tools':[]}})
        self.assertEqual(self.calls,[('schemas',)])

    def test_list_notification_preserves_existing_fake_dispatch_and_silence(self):
        self.assertIsNone(self.server.handle_message({'jsonrpc':'2.0','method':'tools/list'}))
        self.assertEqual(self.calls,[('schemas',)])

    def test_tool_request_uses_only_fixed_fakes(self):
        message={'jsonrpc':'2.0','id':7,'method':'tools/call','params':{'name':'fixed','arguments':{'x':1}}}
        result=self.server.handle_message(message)
        self.assertEqual(result,{'jsonrpc':'2.0','id':7,'result':{'fixed_result':{'ok':True,'fixed':'fake'},'isError':False}})
        self.assertEqual(self.calls,[('call_tool','fixed',{'x':1},7),('tool_result',{'ok':True,'fixed':'fake'},False)])

    def test_tool_notification_preserves_existing_fake_dispatch_and_silence(self):
        self.assertIsNone(self.server.handle_message({'jsonrpc':'2.0','method':'tools/call','params':{'name':'fixed','arguments':{}}}))
        self.assertEqual(self.calls,[('call_tool','fixed',{},None),('tool_result',{'ok':True,'fixed':'fake'},False)])


if __name__ == '__main__':
    if '--source' in sys.argv:
        index = sys.argv.index('--source')
        SOURCE = Path(sys.argv[index + 1]).resolve()
        del sys.argv[index:index + 2]
    unittest.main()
