import json
from pathlib import Path
import pytest
from eimemory.governance.release_binding_refresh import refresh_bindings


def test_invalid_receipt_preserves_binding(tmp_path):
    path = tmp_path/'bindings.json'
    path.write_text('[{"scope": {}}]')
    path.chmod(0o600)
    before = path.read_bytes()
    class Store:
        def get_by_id(self, *args, **kwargs):
            return None
    class Runtime:
        store = Store()
    with pytest.raises(ValueError):
        refresh_bindings(Runtime(), path, 'missing')
    assert path.read_bytes() == before


@pytest.mark.parametrize('invalid', ['', 'commit', 'scope'])
def test_refresh_checks_current_receipt_and_preserves_grants(tmp_path, monkeypatch, invalid):
    from eimemory.api.runtime import Runtime
    from eimemory.governance.evidence_contract import current_release_identity
    from test_release_scope_binding import configure, receipt_for_service, TARGET, RELEASE
    runtime = Runtime.create(root=tmp_path/'store')
    runtime._test_runtime_commit = RELEASE.commit
    try:
        record = runtime.store.append(receipt_for_service())
        path = configure(tmp_path, monkeypatch, record)
        if invalid == 'commit':
            runtime._test_runtime_commit = 'c'*40
        elif invalid:
            entries = json.loads(path.read_text())
            if invalid == 'scope':
                entries[0]['scope']['tenant_id'] = 'unauthorized'
            else:
                entries[0]['receipt_sha256'] = 'wrong'
            path.write_text(json.dumps(entries))
        before = path.read_bytes()
        if invalid:
            with pytest.raises(ValueError):
                refresh_bindings(runtime, path, record.record_id)
            assert path.read_bytes() == before
        else:
            assert refresh_bindings(runtime, path, record.record_id) == 1
            assert json.loads(path.read_text())[0]['scope'] == TARGET
            assert current_release_identity(runtime, TARGET).receipt_id == record.record_id
            assert current_release_identity(runtime, {**TARGET, 'user_id': 'other'}) is None
            assert path.stat().st_mode & 0o777 == 0o600
    finally:
        runtime.close()


def test_stale_pin_digest_still_refreshes_same_receipt(tmp_path, monkeypatch):
    from eimemory.api.runtime import Runtime
    from eimemory.governance.evidence_contract import current_release_identity
    from test_release_scope_binding import configure, receipt_for_service, TARGET, RELEASE

    runtime = Runtime.create(root=tmp_path / 'store')
    runtime._test_runtime_commit = RELEASE.commit
    try:
        record = runtime.store.append(receipt_for_service())
        path = configure(tmp_path, monkeypatch, record)
        entries = json.loads(path.read_text())
        entries[0]['receipt_sha256'] = 'stale-digest'
        path.write_text(json.dumps(entries))
        assert refresh_bindings(runtime, path, record.record_id) == 1
        refreshed = json.loads(path.read_text())[0]
        assert refreshed['receipt_id'] == record.record_id
        assert refreshed['receipt_sha256'] != 'stale-digest'
        assert current_release_identity(runtime, TARGET).receipt_id == record.record_id
    finally:
        runtime.close()
