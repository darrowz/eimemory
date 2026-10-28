import pytest

from eimemory.core.wiring_audit import _SKIP_DIRS, unwired_public_functions


@pytest.mark.parametrize('directory', sorted(_SKIP_DIRS))
def test_sources_and_root_share_directory_component_exclusions(tmp_path, directory):
    sources = {
        'eimemory/api.py': 'def lonely():\n    return 1\n',
        f'nested/{directory}/caller.py': 'lonely()\n',
    }
    for relative, content in sources.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    expected = {'eimemory/api.py:lonely'}
    assert unwired_public_functions(tmp_path) == expected
    assert unwired_public_functions(tmp_path, sources=sources) == expected
    sources = {'eimemory/api.py': sources['eimemory/api.py'],
               f'nested/{directory}-caller.py': 'lonely()\n'}
    assert unwired_public_functions(tmp_path, sources=sources) == set()


def test_other_worktrees_cannot_satisfy_current_checkout_callers(tmp_path):
    current = tmp_path / 'eimemory'
    current.mkdir()
    (current / 'example.py').write_text('def needs_real_caller():\n    return 1\n')
    sibling = tmp_path / '.worktrees' / 'older' / 'eimemory'
    sibling.mkdir(parents=True)
    (sibling / 'old.py').write_text('from example import needs_real_caller\nneeds_real_caller()\n')
    assert 'eimemory/example.py:needs_real_caller' in unwired_public_functions(tmp_path)


def test_wiring_audit_change_requires_code_evolution_evidence(tmp_path):
    from eimemory.governance.release_impact import _domains_for_change
    assert _domains_for_change(tmp_path, path='eimemory/core/wiring_audit.py',
                               ancestor='a' * 40, current='b' * 40) == {'code.evolution'}


def test_in_memory_proposal_uses_all_replacements_without_writing(tmp_path):
    sources = {
        'eimemory/api.py': 'def existing():\n    return 1\n',
        'eimemory/caller.py': 'from .api import existing\nexisting()\n',
    }
    assert unwired_public_functions(tmp_path, sources=sources) == set()
    sources['eimemory/api.py'] += '\ndef candidate_only():\n    return candidate_only()\n'
    assert unwired_public_functions(tmp_path, sources=sources) == {'eimemory/api.py:candidate_only'}
    sources['eimemory/caller.py'] += '\nfrom .api import candidate_only\ncandidate_only()\n'
    assert unwired_public_functions(tmp_path, sources=sources) == set()
    sources['eimemory/caller.py'] = ''
    assert unwired_public_functions(tmp_path, sources=sources) == {
        'eimemory/api.py:existing', 'eimemory/api.py:candidate_only'}
    assert list(tmp_path.iterdir()) == []
