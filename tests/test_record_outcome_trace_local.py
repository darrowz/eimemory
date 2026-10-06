"""No project imports or real I/O: compile CLI declarations, replace all endpoints."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import urllib.error

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "record_outcome_trace.py"
SOURCE_CODE = compile(SOURCE.read_text(encoding="utf-8"), str(SOURCE), 'exec')

class FakeResponse:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return self.body

class TraceTests(unittest.TestCase):
    def invoke(self, response=b'{"ok":true}', payload='{}', http_error=False, transport_error=None, argv=None):
        namespace = {'__name__': 'inert_trace_review'}
        exec(SOURCE_CODE, namespace)
        calls = []
        def read_text(**kwargs):
            if isinstance(payload, Exception): raise payload
            return payload
        def request(url, **kwargs):
            calls.append((url, kwargs))
            return kwargs
        def urlopen(req, timeout):
            self.assertEqual(timeout, 10)
            if transport_error: raise transport_error
            if http_error: raise urllib.error.HTTPError('https://inert.invalid/', 500, 'fake', {}, io.BytesIO(response))
            return FakeResponse(response)
        namespace['Path'] = lambda _: SimpleNamespace(read_text=read_text)
        namespace['urllib'] = SimpleNamespace(error=urllib.error, request=SimpleNamespace(Request=request, urlopen=urlopen))
        out = io.StringIO()
        with contextlib.redirect_stdout(out): status = namespace['main'](argv or ['ignored'])
        return status, json.loads(out.getvalue()), calls

    def test_success_and_request_shape(self):
        status, result, calls = self.invoke(payload='{"text":"中文"}', argv=['ignored','--tenant-id','','--url','https://inert.invalid/'])
        self.assertEqual((status,result), (0, {'ok':True}))
        url, request = calls[0]
        self.assertEqual(url,'https://inert.invalid/')
        self.assertEqual(request['method'],'POST')
        self.assertEqual(request['headers'], {'Content-Type':'application/json'})
        sent=json.loads(request['data'])
        self.assertEqual(sent['method'],'experience.record_outcome_trace')
        self.assertNotIn('tenant_id',sent['params']['scope'])
        self.assertEqual(sent['params']['payload'],{'text':'中文'})

    def test_input_shapes(self):
        for payload in ('[]','null','1','"x"'):
            with self.subTest(payload=payload):
                status,result,calls=self.invoke(payload=payload)
                self.assertEqual((status,result['error'],calls),(2,'invalid_payload',[]))

    def test_invalid_input_json(self):
        status,result,calls=self.invoke(payload='{')
        self.assertEqual((status,result['error'],calls),(2,'invalid_json',[]))

    def test_file_error(self):
        status,result,calls=self.invoke(payload=OSError('fake'))
        self.assertEqual((status,result['error'],calls),(2,'invalid_json',[]))

    def test_file_utf8_error(self):
        status,result,calls=self.invoke(payload=UnicodeDecodeError('utf8',b'\xff',0,1,'fake'))
        self.assertEqual((status,result['error'],calls),(2,'invalid_json',[]))

    def test_valid_failure(self):
        for http_error in (False,True):
            with self.subTest(http_error=http_error):
                status,result,_=self.invoke(response=b'{"ok":false}',http_error=http_error)
                self.assertEqual((status,result),(2,{'ok':False}))

    def test_bad_response_encoding_or_json(self):
        for http_error in (False,True):
            for body in (b'<html>failed</html>',b'\xff',b''):
                with self.subTest(http_error=http_error,body=body):
                    status,result,_=self.invoke(response=body,http_error=http_error)
                    self.assertEqual((status,result['error']),(2,'rpc_request_failed'))

    def test_nonobject_response(self):
        for http_error in (False,True):
            for body in (b'[]',b'null',b'false',b'1',b'"text"'):
                with self.subTest(http_error=http_error,body=body):
                    status,result,_=self.invoke(response=body,http_error=http_error)
                    self.assertEqual((status,result['error']),(2,'rpc_request_failed'))

    def test_transport_error(self):
        status,result,_=self.invoke(transport_error=OSError('fake'))
        self.assertEqual((status,result['error']),(2,'rpc_request_failed'))

    def test_unencodable_input(self):
        status,result,calls=self.invoke(payload='{"text":"\\ud800"}')
        self.assertEqual((status,result['error'],calls),(2,'rpc_request_failed',[]))

    def test_missing_ok_preserved(self):
        status,result,_=self.invoke(response=b'{}')
        self.assertEqual((status,result),(0,{}))

if __name__=='__main__': unittest.main()
