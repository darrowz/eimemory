"""Private handoff migration preserves real legacy bytes and transaction state."""
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from eimemory.adapters.hermes import outbox

pytestmark = [pytest.mark.usefixtures('trusted_dataset_path_ancestors'),
    pytest.mark.skipif(os.name!='posix',reason='native private files and POSIX migration locks')]
NAMESPACE = {'channel':'hermes','scope':{'agent_id':'colleague'},'endpoint':'http://127.0.0.1:8091/'}


def legacy_queue(tmp_path):
    profile=tmp_path/'native-profile'; state=profile/'state'
    state.mkdir(parents=True,mode=0o700)
    profile.chmod(0o700); state.chmod(0o700)
    box=outbox.HermesOutbox(str(profile),NAMESPACE)
    box.put('pending-write','write',{'body':'original pending business handoff'})
    box.put('partial-delivery','delivery',{'body':'original unacknowledged delivery'})
    box.update('partial-delivery',stage=1)
    raw=box.path.read_bytes()
    box.path.replace(box.legacy_path)
    return box,profile,raw


def test_exact_legacy_payload_is_archived_and_migrated_once(tmp_path):
    previous,profile,raw=legacy_queue(tmp_path)
    recovered=outbox.HermesOutbox(str(profile),NAMESPACE)
    assert recovered.error=='' and recovered.recovered_count==2
    assert recovered.path.read_bytes()==raw
    assert not previous.legacy_path.exists()
    assert recovered.path.with_name('legacy.original.json').read_bytes()==raw
    receipt=json.loads(recovered.path.with_name('legacy-migration.json').read_text())
    assert receipt['sha256']==sha256(raw).hexdigest()
    assert recovered.entries('delivery')[0][1]['stage']==1
    recovered.update('pending-write',outcome='completed')
    again=outbox.HermesOutbox(str(profile),NAMESPACE)
    assert [key for key,_ in again.entries()]==['partial-delivery']
    assert again.path.with_name('legacy.original.json').read_bytes()==raw


@pytest.mark.parametrize('boundary',['archive','queue','receipt','unlink'])
def test_every_migration_crash_boundary_resumes_without_loss_or_duplicate(tmp_path,monkeypatch,boundary):
    previous,profile,raw=legacy_queue(tmp_path)
    write_bytes=outbox.atomic_write_bytes; write_json=outbox.atomic_write_json; unlink=Path.unlink
    failed=False
    def publish(path,payload):
        nonlocal failed
        result=write_bytes(path,payload)
        if not failed and ((boundary=='archive' and path.name=='legacy.original.json')
                or (boundary=='queue' and path.name=='outbox.json')):
            failed=True; raise OSError('injected post-publication crash')
        return result
    def receipt(path,payload,**kwargs):
        nonlocal failed
        result=write_json(path,payload,**kwargs)
        if not failed and boundary=='receipt' and path.name=='legacy-migration.json':
            failed=True; raise OSError('injected receipt publication crash')
        return result
    def retire(path,*args,**kwargs):
        nonlocal failed
        result=unlink(path,*args,**kwargs)
        if not failed and boundary=='unlink' and path==previous.legacy_path:
            failed=True; raise OSError('injected retirement crash')
        return result
    with monkeypatch.context() as crash:
        crash.setattr(outbox,'atomic_write_bytes',publish)
        crash.setattr(outbox,'atomic_write_json',receipt)
        crash.setattr(Path,'unlink',retire)
        interrupted=outbox.HermesOutbox(str(profile),NAMESPACE)
        assert failed and interrupted.error=='outbox_unavailable'
    resumed=outbox.HermesOutbox(str(profile),NAMESPACE)
    assert resumed.error==''
    assert resumed.path.read_bytes()==raw
    assert resumed.path.with_name('legacy.original.json').read_bytes()==raw
    assert {key for key,_ in resumed.entries()}=={'pending-write','partial-delivery'}
    assert not resumed.legacy_path.exists()


