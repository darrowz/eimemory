"""ARCH-01: Data-plane modules must not import Control/Recall at module level."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "eimemory"

# Packages considered Data plane for this guard.
DATA_DIRS = ("storage", "models", "contracts", "raw", "knowledge")
# Forbidden upward targets (Control / Recall / Integration owners).
FORBIDDEN_PREFIXES = (
    "eimemory.capabilities",
    "eimemory.governance",
    "eimemory.scoring",
    "eimemory.retrieval",
    "eimemory.api",
    "eimemory.scheduler",
    "eimemory.recall",
    "eimemory.embeddings",
    "eimemory.adapters",
    "eimemory.evaluation",
    "eimemory.experience",
    "eimemory.ei_bridge",
    "eimemory.ops",
)

# ARCH-01 closed: zero allowlist exceptions. Dual-write backfill lives in eimemory.ops
# (outside the Data-plane AST import graph). Delayed function-body imports are
# counted separately as ARCH-01-Shadow with a soft ceiling (honest, not hidden).
ALLOWLIST: dict[str, set[str]] = {}

# Soft ceiling for function-body delayed imports of forbidden prefixes.
# Lower this as shadows are eliminated; never raise without a remediation note.
ARCH01_SHADOW_SOFT_CEILING = 80


def _module_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.If):
            # Skip TYPE_CHECKING blocks
            test = node.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                continue
            if (
                isinstance(test, ast.Attribute)
                and isinstance(test.value, ast.Name)
                and test.value.id == "typing"
                and test.attr == "TYPE_CHECKING"
            ):
                continue
            for child in node.body:
                if isinstance(child, ast.ImportFrom) and child.module:
                    found.add(child.module)
    return found


def _function_body_forbidden_imports(path: Path) -> list[str]:
    """ARCH-01-Shadow: delayed imports still create a runtime dependency edge."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    rel = path.relative_to(ROOT).as_posix()

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self._scan(node)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
            self._scan(node)
            self.generic_visit(node)

        def _scan(self, node: ast.AST) -> None:
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom) and child.module:
                    module = child.module
                    if any(module == p or module.startswith(p + ".") for p in FORBIDDEN_PREFIXES):
                        hits.append(f"{rel}:{getattr(child, 'lineno', '?')} imports {module}")
                elif isinstance(child, ast.Import):
                    for alias in child.names:
                        module = alias.name
                        if any(module == p or module.startswith(p + ".") for p in FORBIDDEN_PREFIXES):
                            hits.append(f"{rel}:{getattr(child, 'lineno', '?')} imports {module}")

    Visitor().visit(tree)
    # Deduplicate exact lines (nested walk can recount).
    return sorted(set(hits))


def test_data_plane_module_imports_respect_arch01_allowlist() -> None:
    violations: list[str] = []
    for data_dir in DATA_DIRS:
        base = ROOT / data_dir
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            imports = _module_level_imports(path)
            allowed = ALLOWLIST.get(rel, set())
            for module in sorted(imports):
                if any(module == prefix or module.startswith(prefix + ".") for prefix in FORBIDDEN_PREFIXES):
                    if module in allowed or any(module.startswith(a + ".") for a in allowed):
                        continue
                    if any(module == a or module.startswith(a + ".") for a in allowed):
                        continue
                    violations.append(f"{rel} imports {module}")
    assert not violations, "ARCH-01 upward imports:\n" + "\n".join(violations)


def test_arch01_shadow_delayed_imports_are_counted_and_bounded() -> None:
    shadows: list[str] = []
    for data_dir in DATA_DIRS:
        base = ROOT / data_dir
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            shadows.extend(_function_body_forbidden_imports(path))
    assert len(shadows) <= ARCH01_SHADOW_SOFT_CEILING, (
        f"ARCH-01-Shadow delayed imports {len(shadows)} exceed soft ceiling "
        f"{ARCH01_SHADOW_SOFT_CEILING}:\n" + "\n".join(shadows)
    )


def test_capabilities_registry_does_not_import_storage_at_module_level() -> None:
    path = ROOT / "capabilities" / "registry.py"
    imports = _module_level_imports(path)
    bad = [m for m in imports if m.startswith("eimemory.storage")]
    assert bad == [], bad


def test_contracts_package_has_no_control_or_storage_imports() -> None:
    """Sink layer must not reintroduce cycles."""
    base = ROOT / "contracts"
    violations: list[str] = []
    extra_forbidden = FORBIDDEN_PREFIXES + ("eimemory.storage",)
    for path in base.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        for module in sorted(_module_level_imports(path)):
            if any(module == p or module.startswith(p + ".") for p in extra_forbidden):
                violations.append(f"{rel} imports {module}")
    assert not violations, "contracts upward imports:\n" + "\n".join(violations)
