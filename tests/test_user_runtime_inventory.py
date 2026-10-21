"""User-manager inventory must not require the legacy privileged manager."""
from types import SimpleNamespace

import pytest

from eimemory.ops import controlled_stability as probe
from eimemory.ops import runtime_service_observation as observation
from eimemory.core import runtime_manager


def test_user_inventory_never_contacts_privileged_registry(monkeypatch):
    monkeypatch.setattr(observation, 'registered_runtime_context', lambda: False)
    monkeypatch.setattr(runtime_manager, 'read_registry',
                        lambda: pytest.fail('user inventory contacted privileged manager'))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='\n'.join(
            f'hermes-gateway-{name}.service enabled enabled' for name in ('c', 'b', 'a')))

    monkeypatch.setattr(probe.subprocess, 'run', run)
    assert probe._runtime_units() == (*probe.RUNTIME_UNITS,
        'hermes-gateway-a.service', 'hermes-gateway-b.service', 'hermes-gateway-c.service')
    assert calls[0][:2] == ['systemctl', '--user']


@pytest.mark.parametrize('fault', ['denied', 'partial', 'duplicate', 'oversized'])
def test_user_inventory_remains_fail_closed(monkeypatch, fault):
    rows = [f'hermes-gateway-{name}.service enabled enabled' for name in ('a', 'b', 'c')]
    if fault == 'partial':
        rows.pop()
    if fault == 'duplicate':
        rows.append(rows[0])
    output = '\n'.join(rows) if fault != 'oversized' else 'x' * (1024 * 1024 + 1)
    monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **kw:
        SimpleNamespace(returncode=1 if fault == 'denied' else 0, stdout=output))
    with pytest.raises((ValueError, RuntimeError)):
        probe._user_runtime_units()
