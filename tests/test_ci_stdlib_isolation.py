"""Fault injection must leave pytest's process-wide filesystem usable."""
import os
import linecache
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_deployment_tools import _load_managed_systemd_dropin_installer


@pytest.mark.parametrize('load', [_load_managed_systemd_dropin_installer,
    lambda: __import__('deploy.storage_release_transaction', fromlist=['*'])])
def test_faults_are_private_while_monkeypatch_is_active(load, monkeypatch, tmp_path):
    module = load()
    real = {name: getattr(os, name) for name in ('stat', 'open', 'close', 'fstat', 'fsync')}
    from stdlib_isolation import isolate_stdlib
    isolate_stdlib(module, monkeypatch)
    for name in real:
        monkeypatch.setattr(module.os, name, lambda *a, **kw: SimpleNamespace(st_mode=0))
    for name, function in real.items():
        assert getattr(os, name) is function
    path = tmp_path / 'source.py'
    path.write_text('raise ValueError("real traceback")\n')
    assert linecache.getlines(str(path)) == ['raise ValueError("real traceback")\n']
    assert path.stat().st_size > 0


def test_path_and_sys_proxies_are_private(monkeypatch):
    import sys
    from stdlib_isolation import isolate_stdlib
    import pathlib
    module = SimpleNamespace(os=os, sys=sys, Path=Path, pathlib=pathlib)
    original_stat, original_argv = Path.stat, sys.argv
    isolate_stdlib(module, monkeypatch)
    monkeypatch.setattr(module.Path, 'stat', lambda *a, **kw: SimpleNamespace(st_uid=1))
    monkeypatch.setattr(module.sys, 'argv', ['injected'])
    assert Path.stat is original_stat
    assert sys.argv is original_argv
    assert module.pathlib.Path is module.Path
    assert module.pathlib.Path is not pathlib.Path
    assert module.pathlib.Path('.').stat().st_uid == 1
    assert module.Path('.').stat().st_uid == 1


@pytest.mark.parametrize('case', ['parent_open', 'durable_sync', 'snapshot'])
def test_original_fault_cases_keep_real_os_functions(case, tmp_path, monkeypatch):
    from test_deployment_tools import (
        test_managed_systemd_dropin_initial_parent_open_propagates_non_symlink_errors,
    )
    from test_storage_deploy import (
        test_durable_guard_sync_fsyncs_file_and_every_parent_and_propagates_failure,
    )
    from test_production_recall_bootstrap_deploy import (
        test_snapshot_directory_chain_revalidation_rejects_replaced_ancestor_entry,
    )
    real = {name: getattr(os, name) for name in
            ('stat', 'open', 'close', 'fstat', 'fsync', 'write', 'unlink', 'replace')}
    with monkeypatch.context() as active:
        if case == 'parent_open':
            import errno
            test_managed_systemd_dropin_initial_parent_open_propagates_non_symlink_errors(
                tmp_path, active, errno.EACCES)
        elif case == 'durable_sync':
            test_durable_guard_sync_fsyncs_file_and_every_parent_and_propagates_failure(
                tmp_path, active)
        else:
            test_snapshot_directory_chain_revalidation_rejects_replaced_ancestor_entry(active)
        for name, function in real.items():
            assert getattr(os, name) is function
        assert Path(__file__).stat().st_size > 0
