"""Call-graph check for module-level public functions (cross-round #10).

A function is wired when some other site names it, or when a registration
decorator owns the entry point. The checked-in allowlist is the current set of
unwired functions. A new public function with no caller fails the test until
it is called or explicitly allowlisted.
"""
from __future__ import annotations

import ast
from pathlib import Path


# Decorators that register an entry point without a later name reference.
_ENTRY_DECORATORS = frozenset(
    {
        "register",
        "fixture",
        "command",
        "group",
        "route",
        "listener",
        "subscribe",
        "hook",
        "overload",
    }
)

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".worktrees",  # Other revisions are not callers of this checkout.
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
    }
)


def _decorator_name(node: ast.AST) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _is_entry(fn: ast.AST) -> bool:
    return any(_decorator_name(item) in _ENTRY_DECORATORS for item in getattr(fn, "decorator_list", ()))


def _python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def _references(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            found.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            found.append((node.attr, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name and alias.name != "*":
                    found.append((alias.name, node.lineno))
    return found


def unwired_public_functions(root: Path) -> set[str]:
    """Return ``relative/path.py:function`` for public module functions with no caller.

    A load of the name outside the function body counts as a caller. Shared names
    therefore count as wired when any file loads that name. The check catches
    uniquely named functions that nothing references.
    """

    definitions: list[tuple[str, str, int, int]] = []
    references: dict[str, list[tuple[str, int]]] = {}
    for path in _python_files(root):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            if relative_in_package(path, root):
                raise
            continue
        relative = path.relative_to(root).as_posix()
        in_package = relative.startswith("eimemory/")
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not in_package or node.name.startswith("_") or _is_entry(node):
                continue
            end = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
            definitions.append((relative, node.name, node.lineno, end))
        for name, lineno in _references(tree):
            references.setdefault(name, []).append((relative, lineno))
    unwired: set[str] = set()
    for relative, name, start, end in definitions:
        callers = references.get(name, ())
        if any(ref_path != relative or lineno < start or lineno > end for ref_path, lineno in callers):
            continue
        unwired.add(f"{relative}:{name}")
    return unwired


def relative_in_package(path: Path, root: Path) -> bool:
    try:
        return path.relative_to(root).as_posix().startswith("eimemory/")
    except ValueError:
        return False


def load_wiring_allowlist(path: Path) -> set[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.strip().startswith("#")}


def _write_allowlist(root: Path, destination: Path) -> None:
    items = sorted(unwired_public_functions(root))
    header = (
        "# Public module functions with no caller outside their own body.\n"
        "# Wire the function or add it here in the same change.\n"
    )
    body = "\n".join(items)
    destination.write_text(header + (body + "\n" if body else ""), encoding="utf-8")


if __name__ == "__main__":
    repo = Path(__file__).resolve().parents[2]
    _write_allowlist(repo, repo / "tests" / "wiring_allowlist.txt")
