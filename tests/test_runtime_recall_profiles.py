import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from deploy.run_with_governance_env import (
    GovernanceEnvironmentError, load_runtime_recall_environment,
)


def profile(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def test_order_matches_live_rpc_and_nightly_without_importing_other_authority(tmp_path):
    config, root = tmp_path / "config", tmp_path / "data"
    profile(config / "postgres.env", "EIMEMORY_POSTGRES_VECTOR_ENABLED=0\n"
            "EIMEMORY_POSTGRES_VECTOR_DSN='postgresql://private-target/db'\n")
    profile(config / "embedding.env", "EIMEMORY_EMBEDDINGS_MODEL=production-model\n"
            "EIMEMORY_RPC_AUTH_TOKEN=must-not-import\n"
            "EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE=/untrusted/pin\n")
    profile(config / "reranker.env", "EIMEMORY_RERANKER_ENABLED=1\n")
    profile(root / "state/vector-serving.env", "EIMEMORY_POSTGRES_VECTOR_ENABLED=1\n")
    profile(root / "state/caller-recall.env", "EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED=1\n")
    profile(config / "recall.env", "EIMEMORY_RECALL_BUDGET_SECONDS=8\n"
            "EIMEMORY_CAPTURE_ORIGINAL_QUERY=1\nEIMEMORY_LLM_COMMAND=must-not-override-review\n")
    result = load_runtime_recall_environment(config, root, base_environment={
        "PATH": "trusted", "EIMEMORY_LLM_COMMAND": "trusted-review",
        "EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE": "/trusted/pin"})
    assert result["EIMEMORY_POSTGRES_VECTOR_ENABLED"] == "1"
    assert result["EIMEMORY_EMBEDDINGS_MODEL"] == "production-model"
    assert result["EIMEMORY_RERANKER_ENABLED"] == "1"
    assert result["EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED"] == "1"
    assert result["EIMEMORY_CAPTURE_ORIGINAL_QUERY"] == "1"
    assert result["EIMEMORY_RECALL_BUDGET_SECONDS"] == "8"
    assert result["EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE"] == "/trusted/pin"
    assert result["EIMEMORY_LLM_COMMAND"] == "trusted-review"
    assert "EIMEMORY_RPC_AUTH_TOKEN" not in result


def test_missing_optional_profiles_preserve_environment(tmp_path):
    assert load_runtime_recall_environment(tmp_path / "config", tmp_path / "data",
        base_environment={"PATH": "trusted"}) == {"PATH": "trusted"}


@pytest.mark.parametrize("content", [
    "EIMEMORY_POSTGRES_VECTOR_ENABLED=1\nEIMEMORY_POSTGRES_VECTOR_ENABLED=0\n",
    "EIMEMORY_EMBEDDINGS_MODEL='unterminated\n", "not an assignment\n",
])
def test_present_invalid_profile_fails_instead_of_silently_selecting_sqlite(tmp_path, content):
    profile(tmp_path / "config/postgres.env", content)
    with pytest.raises(GovernanceEnvironmentError):
        load_runtime_recall_environment(tmp_path / "config", tmp_path / "data", base_environment={})


def test_symlinked_profile_is_rejected(tmp_path):
    profile(tmp_path / "target.env", "EIMEMORY_POSTGRES_VECTOR_ENABLED=1\n")
    (tmp_path / "config").mkdir()
    try:
        (tmp_path / "config/postgres.env").symlink_to(tmp_path / "target.env")
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(GovernanceEnvironmentError):
        load_runtime_recall_environment(tmp_path / "config", tmp_path / "data", base_environment={})


@pytest.mark.skipif(os.name != "posix", reason="native deployment exec boundary requires POSIX")
def test_real_wrapper_executes_child_with_live_profile_order(tmp_path):
    config, root = tmp_path / "config", tmp_path / "data"
    profile(config / "postgres.env", "EIMEMORY_POSTGRES_VECTOR_ENABLED=0\n")
    profile(root / "state/vector-serving.env", "EIMEMORY_POSTGRES_VECTOR_ENABLED=1\n")
    profile(config / "recall.env", "EIMEMORY_RECALL_BUDGET_SECONDS=8\n")
    wrapper = Path(__file__).resolve().parents[1] / "deploy/run_with_governance_env.py"
    program = "import json,os;print(json.dumps({k:os.getenv(k) for k in " \
              "['EIMEMORY_POSTGRES_VECTOR_ENABLED','EIMEMORY_RECALL_BUDGET_SECONDS']}))"
    result = subprocess.run([sys.executable, "-I", "-B", str(wrapper), "--env-file",
        str(config / "governance.env"), "--optional", "--runtime-recall-config-dir", str(config),
        "--runtime-recall-root", str(root), "--", sys.executable, "-I", "-B", "-c", program],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"EIMEMORY_POSTGRES_VECTOR_ENABLED": "1",
                                      "EIMEMORY_RECALL_BUDGET_SECONDS": "8"}


def test_relative_profile_roots_rejected(tmp_path):
    with pytest.raises(GovernanceEnvironmentError):
        load_runtime_recall_environment("relative", tmp_path, base_environment={})
