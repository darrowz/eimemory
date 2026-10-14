import os
from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from eimemory.adapters.hermes import outbox


def test_matching_host_root_evidence_is_accepted(monkeypatch):
    info = SimpleNamespace(st_uid=65534, st_dev=1, st_ino=2, st_mode=0o40755, st_ctime_ns=3)
    monkeypatch.setattr(outbox, '_private_user_mapping', lambda: True)
    monkeypatch.setattr(outbox.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps([0,1,2,0o40755,3])))
    assert outbox._host_root_owner(Path('/'), info)


@pytest.mark.parametrize('evidence', [[1002,1,2,0o40755,3], [0,1,9,0o40755,3], [0,1,2,0o40777,3], [0,1,2,0o40755,9]])
def test_unknown_owner_or_replaced_inode_is_rejected(monkeypatch, evidence):
    info = SimpleNamespace(st_uid=65534, st_dev=1, st_ino=2, st_mode=0o40755, st_ctime_ns=3)
    monkeypatch.setattr(outbox, '_private_user_mapping', lambda: True)
    monkeypatch.setattr(outbox.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(evidence)))
    assert not outbox._host_root_owner(Path('/'), info)


def test_host_probe_failure_stays_closed(monkeypatch):
    info = SimpleNamespace(st_uid=65534, st_dev=1, st_ino=2, st_mode=0o40755, st_ctime_ns=3)
    monkeypatch.setattr(outbox, '_private_user_mapping', lambda: True)
    monkeypatch.setattr(outbox.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout=''))
    assert not outbox._host_root_owner(Path('/'), info)
