"""Static guardrails for the Registry/Profile-first L5 capability model.

These tests deliberately inspect source instead of importing production modules:
an old compiled taxonomy must not become live merely because a module import
or a default argument reintroduces it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "eimemory"

_HISTORICAL_CAPABILITIES = frozenset(
    {
        "memory.recall",
        "tool.routing",
        "knowledge.intake",
        "proactive.judgment",
        "search.discovery",
        "research.synthesis",
        "operations.uumit",
        "device.control",
        "safety.boundary",
    }
)
_RETIRED_SYMBOL = re.compile(
    r"^(?:"
    r"READINESS_CAPABILITIES|STRONG_CAPABILITIES|WEAK_CAPABILITIES|"
    r"CORE_REPLAY_CAPABILITIES|WEAK_REPLAY_CAPABILITIES|"
    r"CASE_CONTRACTS|BUSINESS_CAPABILITIES|ATTRIBUTABLE_CAPABILITIES|CAPABILITY_TERMS|"
    r"DEFAULT_LONG_TERM(?:_[A-Z0-9_]+)?|SEEDED_[A-Z0-9_]+"
    r")$"
)
_COMPATIBILITY_CALLS = frozenset(
    {
        "ensure_legacy_evaluation_catalog",
        "ensure_all_seeded",
        "register_builtin_probe_executors",
    }
)


def _modules() -> list[Path]:
    return sorted(PRODUCTION.rglob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for item in target.elts for name in _target_names(item)]
    return []


def _literal_strings(node: ast.AST) -> set[str]:
    return {
        value.value
        for value in ast.walk(node)
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    }


def _is_explicit_true(node: ast.Call) -> bool:
    return any(
        keyword.arg == "legacy_compatibility"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in node.keywords
    )


def _has_false_default(function: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    positional = [*function.args.posonlyargs, *function.args.args]
    positional_defaults = {
        argument.arg: default
        for argument, default in zip(positional[-len(function.args.defaults) :], function.args.defaults)
    }
    keyword_defaults = {
        argument.arg: default
        for argument, default in zip(function.args.kwonlyargs, function.args.kw_defaults)
    }
    default = positional_defaults.get(name, keyword_defaults.get(name))
    return isinstance(default, ast.Constant) and default.value is False


def test_retired_taxonomy_aliases_cannot_reappear_in_production() -> None:
    violations: list[str] = []
    for path in _modules():
        tree = _tree(path)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [name for target in targets for name in _target_names(target)]
            elif isinstance(node, ast.ImportFrom):
                names = [alias.asname or alias.name for alias in node.names]
            for name in names:
                if _RETIRED_SYMBOL.fullmatch(name) and not name.startswith("LEGACY_"):
                    violations.append(f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 0)}:{name}")
    assert not violations, "retired L5 taxonomy aliases must be LEGACY_* only:\n" + "\n".join(violations)


def test_fixed_historical_capability_collections_are_legacy_named() -> None:
    violations: list[str] = []
    for path in _modules():
        tree = _tree(path)
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None or len(_literal_strings(value) & _HISTORICAL_CAPABILITIES) < 2:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [name for target in targets for name in _target_names(target)]
            if any(not name.startswith("LEGACY_") for name in names):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{','.join(names)}")
    assert not violations, "fixed L5 capability collections require a LEGACY_* isolation name:\n" + "\n".join(violations)


def test_known_capability_fallbacks_are_not_used_by_default_paths() -> None:
    violations: list[str] = []
    for path in _modules():
        tree = _tree(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "get":
                continue
            if len(node.args) < 2:
                continue
            fallback = node.args[1]
            if isinstance(fallback, ast.Constant) and fallback.value in _HISTORICAL_CAPABILITIES:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{fallback.value}")
    assert not violations, "known capabilities cannot be unconditional .get() fallback values:\n" + "\n".join(violations)


def test_legacy_bootstraps_require_an_explicit_true_flag() -> None:
    violations: list[str] = []
    for path in _modules():
        tree = _tree(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in _COMPATIBILITY_CALLS and not _is_explicit_true(node):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{name}")
    assert not violations, "legacy bootstrap calls require legacy_compatibility=True:\n" + "\n".join(violations)


def test_public_compatibility_flags_default_to_false() -> None:
    required = {
        "governance/capability_acceptance.py": {
            "ensure_legacy_evaluation_catalog",
            "capability_acceptance_case",
            "run_capability_acceptance",
        },
        "governance/capability_attribution.py": {"collect_capability_evidence"},
        "governance/capability_ledger.py": {"build_capability_ledger"},
        "governance/capability_replay_packs.py": {
            "build_capability_replay_packs",
            "capability_replay_case_ids",
        },
        "governance/capability_seeding.py": {"ensure_all_seeded"},
        "governance/l5_readiness.py": {"build_l5_readiness_report"},
    }
    violations: list[str] = []
    for relative, function_names in required.items():
        path = PRODUCTION / relative
        functions = {
            node.name: node
            for node in ast.walk(_tree(path))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in function_names:
            function = functions.get(name)
            if function is None or not _has_false_default(function, "legacy_compatibility"):
                violations.append(f"{relative}:{name}")
    assert not violations, "public compatibility paths must default legacy_compatibility to False:\n" + "\n".join(violations)


def test_dynamic_catalog_consumers_never_bootstrap_legacy_cases() -> None:
    consumers = (
        PRODUCTION / "capabilities" / "consumer_views.py",
        PRODUCTION / "governance" / "capability_replay_packs.py",
    )
    violations = [str(path.relative_to(ROOT)) for path in consumers if "ensure_legacy_evaluation_catalog" in path.read_text(encoding="utf-8")]
    assert not violations, "dynamic catalog consumers must fail closed, not bootstrap legacy cases: " + ", ".join(violations)


def test_dynamic_catalog_consumers_use_typed_application_catalog_resolution() -> None:
    consumers = (
        PRODUCTION / "capabilities" / "consumer_views.py",
        PRODUCTION / "governance" / "capability_replay_packs.py",
        PRODUCTION / "governance" / "dynamic_capability_evolution.py",
        PRODUCTION / "governance" / "capability_acceptance.py",
        PRODUCTION / "governance" / "l5_readiness.py",
    )
    violations: list[str] = []
    for path in consumers:
        source = path.read_text(encoding="utf-8")
        if "resolve_application_capability_catalog" not in source:
            violations.append(f"{path.relative_to(ROOT)}:missing_typed_catalog_resolver")
        if "default_capability_catalog(" in source:
            violations.append(f"{path.relative_to(ROOT)}:direct_default_catalog_resolution")
    assert not violations, "dynamic paths must use the typed application catalog resolver:\n" + "\n".join(violations)
