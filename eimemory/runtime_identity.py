from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path


def package_import_root() -> Path:
    """Return the actual imported eimemory package root for this process."""

    return _PACKAGE_IMPORT_ROOT


def runtime_package_tree_digest() -> str:
    """Return the package digest frozen when this process imported eimemory."""

    return _PACKAGE_TREE_DIGEST


def package_tree_digest(root: str | Path) -> str:
    """Hash package-relative files and symlink targets without following links."""

    package_root = Path(root).expanduser().resolve(strict=True)
    return package_entries_digest(_package_entries(package_root))


def package_entries_digest(entries: list[tuple[str, str, bytes]]) -> str:
    digest = sha256()
    for entry_type, relative_path, payload in sorted(entries, key=lambda item: (item[1], item[0])):
        for value in (entry_type.encode("ascii"), relative_path.encode("utf-8"), payload):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def _package_entries(root: Path) -> list[tuple[str, str, bytes]]:
    entries: list[tuple[str, str, bytes]] = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as children:
            for child in sorted(children, key=lambda item: item.name):
                path = Path(child.path)
                relative = path.relative_to(root).as_posix()
                if child.name == "__pycache__" or child.name.endswith((".pyc", ".pyo")):
                    continue
                if child.is_symlink():
                    entries.append(("link", relative, os.fsencode(os.readlink(path))))
                elif child.is_dir(follow_symlinks=False):
                    visit(path)
                elif child.is_file(follow_symlinks=False):
                    entries.append(("file", relative, path.read_bytes()))

    visit(root)
    return sorted(entries, key=lambda item: (item[1], item[0]))


_PACKAGE_IMPORT_ROOT = Path(__file__).resolve().parent
_PACKAGE_TREE_DIGEST = package_tree_digest(_PACKAGE_IMPORT_ROOT)
