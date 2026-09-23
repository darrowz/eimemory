from __future__ import annotations

import pytest

from eimemory.governance.code_evolution_semantic_validation import (
    code_evolution_proposal_semantic_error,
    python_execution_authority_error,
)


BENIGN = '''
def helper(value: str) -> str:
    return value.strip()
'''


@pytest.mark.parametrize(
    "snippet,reason_prefix",
    [
        ("import importlib\n", "execution_authority_banned_import:importlib"),
        ("import ctypes\n", "execution_authority_banned_import:ctypes"),
        ("import socket\n", "execution_authority_banned_import:socket"),
        ("from urllib import request\n", "execution_authority_banned_import:urllib"),
        ("import http.client\n", "execution_authority_banned_import:http"),
        ("import requests\n", "execution_authority_banned_import:requests"),
        ("import os\nos.execv('/bin/true', ['true'])\n", "execution_authority_banned_call:os.execv"),
        ("import os\nos.spawnv(0, '/bin/true', ['true'])\n", "execution_authority_banned_call:os.spawnv"),
        ("import os\nos.fork()\n", "execution_authority_banned_call:os.fork"),
        ("import os\ngetattr(os, 'system')('true')\n", "execution_authority_banned_getattr:os.system"),
        ("from importlib import import_module\nimport_module('os')\n", "execution_authority_banned_call:importlib.import_module"),
    ],
)
def test_banned_patterns_fail_with_stable_reason(snippet: str, reason_prefix: str) -> None:
    assert python_execution_authority_error(snippet).startswith(reason_prefix)
    error = code_evolution_proposal_semantic_error(
        {"incident_class": "deployment.runtime_commit_drift"},
        [{"path": "eimemory/governance/l5_reader.py", "content": snippet}],
    )
    assert error.startswith(reason_prefix)


def test_benign_patch_passes_all_incidents() -> None:
    assert python_execution_authority_error(BENIGN) == ""
    assert code_evolution_proposal_semantic_error(
        {"incident_class": "deployment.runtime_commit_drift"},
        [{"path": "deploy/runtime_identity_policy.py", "content": BENIGN}],
    ) == ""


def test_deny_self_paths_rejected() -> None:
    assert code_evolution_proposal_semantic_error(
        {"incident_class": "deployment.runtime_commit_drift"},
        [{"path": "eimemory/governance/code_evolution_effects.py", "content": BENIGN}],
    ) == "code_evolution_deny_self_path"
