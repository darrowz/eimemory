"""Dependency-free release impact classification for lineage and deployment."""

from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Any


DOMAINS = (
    "memory.recall",
    "memory.governance",
    "channel.delivery",
    "storage.integrity",
    "deployment.runtime",
    "code.evolution",
)

# ``eimemory/models`` contains shared persisted and wire contracts consumed by
# all six domains. A change there must invalidate every dependent domain rather
# than falling through as an unknown path or inheriting stale evidence.
_SHARED_MODEL_PATHS = ("eimemory/models",)

DOMAIN_PATHS: dict[str, tuple[str, ...]] = {
    "memory.recall": (
        "eimemory/api/memory.py",
        "eimemory/embeddings",
        "integrations/hermes/eimemory/__init__.py",
        "eimemory/recall",
        "eimemory/retrieval",
        "eimemory/scoring",
        "eimemory/storage/runtime_store.py",
        "eimemory/storage/sqlite_store.py",
        *_SHARED_MODEL_PATHS,
    ),
    "memory.governance": (
        "eimemory/api/runtime.py",
        "eimemory/evaluation",
        "eimemory/experience",
        "eimemory/governance",
        "integrations/hermes/eimemory_hook/__init__.py",
        *_SHARED_MODEL_PATHS,
    ),
    "channel.delivery": (
        "deploy/install_hermes_integration.py",
        "deploy/openclaw",
        "deploy/ensure_openclaw",
        "deploy/ensure_openclaw_bundled_bridge.py",
        "deploy/ensure_openclaw_bridge_config.py",
        "deploy/install_immutable_release.sh",
        "deploy/patch_openclaw",
        "deploy/systemd/openclaw-",
        "deploy/verify_openclaw",
        "deploy/verify_openclaw_plugin_runtime.py",
        "deploy/wait_openclaw_gateway_ready.py",
        "deploy/verify_hermes_integration.py",
        "eimemory/adapters/hermes/channel_delivery.py",
        "eimemory/adapters/openclaw",
        "eimemory/adapters/runtime",
        "eimemory/ei_bridge",
        "eimemory/ops/openclaw_loop.py",
        "eimemory/governance/external_channel_acceptance.py",
        "integrations/hermes/eimemory_hook",
        "integrations/openclaw",
        *_SHARED_MODEL_PATHS,
    ),
    "storage.integrity": (
        "deploy/migrate_storage_release.py",
        "deploy/install_immutable_release.sh",
        "deploy/storage",
        "deploy/storage_release_transaction.py",
        "deploy/systemd/eimemory-storage",
        "deploy/verify_storage_release.py",
        "eimemory/storage",
        *_SHARED_MODEL_PATHS,
    ),
    "deployment.runtime": (
        "deploy/bootstrap_production_recall.py",
        "deploy/capture_prior_health",
        "deploy/ensure_evidence_receipt",
        "deploy/install_hermes_integration.py",
        "deploy/install_immutable_release.sh",
        "deploy/install_managed_systemd_dropin.py",
        "deploy/record_deployment_receipt.py",
        "deploy/record_release_closure_incident.py",
        "deploy/release_impact.py",
        "deploy/summarize_release_closure.py",
        "deploy/record_release_lineage.py",
        "deploy/runtime_identity_policy.py",
        "deploy/systemd/eimemory-",
        "deploy/systemd/hermes-",
        "deploy/verify_hermes_integration.py",
        "deploy/verify_release_health.py",
        "eimemory/adapters/eibrain/rpc_server.py",
        "eimemory/governance/deployment_receipt.py",
        "eimemory/governance/release_impact.py",
        "eimemory/ops/runtime_identity_drift.py",
        "eimemory/runtime_identity.py",
        "integrations/hermes/eimemory/__init__.py",
        "integrations/hermes/eimemory_hook/__init__.py",
        *_SHARED_MODEL_PATHS,
    ),
    "code.evolution": (
        "eimemory/adapters/hermes/code_implementation.py",
        "eimemory/capabilities/code_implementation_bootstrap.py",
        "eimemory/capabilities/data/code_implementation.v2.json",
        "eimemory/evaluation/hongtu_code_implementation.py",
        "eimemory/governance/autonomous_evolution.py",
        "eimemory/governance/autonomous_learning.py",
        "eimemory/governance/code_automation_policy.py",
        "eimemory/governance/code_maintenance.py",
        "eimemory/governance/code_evolution_bridge.py",
        "eimemory/governance/code_evolution_effects.py",
        "eimemory/governance/code_evolution_observation.py",
        "eimemory/governance/code_evolution_repository.py",
        "eimemory/governance/code_evolution_test_plans.py",
        "eimemory/governance/code_evolution_transaction.py",
        "eimemory/governance/code_patch_command_policy.py",
        "eimemory/governance/deployment_receipt.py",
        "eimemory/governance/l5_product_completion.py",
        "eimemory/governance/l5_reader.py",
        "eimemory/governance/promotion_watch.py",
        "eimemory/governance/release_closure_gate_evidence.py",
        "eimemory/governance/release_closure_lineage.py",
        "eimemory/governance/release_pre_observation.py",
        "eimemory/governance/release_impact.py",
        "eimemory/governance/release_lineage.py",
        "eimemory/governance/system_code_repair.py",
        "eimemory/ops/release_closure_failure.py",
        "eimemory/ops/system_code_repair_failure.py",
        "eimemory/cli/main.py",
        "eimemory/scheduler/jobs.py",
        "eimemory/storage/code_evolution_store.py",
        "eimemory/storage/migrations/code_evolution_transactions.py",
        "deploy/code-automation-policy.v2.json.example",
        "deploy/governance.env.example",
        "deploy/install_hermes_integration.py",
        "deploy/install_immutable_release.sh",
        "deploy/record_release_closure_incident.py",
        "deploy/release_impact.py",
        "deploy/summarize_release_closure.py",
        "deploy/runtime_identity_policy.py",
        "deploy/systemd/eimemory-learn-watch.service",
        "deploy/systemd/eimemory-learn-watch.timer",
        "deploy/systemd/hermes-gateway-eimemory.conf",
        "deploy/verify_hermes_integration.py",
        "integrations/hermes/eimemory_hook/__init__.py",
        "integrations/hermes/eimemory_hook/plugin.yaml",
        *_SHARED_MODEL_PATHS,
    ),
}

