"""Synthetic files only; reuse isolated AST writer, no application imports."""
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_ea636_dataset_publication import WriterChecks, writer

class CompletionChecks(WriterChecks):
    def test_equal_content_during_other_writer_cleanup_is_retryable(self):
        linked=threading.Event()
        release=threading.Event()
        outcome=[]
        def blocked_link(src,dst):
            os.link(src,dst)
            linked.set()
            if not release.wait(5):
                raise RuntimeError('test release timed out')
        proxy=SimpleNamespace(**{n:getattr(os,n) for n in ('fdopen','fsync','chmod','close','link')})
        proxy.link=blocked_link
        def first():
            try:outcome.append(writer(self.source,proxy)({'a':1},self.target))
            except BaseException as exc:outcome.append(exc)
        worker=threading.Thread(target=first)
        worker.start()
        try:
            self.assertTrue(linked.wait(5))
            self.assertEqual(self.target.stat().st_nlink,2)
            with self.assertRaisesRegex(RuntimeError,'publication is incomplete; retry'):
                writer(self.source)({'a':1},self.target)
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcome),1)
        self.assertIsInstance(outcome[0],dict)
        self.assertEqual(self.target.stat().st_nlink,1)
        self.assertTrue(writer(self.source)({'a':1},self.target)['unchanged'])

    def test_link_collision_with_two_links_cleans_only_own_temporary(self):
        other=self.target.parent/'other-writer.tmp'
        other.write_bytes(b'{"a":1}\n')
        def collision(src,dst):
            os.link(other,dst)
            os.link(src,dst)
        proxy=SimpleNamespace(**{n:getattr(os,n) for n in ('fdopen','fsync','chmod','close','link')})
        proxy.link=collision
        with self.assertRaisesRegex(RuntimeError,'publication is incomplete; retry'):
            writer(self.source,proxy)({'a':1},self.target)
        self.assertEqual(set(self.target.parent.iterdir()),{other,self.target})
        self.assertEqual(self.target.stat().st_nlink,2)
        other.unlink()  # Fixture owns this path; production writer did not remove it.
        self.assertTrue(writer(self.source)({'a':1},self.target)['unchanged'])

    def test_persistent_cleanup_failure_reports_owned_path_and_retry_fails(self):
        original_unlink=Path.unlink
        owned=[]
        def fail_owned(path,*args,**kwargs):
            if path.parent==self.target.parent and path.name.endswith('.tmp'):
                owned.append(path)
                raise OSError('synthetic cleanup failure')
            return original_unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail_owned):
            with self.assertRaises(OSError) as raised:
                writer(self.source)({'a':1},self.target)
        self.assertEqual(len(owned),1)
        self.assertIn(str(owned[0]),str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__,OSError)
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError,'publication is incomplete; retry'):
                writer(self.source)({'a':1},self.target)
        self.assertTrue(owned[0].exists())
        self.assertEqual(self.target.stat().st_nlink,2)
        self.assertEqual(set(self.target.parent.iterdir()),{owned[0],self.target})

if __name__=='__main__':
    suite=unittest.TestSuite(CompletionChecks(name) for name in (
        'test_equal_content_during_other_writer_cleanup_is_retryable',
        'test_link_collision_with_two_links_cleans_only_own_temporary',
        'test_persistent_cleanup_failure_reports_owned_path_and_retry_fails',
    ))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
