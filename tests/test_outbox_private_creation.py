import os
import stat
import pytest
from eimemory.adapters.hermes.outbox import HermesOutbox


def test_private_queue_does_not_create_a_native_profile_under_group_writable_umask(tmp_path, trusted_dataset_path_ancestors):
    old = os.umask(0o002)
    try:
        home = tmp_path / 'new-profile'
        box = HermesOutbox(str(home), {'scope': {'tenant_id': 'test'}})
        assert box.put('event', 'write', {'event': 1})
        assert not home.exists()
        assert stat.S_IMODE(box.path.parent.stat().st_mode) == 0o700
        assert box.status()['pending_writes'] == 1
    finally:
        os.umask(old)


def test_existing_group_writable_profile_is_not_changed(tmp_path, trusted_dataset_path_ancestors):
    home = tmp_path / 'unsafe-profile'
    home.mkdir(mode=0o770)
    home.chmod(0o770)
    box = HermesOutbox(str(home), {'scope': {'tenant_id': 'test'}})
    assert box.put('event', 'write', {'event': 1})
    assert not box.path.is_relative_to(home)
    assert stat.S_IMODE(home.stat().st_mode) == 0o770