IGNORED_PATH_PREFIXES = ("docs/", "tests/", ".github/")
IGNORED_PATHS = {
    "CHANGELOG.md",
    "deploy/systemd/README.md",
    "scripts/test_openclaw_loop.py",
}
INTEGRATION_VERSION_PATHS = {
    "integrations/codex/eimemory/.codex-plugin/plugin.json",
    "integrations/hermes/eimemory/plugin.yaml",
    "integrations/hermes/eimemory_hook/plugin.yaml",
}
COMMIT_RE = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)


class ReleaseImpactError(ValueError):
    """Raised when an exact release impact cannot be established."""


def release_impact(
    repo: Path,
    *,
    ancestor: str,
    current: str,
) -> dict[str, Any]:
    """Return a bounded JSON-safe classification for two exact commits."""

    repository = Path(repo)
    if not repository.is_dir():
        raise ReleaseImpactError("repository is not a directory")
    for label, commit in (("prior", ancestor), ("current", current)):
        if COMMIT_RE.fullmatch(commit) is None:
            raise ReleaseImpactError(f"{label} commit must be an exact 40-character SHA")
        if _git_bytes(repository, "cat-file", "-e", f"{commit}^{{commit}}") is None:
            raise ReleaseImpactError(f"{label} commit is unavailable")

    change = _release_change_summary(
        repository,
        ancestor=ancestor,
        current=current,
    )
    if change is None:
        raise ReleaseImpactError("changed paths could not be read")
    changed_paths, classified, unknown = change
    unknown_set = set(unknown)
    paths: list[dict[str, Any]] = []
    affected_domains: set[str] = set()
    for path in changed_paths:
        domains = sorted(classified[path])
        affected_domains.update(domains)
        if domains:
            classification = "classified"
        elif path in unknown_set:
            classification = "unknown_production"
        else:
            classification = "ignored"
        paths.append(
            {
                "path": path,
                "domains": domains,
                "classification": classification,
            }
        )
    if unknown:
        reason = "unknown_production_change"
    elif affected_domains:
        reason = "classified_production_change"
    else:
        reason = "lightweight_release"
    return {
        "requires_closure": bool(unknown or affected_domains),
        "reason": reason,
        "affected_domains": sorted(affected_domains),
        "unknown_production_paths": unknown,
        "paths": paths,
    }


def _changed_paths(repo: Path, ancestor: str, current: str) -> list[str] | None:
    raw = _git_bytes(repo, "diff", "--name-only", "-z", f"{ancestor}..{current}")
    if raw is None:
        return None
    return sorted(
        path.decode("utf-8", errors="surrogateescape")
        for path in raw.split(b"\0")
        if path
    )


