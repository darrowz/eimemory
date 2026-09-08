from pathlib import Path

from eimemory.retrieval import vector_sync_worker as worker


def test_current_or_disabled_index_does_not_reembed(monkeypatch):
    for state in ({'enabled':False}, {'enabled':True,'available':True,'watermark':'current'}):
        calls=[]
        def handle(command, runtime):
            calls.append(command.vector_index_command)
            return {'ok':True,'vector_index':state}
        monkeypatch.setattr(worker,'handle_vector_index_command',handle)
        assert worker.maintain_index(object())['ok']
        assert calls == ['status']


def test_stale_index_uses_bounded_resumable_sync(monkeypatch):
    calls=[]
    def handle(command, runtime):
        calls.append(command)
        return {'ok':True,'vector_index':{'enabled':True,'available':False}} if command.vector_index_command == 'status' else {'ok':True,'complete':False}
    monkeypatch.setattr(worker,'handle_vector_index_command',handle)
    assert worker.maintain_index(object()) == {'ok':True,'complete':False}
    assert calls[-1].batch_size == 4 and calls[-1].max_pages == 25


def test_maintenance_can_build_without_enabling_serving(monkeypatch):
    calls = []
    def handle(command, runtime):
        calls.append(command.vector_index_command)
        if command.vector_index_command == 'status':
            return {'ok':True, 'vector_index':{'enabled':False, 'maintenance_enabled':True, 'available':False}}
        return {'ok':True, 'complete':False}
    monkeypatch.setattr(worker, 'handle_vector_index_command', handle)
    assert worker.maintain_index(object())['ok']
    assert calls == ['status', 'sync']


def test_optional_deployment_wires_configs_and_dependency():
    unit=Path('deploy/systemd/eimemory-rpc.service').read_text()
    assert 'EnvironmentFile=-/etc/eimemory/postgres.env' in unit
    assert 'EnvironmentFile=-/etc/eimemory/embedding.env' in unit
    script=Path('deploy/install_immutable_release.sh').read_text()
    assert 'EIMEMORY_INSTALL_POSTGRES_EXTRA:-0' in script
    assert 'pip install "$STAGE_DIR[postgres]"' in script
