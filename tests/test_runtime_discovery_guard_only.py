import subprocess
from pathlib import Path


def test_guard_only_dropin_is_not_a_runtime_identity_binding(tmp_path):
    units = tmp_path / 'systemd'
    units.mkdir()
    ghost = units / 'eimemory-experience-autopromote.service.d'
    ghost.mkdir()
    (ghost / '05-eimemory-storage-release-guard.conf').write_text('[Service]\nExecCondition=/usr/bin/python3 /opt/eimemory/libexec/storage-release-transaction.py guard\n')
    # An actual identity binding must remain discovered even when its base
    # unit resides in a vendor directory rather than the user directory.
    bound = units / 'vendor-worker.service.d'
    bound.mkdir()
    (bound / 'zzzz-eimemory-python-runtime.conf').write_text('[Service]\nEnvironment=EIMEMORY_RUNTIME_COMMIT=abc\n')
    orphan = units / 'retired-worker.service.d'
    orphan.mkdir()
    (orphan / 'zzzz-eimemory-python-runtime.conf').write_text('[Service]\nEnvironment=EIMEMORY_RUNTIME_COMMIT=old\n')
    command = '''systemctl() {
      case "$*" in
        *vendor-worker.service*) printf 'loaded\\n' ;;
        *) printf 'not-found\\n' ;;
      esac
    }; export -f systemctl; bash "$1" "$2"'''
    result = subprocess.run(['bash', '-c', command, 'bash', str(Path('deploy/discover_python_runtime_units.sh')), str(units)], capture_output=True, text=True, check=True)
    discovered = result.stdout.splitlines()
    assert 'retired-worker.service' not in discovered
    assert 'eimemory-experience-autopromote.service' not in discovered
    assert 'vendor-worker.service' in discovered
