import importlib
from pathlib import Path
import pytest

MODULES = ['l5_reader', 'code_maintenance', 'l5_v3_reconcile', 'l5_readiness', 'l5_shadow', 'system_code_repair', 'closure_rehearsal', 'release_lineage']

@pytest.mark.parametrize('module', MODULES)
@pytest.mark.parametrize('value', ['/trusted/repository', Path('/trusted/repository')])
def test_explicit_repository_root_does_not_recurse(module, value):
    resolver = importlib.import_module('eimemory.governance.' + module)._resolve_repo_root
    assert resolver(value) == '/trusted/repository'

@pytest.mark.parametrize('module', MODULES)
def test_missing_repository_root_preserves_trusted_lookup(module, monkeypatch):
    import eimemory.config.trusted as trusted
    monkeypatch.setattr(trusted, 'trusted_repository_root', lambda: Path('/operator/configured'))
    resolver = importlib.import_module('eimemory.governance.' + module)._resolve_repo_root
    assert resolver(None) == '/operator/configured'
    assert resolver('  ') == '/operator/configured'
