import os
import stat
import pytest
from eimemory.adapters.hermes.outbox import HermesOutbox


def test_new_profile_is_private_under_group_writable_umask(tmp_path, trusted_dataset_path_ancestors):
    old = os.umask(0o002)
    try:
        home = tmp_path / 'new-profile'
        box = HermesOutbox(str(home), {'scope': {'tenant_id': 'test'}})
        assert box.put('event', 'write', {'event': 1})
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        assert box.status()['pending_writes'] == 1
    finally:
        os.umask(old)


def test_existing_group_writable_profile_is_not_changed(tmp_path):
    home = tmp_path / 'unsafe-profile'
    home.mkdir(mode=0o770)
    home.chmod(0o770)
    box = HermesOutbox(str(home), {'scope': {'tenant_id': 'test'}})
    with pytest.raises(ValueError, match='outbox_ancestor_untrusted'):
        box.put('event', 'write', {'event': 1})
    assert stat.S_IMODE(home.stat().st_mode) == 0o770