def _release_change_summary(
    repo: Path,
    *,
    ancestor: str,
    current: str,
) -> tuple[list[str], dict[str, set[str]], list[str]] | None:
    changed_paths = _changed_paths(repo, ancestor, current)
    if changed_paths is None:
        return None
    classified = {
        path: _domains_for_change(
            repo,
            path=path,
            ancestor=ancestor,
            current=current,
        )
        for path in changed_paths
    }
    unknown = sorted(
        path
        for path, domains in classified.items()
        if not domains
        and not _ignored_change(
            repo,
            path=path,
            ancestor=ancestor,
            current=current,
        )
    )
    return changed_paths, classified, unknown


def _domains_for_change(
    repo: Path,
    *,
    path: str,
    ancestor: str,
    current: str,
) -> set[str]:
    if path in {"pyproject.toml", "eimemory/version.py"}:
        return set() if _version_metadata_only_change(
            repo,
            path=path,
            ancestor=ancestor,
            current=current,
        ) else set(DOMAINS)
    if path in INTEGRATION_VERSION_PATHS and _integration_version_only_change(
        repo,
        path=path,
        ancestor=ancestor,
        current=current,
    ):
        return set()
    return {
        domain
        for domain, rules in DOMAIN_PATHS.items()
        if any(_path_matches_rule(path, rule) for rule in rules)
    }


def _path_matches_rule(path: str, rule: str) -> bool:
    return bool(
        path == rule
        or path.startswith(rule.rstrip("/") + "/")
        or (rule.endswith(("-", "_")) and path.startswith(rule))
    )


def _ignored_change(
    repo: Path,
    *,
    path: str,
    ancestor: str,
    current: str,
) -> bool:
    return (
        path in IGNORED_PATHS
        or path.startswith(IGNORED_PATH_PREFIXES)
        or path.startswith("README")
        or path.startswith("CHANGELOG")
        or (
            path in {"pyproject.toml", "eimemory/version.py"}
            and _version_metadata_only_change(
                repo,
                path=path,
                ancestor=ancestor,
                current=current,
            )
        )
        or (
            path in INTEGRATION_VERSION_PATHS
            and _integration_version_only_change(
                repo,
                path=path,
                ancestor=ancestor,
                current=current,
            )
        )
    )


def _version_metadata_only_change(
    repo: Path,
    *,
    path: str,
    ancestor: str,
    current: str,
) -> bool:
    before = _git_bytes(repo, "show", f"{ancestor}:{path}")
    after = _git_bytes(repo, "show", f"{current}:{path}")
    if before is None or after is None:
        return False
    try:
        if path == "pyproject.toml":
            before_payload = deepcopy(tomllib.loads(before.decode("utf-8")))
            after_payload = deepcopy(tomllib.loads(after.decode("utf-8")))
            for payload in (before_payload, after_payload):
                project = payload.get("project")
                if isinstance(project, dict):
                    project.pop("version", None)
            return before_payload == after_payload
        return _normalized_version_module(before) == _normalized_version_module(after)
    except (SyntaxError, UnicodeError, ValueError, TypeError):
        return False


def _integration_version_only_change(
    repo: Path,
    *,
    path: str,
    ancestor: str,
    current: str,
) -> bool:
    before = _git_bytes(repo, "show", f"{ancestor}:{path}")
    after = _git_bytes(repo, "show", f"{current}:{path}")
    if before is None or after is None:
        return False
    try:
        if path.endswith(".json"):
            before_payload = json.loads(before.decode("utf-8"))
            after_payload = json.loads(after.decode("utf-8"))
            if not isinstance(before_payload, dict) or not isinstance(after_payload, dict):
                return False
            before_payload.pop("version", None)
            after_payload.pop("version", None)
            return before_payload == after_payload
        version_line = re.compile(r"^version\s*:")

        def normalized_lines(raw: bytes) -> tuple[str, ...]:
            return tuple(
                line.rstrip()
                for line in raw.decode("utf-8").splitlines()
                if version_line.match(line) is None
            )

        return normalized_lines(before) == normalized_lines(after)
    except (UnicodeError, ValueError, TypeError):
        return False


def _normalized_version_module(raw: bytes) -> str:
    tree = ast.parse(raw.decode("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in targets
            ):
                node.value = ast.Constant(value="<release-version>")
    return ast.dump(tree, include_attributes=False)


def _git_bytes(repo: Path, *args: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None