@pytest.mark.parametrize('fault',['writable_ancestor','symlink','hardlink','world_readable','corrupt'])
def test_untrusted_old_payload_is_preserved_and_never_replaced_by_empty_queue(tmp_path,fault):
    previous,profile,raw=legacy_queue(tmp_path)
    if fault=='writable_ancestor':profile.chmod(0o775)
    elif fault=='symlink':
        original=previous.legacy_path.with_name('original.json');previous.legacy_path.rename(original)
        previous.legacy_path.symlink_to(original)
    elif fault=='hardlink':os.link(previous.legacy_path,profile/'duplicate.json')
    elif fault=='world_readable':previous.legacy_path.chmod(0o644)
    else:previous.legacy_path.write_bytes(b'{broken legacy JSON')
    recovered=outbox.HermesOutbox(str(profile),NAMESPACE)
    assert recovered.error=='outbox_unavailable'
    assert not recovered.path.exists()
    assert previous.legacy_path.exists()
    assert previous.legacy_path.read_bytes()==(b'{broken legacy JSON' if fault=='corrupt' else raw)


def test_recreated_old_writer_cannot_replay_acknowledged_payload(tmp_path):
    previous,profile,raw=legacy_queue(tmp_path)
    recovered=outbox.HermesOutbox(str(profile),NAMESPACE)
    recovered.update('pending-write',outcome='completed')
    # A stale older process publishes a changed queue after the cutover.
    state=json.loads(raw);state['entries']['pending-write']['payload']['body']='stale rewrite'
    outbox.atomic_write_json(previous.legacy_path,state)
    with pytest.raises(ValueError,match='legacy_changed_after_migration'):
        recovered.entries()
    assert 'pending-write' not in json.loads(recovered.path.read_bytes())['entries']
    assert previous.legacy_path.exists()


def test_legacy_inflight_consumer_prevents_migration_without_touching_payload(tmp_path):
    previous,profile,raw=legacy_queue(tmp_path)
    with outbox.interprocess_lock(previous.legacy_path.with_suffix('.consumer.lock')):
        blocked=outbox.HermesOutbox(str(profile),NAMESPACE)
        assert blocked.error=='outbox_unavailable'
        assert previous.legacy_path.read_bytes()==raw
        assert not blocked.path.exists()
        assert not blocked.path.with_name('legacy.original.json').exists()
    recovered=outbox.HermesOutbox(str(profile),NAMESPACE)
    assert recovered.error=='' and recovered.path.read_bytes()==raw


@pytest.mark.parametrize('source_present',[False,True])
def test_missing_migrated_destination_never_becomes_a_fresh_empty_queue(tmp_path,source_present):
    previous,profile,raw=legacy_queue(tmp_path)
    recovered=outbox.HermesOutbox(str(profile),NAMESPACE)
    recovered.update('pending-write',outcome='completed')
    recovered.path.unlink()
    if source_present:outbox.atomic_write_bytes(previous.legacy_path,raw)
    for box in (recovered,outbox.HermesOutbox(str(profile),NAMESPACE)):
        with pytest.raises(ValueError,match='migration_destination_missing'):
            box.put('new','write',{'event':3})
        assert not box.path.exists()
        assert box.path.with_name('legacy.original.json').read_bytes()==raw
    assert previous.legacy_path.exists() is source_present


def test_profile_and_scope_isolation_do_not_rely_on_native_workspace_permissions(tmp_path):
    parent=tmp_path/'collaboration'; parent.mkdir();parent.chmod(0o775)
    first=parent/'hongtai'; first.mkdir();first.chmod(0o775)
    second=parent/'xiaomage';second.mkdir();second.chmod(0o775)
    boxes=[outbox.HermesOutbox(str(profile),namespace) for profile,namespace in (
        (first,NAMESPACE),(second,NAMESPACE),(first,{**NAMESPACE,'endpoint':'http://other:8091/'}))]
    assert len({box.path for box in boxes})==3
    for index,box in enumerate(boxes):assert box.put(str(index),'write',{'index':index})
    assert all(not box.path.is_relative_to(parent) for box in boxes)
    assert all(path.stat().st_mode&0o777==0o775 for path in (parent,first,second))
