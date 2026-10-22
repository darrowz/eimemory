"""The EI hint never repairs a native gateway directory's permissions."""
import os
import stat

import pytest

from eimemory.adapters.runtime.receipt_handoff import ReceiptIdHandoff


def append(handoff):
    handoff.append(channel='hermes', scope=dict(tenant_id='t', agent_id='a', workspace_id='w', user_id='u'),
                   session_id='session', run_id='run', receipt_id='receipt-1')


@pytest.mark.skipif(os.name != 'posix', reason='native Unix directory permissions')
def test_existing_directory_is_never_chmodded(tmp_path):
    native = tmp_path / 'native-profile'
    native.mkdir(mode=0o755)
    # Establish the native owner's policy independently of the test runner's
    # service umask; mkdir(mode=...) alone can yield 0750 under umask 0027.
    native.chmod(0o755)
    assert stat.S_IMODE(native.stat().st_mode) == 0o755
    append(ReceiptIdHandoff(native / 'hint.sqlite'))
    assert stat.S_IMODE(native.stat().st_mode) == 0o755


@pytest.mark.skipif(os.name != 'posix', reason='native Unix directory permissions')
def test_shared_writable_directory_is_rejected_without_mutation(tmp_path):
    native = tmp_path / 'native-profile'
    native.mkdir()
    native.chmod(0o775)
    assert stat.S_IMODE(native.stat().st_mode) == 0o775
    with pytest.raises(ValueError, match='not shared writable'):
        append(ReceiptIdHandoff(native / 'hint.sqlite'))
    assert stat.S_IMODE(native.stat().st_mode) == 0o775
    assert not (native / 'hint.sqlite').exists()


@pytest.mark.skipif(os.name != 'posix', reason='native Unix directory permissions')
def test_new_handoff_leaf_is_private(tmp_path):
    leaf = tmp_path / 'owned-handoff'
    append(ReceiptIdHandoff(leaf / 'hint.sqlite'))
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o700
