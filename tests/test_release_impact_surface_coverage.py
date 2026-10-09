"""Every tracked production file must have a release-lineage domain.

1.14.46 shipped 33 tracked production files without a domain owner.  The
post-deploy closure therefore reported ``unknown_production_paths``; the
code-evolution auto-authorization refused to mint (correctly), and the
lineage stayed ``release_lineage_not_compatible``.  This guard keeps the
tracked tree fully classified while new, unregistered files still fail closed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from eimemory.governance.release.release_impact import (
    DOMAIN_PATHS,
    IGNORED_PATH_PREFIXES,
    IGNORED_PATHS,
    INTEGRATION_VERSION_PATHS,
    _path_matches_rule,
)

ROOT = Path(__file__).resolve().parents[1]
_SPECIAL = {"pyproject.toml", "eimemory/version.py", *INTEGRATION_VERSION_PATHS}


def _tracked() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            check=True, capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git checkout unavailable")
    return [p.decode() for p in out.split(b"\0") if p]


def _ignored(path: str) -> bool:
    return (
        path in IGNORED_PATHS
        or path.startswith(IGNORED_PATH_PREFIXES)
        or path.startswith(("README", "CHANGELOG", "FAQ", "CONTRIBUTING"))
        or path == "LICENSE"
    )


def _domains(path: str) -> set[str]:
    return {d for d, rules in DOMAIN_PATHS.items() if any(_path_matches_rule(path, r) for r in rules)}


def test_every_tracked_production_file_has_a_domain() -> None:
    missing = [
        p for p in _tracked()
        if p not in _SPECIAL and not _ignored(p) and not _domains(p)
    ]
    assert missing == []


@pytest.mark.parametrize(
    ("path", "required"),
    [
        ("deploy/capture_prior_health_snapshot.py", {"deployment.runtime"}),
        ("deploy/eimemory-deploy-worker", {"deployment.runtime"}),
        ("deploy/prepare_qwen_reranker.py", {"memory.recall", "deployment.runtime"}),
        ("eimemory/adapters/hermes/native_memory.py", {"memory.recall", "channel.delivery"}),
        ("eimemory/adapters/codex/mcp_server.py", {"channel.delivery"}),
        ("eimemory/capabilities/seed_manifest.py", {"memory.governance", "code.evolution"}),
        ("eimemory/config/loader.py", {"memory.recall", "deployment.runtime"}),
        ("eimemory/knowledge/ingest.py", {"memory.recall", "memory.governance"}),
        ("eimemory/persona/context_router.py", {"memory.recall"}),
        ("eimemory/raw/synthetic.py", {"memory.recall"}),
        ("integrations/hermes/host-patches/memory-sync-snapshot.patch", {"channel.delivery", "deployment.runtime"}),
    ],
)
def test_1_14_46_unknown_paths_now_have_owners(path: str, required: set[str]) -> None:
    assert required <= _domains(path)


@pytest.mark.parametrize(
    "path",
    ["deploy/brand_new_helper.py", "eimemory/knowledge/brand_new.py", "scripts/brand_new.py"],
)
def test_new_unregistered_files_still_fail_closed(path: str) -> None:
    assert _domains(path) == set()
