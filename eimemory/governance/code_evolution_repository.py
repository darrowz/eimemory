"""Canonical read-only repository attestations for code evolution."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from hashlib import sha256
import json
from pathlib import Path


def protected_paths_digest(root: str | Path, paths: Sequence[str]) -> str:
    repository = Path(root)
    return _entries_digest(
        paths,
        reader=lambda relative: (repository / relative).read_bytes(),
    )


def protected_paths_digest_at_commit(
    root: str | Path,
    commit: str,
    paths: Sequence[str],
    *,
    git_blob_reader: Callable[[Path, str, str], bytes],
) -> str:
    repository = Path(root)
    return _entries_digest(
        paths,
        reader=lambda relative: git_blob_reader(repository, str(commit), relative),
    )


def remote_url_digest(remote_url: str) -> str:
    return sha256(str(remote_url).strip().encode("utf-8")).hexdigest()


def _entries_digest(paths: Sequence[str], *, reader: Callable[[str], bytes]) -> str:
    entries = [
        {
            "path": relative,
            "sha256": sha256(reader(relative).replace(b"\r\n", b"\n")).hexdigest(),
        }
        for relative in sorted(str(item) for item in paths)
    ]
    return sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


__all__ = ["protected_paths_digest", "protected_paths_digest_at_commit", "remote_url_digest"]
