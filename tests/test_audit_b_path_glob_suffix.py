"""Whole-segment authority checks for operator-selected evolution path globs."""
import pytest
from eimemory.governance.evolution.code_evolution_path_policy import (
    match_path_glob, path_allowed_for_evolution,
)

@pytest.mark.parametrize('path,pattern,expected', [
    ('eimemory/ops/x.py.sh', 'eimemory/ops/*.py', False),
    ('eimemory/ops/target.py.backup', 'eimemory/ops/*target.py', False),
    ('eimemory/ops/x.py', 'eimemory/ops/*.py', True),
    ('eimemory/ops/a.py.py', 'eimemory/ops/*.py', True),
    ('eimemory/ops/aaab', 'eimemory/ops/*a*b', True),
    ('eimemory/ops/aaabc', 'eimemory/ops/*a*b', False),
    ('eimemory/ops/ab', 'eimemory/ops/a*ab', False),
    ('eimemory/ops/aab', 'eimemory/ops/a*ab', True),
    ('eimemory/ops/axbxc', 'eimemory/ops/a*b*c*', True),
    ('eimemory/ops/nested/x.py', 'eimemory/ops/*.py', False),
    ('eimemory/ops/nested/x.py', 'eimemory/ops/**/*.py', True),
])
def test_path_glob_is_whole_segment(path, pattern, expected):
    assert match_path_glob(path, pattern) is expected


def test_suffix_cannot_expand_path_authority():
    assert path_allowed_for_evolution(
        'eimemory/ops/x.py.sh', allowed_path_globs=['eimemory/ops/*.py'],
    ) == (False, 'path_not_allowed')


def test_deny_suffix_does_not_deny_unmatched_extension():
    assert path_allowed_for_evolution(
        'eimemory/ops/not_secret.key.txt',
        allowed_path_globs=['eimemory/ops/**'],
        denied_path_globs=['**/*.key'],
    ) == (True, '')
