from eimemory.core.wiring_audit import unwired_public_functions


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
