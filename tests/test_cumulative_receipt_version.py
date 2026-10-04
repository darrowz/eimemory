from pathlib import Path
import pytest
from eimemory.governance.release import deployment_receipt as receipt
from eimemory.governance.release.release_impact import _normalized_version_module


@pytest.mark.parametrize('module,expected', [
    ('__version__ = "1.2.3"', '1.2.3'),
    ('__version__: str = "1.2.3"', '1.2.3'),
    ('"""__version__ = \'1.2.3\'"""', ''),
    ('__version__ = "1.2.3"\n__version__ = "9.9.9"', ''),
    ('__version__ = other = "1.2.3"', ''),
    ('__version__ = compute()', ''),
    ('if True:\n    __version__ = "1.2.3"', ''),
    ('__version__ = "9.9.9"', ''),
    ('__version__ =', ''),
    ('', ''),
])
def test_receipt_reads_literal_metadata_without_execution(monkeypatch, module, expected):
    monkeypatch.setattr(receipt, '_git', lambda repo, *args: '[project]\nversion = "1.2.3"' if args[-1].endswith('pyproject.toml') else module)
    assert receipt._project_version(Path('.'), commit='fixture') == expected


def test_impact_keeps_executable_version_changes_visible():
    assert _normalized_version_module(b'__version__: str = "1"') == _normalized_version_module(b'__version__: str = "2"')
    for before, after in [(b'__version__ = f("1")', b'__version__ = f("2")'), (b'__version__ = other = "1"', b'__version__ = other = "2"'), (b'if True:\n __version__ = "1"', b'if True:\n __version__ = "2"')]:
        assert _normalized_version_module(before) != _normalized_version_module(after)


@pytest.mark.parametrize('toml', ['project = 1', 'project = []', '[project]\nversion = 123', '[project]\nversion = ""', 'not valid toml'])
def test_receipt_rejects_malformed_project_version(monkeypatch,toml):
    monkeypatch.setattr(receipt,'_git',lambda repo,*args:toml if args[-1].endswith('pyproject.toml') else '__version__ = "123"')
    assert receipt._project_version(Path('.')) == ''


@pytest.mark.parametrize('module', ['', '__version__ = "1.2.3"\n__version__ = "9.9.9"'])
def test_public_receipt_rejects_bad_version_before_health_or_write(tmp_path, monkeypatch, module):
    from types import SimpleNamespace
    repo = tmp_path / 'fixture-repo'
    (repo / '.git').mkdir(parents=True)
    link = tmp_path / 'fixture-current'
    monkeypatch.setenv('EIMEMORY_TRUSTED_REPOSITORY_ROOT', str(repo))
    monkeypatch.setenv('EIMEMORY_DEPLOYMENT_CURRENT_LINK', str(link))
    monkeypatch.setenv('EIMEMORY_DEPLOYMENT_HEALTH_URL', 'http://127.0.0.1:1/health')
    def fake_git(repo, *args):
        if args[0] == 'rev-parse':
            return 'a' * 40
        return '[project]\nversion = "1.2.3"' if args[-1].endswith('pyproject.toml') else module
    monkeypatch.setattr(receipt, '_git', fake_git)
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid version must not reach health or persistence')
    monkeypatch.setattr(receipt, 'safe_urlopen', forbidden)
    monkeypatch.setattr(receipt, 'append_learning_record_once', forbidden)
    result = receipt.verify_and_record_deployment(
        SimpleNamespace(store=None), scope={}, repo_root=repo, current_link=link,
        health_url='http://127.0.0.1:1/health',
    )
    assert result == {'ok': False, 'error': 'repo_version_unavailable'}
    assert not link.exists()
