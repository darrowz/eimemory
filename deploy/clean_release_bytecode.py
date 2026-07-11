#!/usr/bin/env python3
"""Remove source-tree bytecode from an immutable release without following links."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Sequence


COMMIT_DIRECTORY_PATTERN = re.compile(r"[0-9a-fA-F]{40}")
STAGE_DIRECTORY_PATTERN = re.compile(r"\.eimemory-stage-[0-9a-fA-F]{40}-[A-Za-z0-9]+")
BACKUP_DIRECTORY_PATTERN = re.compile(r"\.eimemory-backup-[0-9a-fA-F]{40}-[A-Za-z0-9]+")


class CleanupError(RuntimeError):
    """Raised when release cleanup cannot be proven safe and complete."""


def resolve_release_paths(
    release_dir: str | Path,
    releases_root: str | Path,
    *,
    allow_stage: bool = False,
) -> tuple[Path, Path]:
    release = Path(release_dir)
    root = Path(releases_root)
    if not str(release) or not str(root):
        raise CleanupError("unsafe release directory: empty path")
    if _is_link_like(root) or _is_link_like(release):
        raise CleanupError("unsafe release directory: symlink root or release")
    try:
        resolved_root = root.resolve(strict=True)
        resolved_release = release.resolve(strict=True)
    except OSError as exc:
        raise CleanupError(f"unsafe release directory: {exc}") from exc
    if not resolved_root.is_dir() or not resolved_release.is_dir():
        raise CleanupError("unsafe release directory: root and release must be directories")
    if resolved_release.parent != resolved_root:
        raise CleanupError("unsafe release directory: release must be a direct child of releases root")
    valid_commit = COMMIT_DIRECTORY_PATTERN.fullmatch(resolved_release.name) is not None
    valid_stage = allow_stage and (
        STAGE_DIRECTORY_PATTERN.fullmatch(resolved_release.name) is not None
        or BACKUP_DIRECTORY_PATTERN.fullmatch(resolved_release.name) is not None
    )
    if not valid_commit and not valid_stage:
        raise CleanupError("unsafe release directory: release name must be a 40-character commit")
    return resolved_release, resolved_root


def _is_link_like(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())
    except OSError:
        return True


def _resolve_release_target(release_dir: str | Path, releases_root: str | Path) -> tuple[Path, Path]:
    release = Path(release_dir)
    root = Path(releases_root)
    if _is_link_like(root) or _is_link_like(release):
        raise CleanupError("unsafe release directory: symlink root or release")
    try:
        resolved_root = root.resolve(strict=True)
        resolved_parent = release.parent.resolve(strict=True)
    except OSError as exc:
        raise CleanupError(f"unsafe release directory: {exc}") from exc
    if not resolved_root.is_dir() or resolved_parent != resolved_root:
        raise CleanupError("unsafe release directory: release must be a direct child of releases root")
    if COMMIT_DIRECTORY_PATTERN.fullmatch(release.name) is None:
        raise CleanupError("unsafe release directory: release name must be a 40-character commit")
    return resolved_root / release.name, resolved_root


def _directory_open_flags() -> int:
    required = (os.open, os.stat, os.unlink, os.rmdir)
    if any(operation not in os.supports_dir_fd for operation in required) or os.listdir not in os.supports_fd:
        raise CleanupError("safe dir_fd bytecode cleanup is unsupported on this platform")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise CleanupError("safe no-follow bytecode cleanup is unsupported on this platform")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory_at(parent_fd: int, name: str, flags: int) -> int:
    return os.open(name, flags, dir_fd=parent_fd)


def _remove_directory_tree(parent_fd: int, name: str, flags: int) -> None:
    directory_fd = _open_directory_at(parent_fd, name, flags)
    try:
        for child_name in os.listdir(directory_fd):
            child_stat = os.stat(child_name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(child_stat.st_mode):
                _remove_directory_tree(directory_fd, child_name, flags)
            else:
                os.unlink(child_name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _clean_directory_fd(directory_fd: int, flags: int, *, release_root: bool) -> None:
    for name in os.listdir(directory_fd):
        if release_root and name == ".venv":
            continue
        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry_stat.st_mode):
            if name == "__pycache__":
                _remove_directory_tree(directory_fd, name, flags)
                continue
            child_fd = _open_directory_at(directory_fd, name, flags)
            try:
                _clean_directory_fd(child_fd, flags, release_root=False)
            finally:
                os.close(child_fd)
            continue
        if stat.S_ISREG(entry_stat.st_mode) and (name.endswith(".pyc") or name.endswith(".pyo")):
            os.unlink(name, dir_fd=directory_fd)


def _clean_release_fd(release_fd: int, flags: int) -> None:
    _clean_directory_fd(release_fd, flags, release_root=True)


def _git_tree(repo_root: Path, commit: str) -> dict[str, tuple[str, bytes]]:
    if COMMIT_DIRECTORY_PATTERN.fullmatch(commit) is None:
        raise CleanupError("commit must be a full 40-character SHA")
    result = subprocess.run(
        ["git", "ls-tree", "-r", "-z", commit],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise CleanupError("unable to read trusted Git release tree")
    entries: dict[str, tuple[str, bytes]] = {}
    for raw_entry in result.stdout.split(b"\0"):
        if not raw_entry:
            continue
        metadata, raw_path = raw_entry.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
        if object_type != "blob" or mode not in {"100644", "100755", "120000"}:
            raise CleanupError("unsupported Git release tree entry")
        path = os.fsdecode(raw_path)
        if not path or path.startswith("/") or "\\" in path or ".." in Path(path).parts:
            raise CleanupError("unsafe Git release tree path")
        blob = subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        if blob.returncode != 0:
            raise CleanupError("unable to read trusted Git release blob")
        entries[path] = (mode, blob.stdout)
    return entries


def _read_file_at(directory_fd: int, name: str, flags: int) -> bytes:
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory_fd)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(file_fd)


def _release_entries(directory_fd: int, flags: int, *, prefix: str = "") -> dict[str, tuple[str, bytes]]:
    entries: dict[str, tuple[str, bytes]] = {}
    for name in os.listdir(directory_fd):
        if not prefix and name == ".venv":
            continue
        relative_path = f"{prefix}/{name}" if prefix else name
        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = _open_directory_at(directory_fd, name, flags)
            try:
                entries.update(_release_entries(child_fd, flags, prefix=relative_path))
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(entry_stat.st_mode):
            mode = "100755" if entry_stat.st_mode & stat.S_IXUSR else "100644"
            entries[relative_path] = (mode, _read_file_at(directory_fd, name, flags))
        elif stat.S_ISLNK(entry_stat.st_mode):
            entries[relative_path] = ("120000", os.fsencode(os.readlink(name, dir_fd=directory_fd)))
        else:
            raise CleanupError(f"unsupported release tree entry: {relative_path}")
    return entries


def _validate_source_fd(release_fd: int, flags: int, *, repo_root: Path, commit: str) -> None:
    if _release_entries(release_fd, flags) != _git_tree(repo_root, commit):
        raise CleanupError("release source tree does not match trusted Git commit")


def prepare_release_directory(
    *,
    release_dir: str | Path,
    releases_root: str | Path,
    repo_root: str | Path,
    commit: str,
) -> str:
    release, root = _resolve_release_target(release_dir, releases_root)
    flags = _directory_open_flags()
    root_fd = os.open(root, flags)
    try:
        try:
            release_stat = os.stat(release.name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.mkdir(release.name, mode=0o755, dir_fd=root_fd)
            release_fd = _open_directory_at(root_fd, release.name, flags)
            os.close(release_fd)
            return "created"
        if not stat.S_ISDIR(release_stat.st_mode):
            raise CleanupError("unsafe release directory: existing release is not a real directory")
        release_fd = _open_directory_at(root_fd, release.name, flags)
        try:
            _clean_release_fd(release_fd, flags)
            _validate_source_fd(release_fd, flags, repo_root=Path(repo_root).resolve(strict=True), commit=commit)
            try:
                venv_stat = os.stat(".venv", dir_fd=release_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISDIR(venv_stat.st_mode):
                    raise CleanupError("unsafe existing virtual environment")
                _remove_directory_tree(release_fd, ".venv", flags)
        finally:
            os.close(release_fd)
        return "existing"
    finally:
        os.close(root_fd)


def validate_release_source(
    *,
    release_dir: str | Path,
    releases_root: str | Path,
    repo_root: str | Path,
    commit: str,
    allow_stage: bool = False,
) -> None:
    release, root = resolve_release_paths(release_dir, releases_root, allow_stage=allow_stage)
    flags = _directory_open_flags()
    root_fd = os.open(root, flags)
    try:
        release_fd = _open_directory_at(root_fd, release.name, flags)
        try:
            _validate_source_fd(release_fd, flags, repo_root=Path(repo_root).resolve(strict=True), commit=commit)
        finally:
            os.close(release_fd)
    finally:
        os.close(root_fd)


def clean_release_bytecode(
    *, release_dir: str | Path, releases_root: str | Path, allow_stage: bool = False
) -> None:
    release, root = resolve_release_paths(release_dir, releases_root, allow_stage=allow_stage)
    flags = _directory_open_flags()
    root_fd = os.open(root, flags)
    try:
        release_fd = _open_directory_at(root_fd, release.name, flags)
        try:
            _clean_release_fd(release_fd, flags)
        finally:
            os.close(release_fd)
    finally:
        os.close(root_fd)


def remove_stage_directory(*, stage_dir: str | Path, releases_root: str | Path) -> None:
    candidate = Path(stage_dir)
    allow_backup = BACKUP_DIRECTORY_PATTERN.fullmatch(candidate.name) is not None
    stage, root = resolve_release_paths(stage_dir, releases_root, allow_stage=True)
    if STAGE_DIRECTORY_PATTERN.fullmatch(stage.name) is None and not allow_backup:
        raise CleanupError("unsafe stage directory name")
    flags = _directory_open_flags()
    root_fd = os.open(root, flags)
    try:
        _remove_directory_tree(root_fd, stage.name, flags)
    finally:
        os.close(root_fd)


def relocate_virtualenv_scripts(
    *,
    release_dir: str | Path,
    releases_root: str | Path,
    from_stage: str | Path,
    to_release: str | Path,
) -> list[str]:
    release, root = resolve_release_paths(release_dir, releases_root)
    stage = Path(from_stage)
    try:
        stage_parent = stage.parent.resolve(strict=True)
        target = Path(to_release).resolve(strict=True)
    except OSError as exc:
        raise CleanupError(f"unsafe virtualenv relocation path: {exc}") from exc
    if stage_parent != root or STAGE_DIRECTORY_PATTERN.fullmatch(stage.name) is None:
        raise CleanupError("unsafe virtualenv relocation stage")
    if target != release:
        raise CleanupError("unsafe virtualenv relocation target")
    flags = _directory_open_flags()
    root_fd = os.open(root, flags)
    changed: list[str] = []
    try:
        release_fd = _open_directory_at(root_fd, release.name, flags)
        try:
            venv_fd = _open_directory_at(release_fd, ".venv", flags)
            try:
                bin_fd = _open_directory_at(venv_fd, "bin", flags)
                try:
                    old_prefix = os.fsencode(f"#!{stage}/.venv/bin/")
                    new_prefix = os.fsencode(f"#!{release}/.venv/bin/")
                    interpreter_pattern = re.compile(br"python(?:3(?:\.\d+)*)?")
                    for name in os.listdir(bin_fd):
                        entry_stat = os.stat(name, dir_fd=bin_fd, follow_symlinks=False)
                        if not stat.S_ISREG(entry_stat.st_mode):
                            continue
                        content = _read_file_at(bin_fd, name, flags)
                        if b"\0" in content or b"\n" not in content:
                            continue
                        first_line, remainder = content.split(b"\n", 1)
                        if not first_line.startswith(old_prefix):
                            continue
                        interpreter = first_line[len(old_prefix) :]
                        if interpreter_pattern.fullmatch(interpreter) is None:
                            continue
                        output = new_prefix + interpreter + b"\n" + remainder
                        file_fd = os.open(
                            name,
                            os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                            dir_fd=bin_fd,
                        )
                        try:
                            view = memoryview(output)
                            while view:
                                written = os.write(file_fd, view)
                                if written <= 0:
                                    raise OSError("short write while relocating virtualenv script")
                                view = view[written:]
                        finally:
                            os.close(file_fd)
                        changed.append(name)
                finally:
                    os.close(bin_fd)
            finally:
                os.close(venv_fd)
        finally:
            os.close(release_fd)
    finally:
        os.close(root_fd)
    return changed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--releases-root", required=True)
    parser.add_argument("--repo-root")
    parser.add_argument("--commit")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--validate-source", action="store_true")
    action.add_argument("--remove-stage", action="store_true")
    action.add_argument("--relocate-venv", action="store_true")
    parser.add_argument("--allow-stage", action="store_true")
    parser.add_argument("--from-stage")
    parser.add_argument("--to-release")
    args = parser.parse_args(argv)
    try:
        if args.prepare or args.validate_source:
            if not args.repo_root or not args.commit:
                raise CleanupError("repo root and commit are required")
        if args.prepare:
            print(
                prepare_release_directory(
                    release_dir=args.release_dir,
                    releases_root=args.releases_root,
                    repo_root=args.repo_root,
                    commit=args.commit,
                )
            )
        elif args.validate_source:
            validate_release_source(
                release_dir=args.release_dir,
                releases_root=args.releases_root,
                repo_root=args.repo_root,
                commit=args.commit,
                allow_stage=args.allow_stage,
            )
        elif args.remove_stage:
            remove_stage_directory(stage_dir=args.release_dir, releases_root=args.releases_root)
        elif args.relocate_venv:
            if not args.from_stage or not args.to_release:
                raise CleanupError("from-stage and to-release are required")
            changed = relocate_virtualenv_scripts(
                release_dir=args.release_dir,
                releases_root=args.releases_root,
                from_stage=args.from_stage,
                to_release=args.to_release,
            )
            print("\n".join(changed))
        else:
            clean_release_bytecode(
                release_dir=args.release_dir,
                releases_root=args.releases_root,
                allow_stage=args.allow_stage,
            )
    except (CleanupError, OSError) as exc:
        print(f"release bytecode cleanup failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
