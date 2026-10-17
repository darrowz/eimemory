from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from deploy.run_with_governance_env import (
    ALLOWED_KEYS,
    GovernanceEnvironmentError,
    RUNTIME_AUTHORITY_KEY,
    load_runtime_authority_environment,
)


def _file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "rpc.env"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_runtime_authority_only_imports_pin_without_expansion(tmp_path):
    pin = '/private/operator pin.json'
    path = _file(tmp_path, f"{RUNTIME_AUTHORITY_KEY}='{pin}'\nRPC_TOKEN='private'\n"
                 "EIMEMORY_LLM_COMMAND='wrong-route'\nPYTHONPATH='/untrusted'\n")
    environment = {"PATH": "trusted", "EIMEMORY_LLM_COMMAND": "governed"}
    assert load_runtime_authority_environment(path, base_environment=environment) == {
        **environment, RUNTIME_AUTHORITY_KEY: pin,
    }
    assert RUNTIME_AUTHORITY_KEY not in ALLOWED_KEYS
    assert environment == {"PATH": "trusted", "EIMEMORY_LLM_COMMAND": "governed"}


@pytest.mark.parametrize("inherited,configured,expected", [
    ("", "/private/pin.json", "/private/pin.json"),
    ("/private/pin.json", "/private/pin.json", "/private/pin.json"),
    ("/private/pin.json", "", "/private/pin.json"),
    ("", "", ""),
])
def test_runtime_authority_preserves_unambiguous_pin(tmp_path, inherited, configured, expected):
    path = _file(tmp_path, f"{RUNTIME_AUTHORITY_KEY}='{configured}'\n")
    loaded = load_runtime_authority_environment(path, base_environment={RUNTIME_AUTHORITY_KEY: inherited})
    assert loaded[RUNTIME_AUTHORITY_KEY] == expected


@pytest.mark.parametrize("content,inherited", [
    (f"{RUNTIME_AUTHORITY_KEY}=/private/other.json\n", "/private/pin.json"),
    (f"{RUNTIME_AUTHORITY_KEY}=one\n{RUNTIME_AUTHORITY_KEY}=two\n", ""),
    (f"{RUNTIME_AUTHORITY_KEY}=one\nRPC_TOKEN=a\nRPC_TOKEN=b\n", ""),
    (f"{RUNTIME_AUTHORITY_KEY}='unterminated\n", ""),
    (f"{RUNTIME_AUTHORITY_KEY}=one two\n", ""),
    ("not an assignment\n", ""),
    (f"{RUNTIME_AUTHORITY_KEY}=one\x00\n", ""),
    (f"{RUNTIME_AUTHORITY_KEY}=one\n#" + "x" * 65536, ""),
], ids=["conflict", "duplicate-pin", "duplicate-unrelated", "bad-quote", "multiple-values",
        "not-assignment", "nul", "oversize"])
def test_runtime_authority_rejects_unsafe_or_ambiguous_input(tmp_path, content, inherited):
    path = _file(tmp_path, content)
    with pytest.raises(GovernanceEnvironmentError):
        load_runtime_authority_environment(path, base_environment={RUNTIME_AUTHORITY_KEY: inherited})


def test_runtime_authority_missing_file_is_rejected_even_with_inherited_pin(tmp_path):
    with pytest.raises(GovernanceEnvironmentError):
        load_runtime_authority_environment(tmp_path / "missing.env", base_environment={RUNTIME_AUTHORITY_KEY: "pin"})


def test_runtime_authority_optional_missing_file_preserves_base(tmp_path):
    base = {RUNTIME_AUTHORITY_KEY: "existing-pin", "PATH": "trusted"}
    assert load_runtime_authority_environment(
        tmp_path / "missing.env", base_environment=base, optional=True) == base


def test_runtime_authority_optional_broken_symlink_is_rejected(tmp_path):
    link = tmp_path / "broken.env"
    try:
        link.symlink_to(tmp_path / "missing.env")
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(GovernanceEnvironmentError):
        load_runtime_authority_environment(link, base_environment={}, optional=True)


def test_runtime_authority_rejects_symlink(tmp_path):
    target = _file(tmp_path, f"{RUNTIME_AUTHORITY_KEY}=pin\n")
    link = tmp_path / "link.env"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(GovernanceEnvironmentError):
        load_runtime_authority_environment(link, base_environment={})


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permission boundary")
def test_runtime_authority_rejects_group_writable_file(tmp_path):
    path = _file(tmp_path, f"{RUNTIME_AUTHORITY_KEY}=pin\n")
    path.chmod(0o660)
    with pytest.raises(GovernanceEnvironmentError):
        load_runtime_authority_environment(path, base_environment={})


@pytest.mark.skipif(os.name != "posix", reason="POSIX production exec boundary")
def test_wrapper_passes_only_runtime_pin_to_real_child(tmp_path):
    path = _file(tmp_path, f"{RUNTIME_AUTHORITY_KEY}='/private/operator pin.json'\n"
                 "RPC_PRIVATE_SENTINEL=private\nEIMEMORY_LLM_COMMAND=wrong-route\n")
    environment = dict(os.environ)
    for key in (RUNTIME_AUTHORITY_KEY, "RPC_PRIVATE_SENTINEL", "EIMEMORY_LLM_COMMAND"):
        environment.pop(key, None)
    script = "import json,os;print(json.dumps({k:os.environ.get(k) for k in " + repr([
        RUNTIME_AUTHORITY_KEY, "RPC_PRIVATE_SENTINEL", "EIMEMORY_LLM_COMMAND",
    ]) + "}))"
    wrapper = Path(__file__).resolve().parents[1] / "deploy/run_with_governance_env.py"
    result = subprocess.run([
        sys.executable, "-I", "-B", str(wrapper), "--env-file", str(tmp_path / "missing-governance.env"),
        "--optional", "--runtime-authority-env-file", str(path), "--", sys.executable, "-c", script,
    ], env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        RUNTIME_AUTHORITY_KEY: "/private/operator pin.json", "RPC_PRIVATE_SENTINEL": None,
        "EIMEMORY_LLM_COMMAND": None,
    }


def test_wrapper_conflict_exits_without_child_or_private_values(tmp_path):
    path = _file(tmp_path, f"{RUNTIME_AUTHORITY_KEY}=private-file-pin\n")
    marker = tmp_path / "child-executed"
    wrapper = Path(__file__).resolve().parents[1] / "deploy/run_with_governance_env.py"
    result = subprocess.run([
        sys.executable, "-I", "-B", str(wrapper), "--env-file", str(tmp_path / "missing.env"),
        "--optional", "--runtime-authority-env-file", str(path), "--", sys.executable, "-c",
        "from pathlib import Path;Path(" + repr(str(marker)) + ").touch()",
    ], env={**os.environ, RUNTIME_AUTHORITY_KEY: "private-inherited-pin"},
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert "conflicts" in result.stderr
    assert "private-file-pin" not in result.stderr and "private-inherited-pin" not in result.stderr
    assert not marker.exists()
