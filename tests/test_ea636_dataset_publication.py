"""Only one approved function is compiled; no application imports or data."""
import ast
import json
import os
from pathlib import Path
from hashlib import sha256
import tempfile
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'eimemory/evaluation/production_query_dataset.py'

def writer(path, injected=None):
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'write_production_query_dataset')
    node.returns = None
    for arg in node.args.args:
        arg.annotation = None
    namespace = dict(Path=Path,json=json,sha256=sha256,tempfile=tempfile,os=injected or os)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    return namespace['write_production_query_dataset']

class WriterChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ea636-')
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name)/'snapshot.json'
        self.source = SOURCE
    def test_different_concurrent_publication_is_not_overwritten(self):
        winning = b'{"winner":true}\n'
        def race(src,dst,operation):
            self.target.write_bytes(winning)
            return operation(src,dst)
        proxy = SimpleNamespace(**{n:getattr(os,n) for n in ('fdopen','fsync','chmod','close','link','replace')})
        proxy.link = lambda src,dst: race(src,dst,os.link)
        proxy.replace = lambda src,dst: race(src,dst,os.replace)
        with self.assertRaises(FileExistsError):
            writer(self.source,proxy)({'loser':True},self.target)
        self.assertEqual(self.target.read_bytes(),winning)
    def test_other_writer_temporary_is_not_deleted(self):
        data={'a':1}
        raw=(json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
        other=self.target.with_name('.'+self.target.name+'.'+sha256(raw).hexdigest()[:16]+'.tmp')
        other.write_bytes(b'other-writer-in-progress')
        try:
            writer(self.source)(data,self.target)
        except FileExistsError:
            pass
        self.assertEqual(other.read_bytes(),b'other-writer-in-progress')
    def test_sequential_retry_and_conflict(self):
        fn=writer(self.source)
        result=fn({'a':1},self.target)
        self.assertFalse(result['unchanged'])
        self.assertEqual(self.target.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.target.stat().st_nlink,1)
        self.assertTrue(fn({'a':1},self.target)['unchanged'])
        with self.assertRaises(FileExistsError):fn({'a':2},self.target)
        self.assertEqual(self.target.read_bytes(),b'{"a":1}\n')
    def test_same_concurrent_publication_is_idempotent(self):
        raw=b'{"a":1}\n'
        def race(src,dst,operation):
            self.target.write_bytes(raw)
            return operation(src,dst)
        proxy=SimpleNamespace(**{n:getattr(os,n) for n in ('fdopen','fsync','chmod','close','link','replace')})
        proxy.link = lambda src,dst: race(src,dst,os.link)
        proxy.replace = lambda src,dst: race(src,dst,os.replace)
        result=writer(self.source,proxy)({'a':1},self.target)
        self.assertTrue(result['unchanged'])
        self.assertEqual(self.target.read_bytes(),raw)

    def test_publication_error_cleans_only_owned_temp(self):
        def fail(src,dst):raise OSError('synthetic publication unavailable')
        proxy=SimpleNamespace(**{n:getattr(os,n) for n in ('fdopen','fsync','chmod','close','link','replace')})
        proxy.link = fail
        proxy.replace = fail
        with self.assertRaises(OSError):writer(self.source,proxy)({'a':1},self.target)
        self.assertEqual(list(self.target.parent.iterdir()),[])

    def test_cleanup_failure_is_reported_after_publication(self):
        from unittest.mock import patch
        original_unlink=Path.unlink
        def fail_owned(path,*args,**kwargs):
            if path.parent == self.target.parent and path.name.endswith('.tmp'):
                raise OSError('synthetic cleanup failure')
            return original_unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail_owned):
            with self.assertRaises(OSError):writer(self.source)({'a':1},self.target)
        self.assertEqual(self.target.read_bytes(),b'{"a":1}\n')
        self.assertEqual(self.target.stat().st_nlink,2)

if __name__=='__main__':
    unittest.main(verbosity=2)
