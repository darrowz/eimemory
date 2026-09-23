from pathlib import Path


def test_rollback_metadata_uses_current_controller_discovery():
    script = Path('deploy/install_immutable_release.sh').read_text()
    body = script.split('_install_current_runtime_metadata() {', 1)[1].split('\n}', 1)[0]
    assert '< "$metadata_release/deploy/discover_python_runtime_units.sh"' in body
    assert '< "$target_release/deploy/discover_python_runtime_units.sh"' not in body
