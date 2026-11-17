"""Offline contract between release-gate environment projection and the real consumer."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from deploy import run_with_governance_env as gate
from eimemory.retrieval.relevance import RelevanceConfig


PREFIX = "EIMEMORY_RERANKER_"
PROFILE = {
    PREFIX + "ENABLED": "1", PREFIX + "URL": "http://127.0.0.1:18919",
    PREFIX + "API_KEY": "synthetic-reranker-key", PREFIX + "MODEL": "fixture/reranker",
    PREFIX + "REVISION": "a" * 40, PREFIX + "MAX_CANDIDATES": "9",
    PREFIX + "TEXT_CHARS": "1400", PREFIX + "TIMEOUT_SECONDS": "1.5",
    PREFIX + "MIN_SCORE": "-0.25", PREFIX + "CALIBRATION": "fixture-calibration-v2",
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    attempts = []

    def denied(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("configuration tests must not use a network or subprocess")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    yield
    assert not attempts


def write_env(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    path.chmod(0o600)


def load(config, root, base=None):
    return gate.load_runtime_recall_environment(config, root, base_environment=base or {})


def test_release_gate_profile_constructs_the_same_real_reranker_config(tmp_path):
    config, root = tmp_path / "config", tmp_path / "runtime"
    write_env(config / "reranker.env", PROFILE)
    direct = RelevanceConfig.from_env(PROFILE)
    projected = RelevanceConfig.from_env(load(config, root))
    assert projected == direct
    assert projected.enabled and projected.endpoint == "http://127.0.0.1:18919"
    assert projected.identity() == direct.identity()


@pytest.mark.parametrize("suffix,value,attribute,expected", [
    ("URL", "http://127.0.0.1:18920", "endpoint", "http://127.0.0.1:18920"),
    ("REVISION", "b" * 40, "revision", "b" * 40),
    ("TEXT_CHARS", "1700", "max_text_chars", 1700),
    ("MIN_SCORE", "0.75", "min_score", 0.75),
    ("CALIBRATION", "fixture-new-calibration", "calibration", "fixture-new-calibration"),
])
def test_consumer_field_overrides_inherited_value(tmp_path, suffix, value, attribute, expected):
    config, root = tmp_path / "config", tmp_path / "runtime"
    write_env(config / "reranker.env", {PREFIX + suffix: value})
    actual = RelevanceConfig.from_env(load(config, root, PROFILE))
    assert actual == replace(RelevanceConfig.from_env(PROFILE), **{attribute: expected})


@pytest.mark.parametrize("last_profile", range(6))
def test_runtime_profile_precedence_preserves_consumer_values(tmp_path, last_profile):
    config, root = tmp_path / "config", tmp_path / "runtime"
    paths = [config / "postgres.env", config / "embedding.env", config / "reranker.env",
             root / "state/vector-serving.env", root / "state/caller-recall.env", config / "recall.env"]
    expected = dict(PROFILE)
    for index, path in enumerate(paths[:last_profile + 1], 1):
        expected = {**PROFILE, PREFIX + "REVISION": format(index, "x") * 40,
                    PREFIX + "URL": f"http://127.0.0.1:{18920 + index}",
                    PREFIX + "TEXT_CHARS": str(1000 + index), PREFIX + "MIN_SCORE": str(index / 10),
                    PREFIX + "CALIBRATION": f"fixture-calibration-{index}"}
        write_env(path, expected)
    assert RelevanceConfig.from_env(load(config, root, PROFILE)) == RelevanceConfig.from_env(expected)


def test_legacy_compatibility_fields_stay_allowed_and_unrelated_routes_stay_filtered(tmp_path):
    config, root = tmp_path / "config", tmp_path / "runtime"
    compatible = {PREFIX + key: value for key, value in {
        "PROVIDER": "fixture-provider", "BASE_URL": "http://127.0.0.1:18921",
        "MAX_TEXT_CHARS": "1900", "FAILURE_THRESHOLD": "3", "COOLDOWN_SECONDS": "20",
    }.items()}
    rejected = {PREFIX + "UNREVIEWED_OPTION": "no", "EIMEMORY_RPC_AUTH_TOKEN": "not-a-real-token",
                "EIMEMORY_RPC_URL": "http://127.0.0.1:19999", "EIMEMORY_LLM_COMMAND": "no-exec",
                "EIMEMORY_ROOT": "/not-authorized", "PATH": "/not-authorized"}
    write_env(config / "reranker.env", {**compatible, **rejected})
    actual = load(config, root, {"PATH": "trusted-parent-path"})
    assert actual == {**compatible, "PATH": "trusted-parent-path"}


@pytest.mark.parametrize("missing", ["API_KEY", "REVISION"])
def test_incomplete_enabled_configuration_is_still_rejected(tmp_path, missing):
    config, root = tmp_path / "config", tmp_path / "runtime"
    values = {key: value for key, value in PROFILE.items() if key != PREFIX + missing}
    write_env(config / "reranker.env", values)
    with pytest.raises(ValueError, match="reranker_identity_or_auth_missing"):
        RelevanceConfig.from_env(load(config, root))


def test_empty_revision_revokes_an_inherited_identity(tmp_path):
    config, root = tmp_path / "config", tmp_path / "runtime"
    write_env(config / "recall.env", {PREFIX + "REVISION": ""})
    with pytest.raises(ValueError, match="reranker_identity_or_auth_missing"):
        RelevanceConfig.from_env(load(config, root, PROFILE))


def test_imported_non_loopback_endpoint_is_not_silently_replaced_by_default(tmp_path):
    config, root = tmp_path / "config", tmp_path / "runtime"
    write_env(config / "reranker.env", {PREFIX + "URL": "https://example.invalid"})
    with pytest.raises(ValueError, match="reranker_requires_literal_loopback_endpoint"):
        RelevanceConfig.from_env(load(config, root, PROFILE))


def test_absent_optional_profiles_keep_reranker_disabled(tmp_path):
    actual = RelevanceConfig.from_env(load(tmp_path / "config", tmp_path / "runtime"))
    assert actual == RelevanceConfig() and not actual.enabled


def test_real_wrapper_hands_complete_configuration_to_its_exec_boundary(tmp_path, monkeypatch):
    config, root = tmp_path / "config", tmp_path / "runtime"
    write_env(config / "reranker.env", PROFILE)
    for key in tuple(os.environ):
        if key.startswith(PREFIX):
            monkeypatch.delenv(key)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    observed = []

    class ExecBoundaryReached(Exception):
        pass

    def intercept(executable, args, environment):
        observed.append(RelevanceConfig.from_env(environment))
        raise ExecBoundaryReached

    monkeypatch.setattr(os, "execvpe", intercept)
    with pytest.raises(ExecBoundaryReached):
        gate.main(["--env-file", str(config / "absent-governance.env"), "--optional",
                   "--runtime-recall-config-dir", str(config), "--runtime-recall-root", str(root),
                   "--", "fixture-closure-entrypoint"])
    assert observed == [RelevanceConfig.from_env(PROFILE)]
