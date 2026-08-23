#!/usr/bin/env python3
"""Inventory maintained sources without importing or executing the runtime."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import Iterable, Sequence


@dataclass(frozen=True, slots=True)
class AuditItem:
    path: str
    disposition: str
    total_lines: int
    parsed_lines: int
    syntax_status: str
    callables: tuple[tuple[str, int, int], ...]
    imports: tuple[str, ...]
    entry_signals: tuple[str, ...]
    risk_signals: tuple[str, ...]


_MAINTAINED_PREFIXES = (
    "pyproject.toml",
    "eimemory/",
    "deploy/",
    "integrations/",
    "scripts/",
    ".github/workflows/",
)
_MAINTAINED_SUFFIXES = {
    ".py",
    ".sh",
    ".js",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".service",
    ".timer",
    ".path",
    ".conf",
    ".example",
}


def _disposition(path: Path) -> str:
    value = path.as_posix()
    if value.startswith(("deploy/", ".github/workflows/", "eimemory/ops/")):
        return "operational_gate"
    if value.startswith("eimemory/compatibility/") or "qmd_compat" in value:
        return "compatibility_surface"
    if value.startswith(
        (
            "eimemory/api/",
            "eimemory/adapters/",
            "eimemory/cli/",
            "eimemory/ei_bridge/",
            "integrations/",
            "scripts/",
        )
    ):
        return "entry_or_adapter"
    if value.startswith(
        (
            "eimemory/autonomous/",
            "eimemory/capabilities/",
            "eimemory/evaluation/",
            "eimemory/experience/",
            "eimemory/governance/",
            "eimemory/intake/",
            "eimemory/knowledge/",
            "eimemory/living/",
            "eimemory/raw/",
            "eimemory/recall/",
            "eimemory/retrieval/",
            "eimemory/scheduler/",
            "eimemory/storage/",
        )
    ):
        return "business_owner"
    return "shared_contract"


def tracked_maintained_paths(repo_root: Path) -> tuple[Path, ...]:
    root = Path(repo_root).resolve()
    result = subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--",
            *_MAINTAINED_PREFIXES,
        ),
        check=True,
        capture_output=True,
    )
    paths = (
        Path(value.decode("utf-8"))
        for value in result.stdout.split(b"\0")
        if value
    )
    return tuple(
        sorted(
            (
                path
                for path in paths
                if path.suffix.lower() in _MAINTAINED_SUFFIXES
            ),
            key=lambda path: path.as_posix(),
        )
    )


def _callable_spans(tree: ast.AST) -> tuple[tuple[str, int, int], ...]:
    values = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            values.append(
                (
                    node.name,
                    int(node.lineno),
                    int(getattr(node, "end_lineno", node.lineno)),
                )
            )
    return tuple(sorted(values, key=lambda value: (value[1], value[2], value[0])))


def _imports(tree: ast.AST) -> tuple[str, ...]:
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            values.add(node.module or ".")
    return tuple(sorted(values))


def _entry_signals(text: str) -> tuple[str, ...]:
    markers = {
        "__main__": 'if __name__ == "__main__"' in text
        or "if __name__ == '__main__'" in text,
        "argparse": "add_parser(" in text,
        "console_script": "[project.scripts]" in text,
        "package_entry_point": "[project.entry-points" in text,
        "systemd_exec": "ExecStart=" in text,
        "subprocess": "subprocess." in text,
        "node_plugin": "api.register" in text or "api.on(" in text,
    }
    return tuple(name for name, present in markers.items() if present)


def _python_risk_signals(tree: ast.AST, text: str) -> tuple[str, ...]:
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                values.add("bare_except")
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                values.add("except_pass")
        elif isinstance(node, ast.Raise):
            target = node.exc
            if isinstance(target, ast.Call):
                target = target.func
            if isinstance(target, ast.Name) and target.id == "NotImplementedError":
                values.add("not_implemented")
        elif isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr == "system"
            ):
                values.add("os_system")
            for keyword in node.keywords:
                if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant):
                    if keyword.value.value is True:
                        values.add("subprocess_shell_true")
    if "TODO" in text or "FIXME" in text:
        values.add("unfinished_marker")
    return tuple(sorted(values))


def _plain_risk_signals(text: str) -> tuple[str, ...]:
    values = []
    if "TODO" in text or "FIXME" in text:
        values.append("unfinished_marker")
    if "shell=True" in text:
        values.append("subprocess_shell_true")
    return tuple(values)


def _audit_one(repo_root: Path, relative_path: Path) -> AuditItem:
    normalized = Path(relative_path.as_posix())
    full_path = repo_root / normalized
    disposition = _disposition(normalized)
    try:
        text = full_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return AuditItem(
            path=normalized.as_posix(),
            disposition=disposition,
            total_lines=0,
            parsed_lines=0,
            syntax_status="invalid",
            callables=(),
            imports=(),
            entry_signals=(),
            risk_signals=("unreadable",),
        )

    total_lines = len(text.splitlines())
    callables: tuple[tuple[str, int, int], ...] = ()
    imports: tuple[str, ...] = ()
    risks = _plain_risk_signals(text)
    status = "deferred_native"
    parsed_lines = total_lines

    try:
        if normalized.suffix.lower() == ".py":
            tree = ast.parse(text, filename=normalized.as_posix())
            callables = _callable_spans(tree)
            imports = _imports(tree)
            risks = _python_risk_signals(tree, text)
            status = "ok"
        elif normalized.suffix.lower() == ".json" or ".json." in normalized.name:
            json.loads(text)
            status = "ok"
        elif normalized.suffix.lower() == ".toml":
            tomllib.loads(text)
            status = "ok"
    except (SyntaxError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        status = "invalid"
        parsed_lines = 0

    return AuditItem(
        path=normalized.as_posix(),
        disposition=disposition,
        total_lines=total_lines,
        parsed_lines=parsed_lines,
        syntax_status=status,
        callables=callables,
        imports=imports,
        entry_signals=_entry_signals(text),
        risk_signals=risks,
    )


def audit_paths(repo_root: Path, paths: Iterable[Path]) -> tuple[AuditItem, ...]:
    root = Path(repo_root).resolve()
    return tuple(
        _audit_one(root, path)
        for path in sorted(tuple(paths), key=lambda value: value.as_posix())
    )


def validate_complete(items: Iterable[AuditItem]) -> None:
    values = tuple(items)
    invalid = sorted(item.path for item in values if item.syntax_status == "invalid")
    uncovered = sorted(
        item.path
        for item in values
        if item.total_lines != item.parsed_lines or not item.disposition
    )
    if invalid or uncovered:
        parts = []
        if invalid:
            parts.append("invalid syntax: " + ", ".join(invalid))
        if uncovered:
            parts.append("uncovered source: " + ", ".join(uncovered))
        raise ValueError("; ".join(parts))


def _summary(items: Sequence[AuditItem]) -> dict[str, object]:
    return {
        "files": len(items),
        "lines": sum(item.total_lines for item in items),
        "callables": sum(len(item.callables) for item in items),
        "by_disposition": dict(sorted(Counter(item.disposition for item in items).items())),
        "by_syntax_status": dict(sorted(Counter(item.syntax_status for item in items).items())),
        "by_suffix": dict(
            sorted(Counter(Path(item.path).suffix.lower() for item in items).items())
        ),
        "entry_signals": dict(
            sorted(Counter(value for item in items for value in item.entry_signals).items())
        ),
        "risk_signals": dict(
            sorted(Counter(value for item in items for value in item.risk_signals).items())
        ),
    }


def render_json(items: Sequence[AuditItem]) -> str:
    payload = {
        "schema": "eimemory.business_closure_source_audit.v1",
        "summary": _summary(items),
        "items": [asdict(item) for item in items],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_markdown(items: Sequence[AuditItem]) -> str:
    summary = _summary(items)
    lines = [
        "# Maintained Business Source Audit",
        "",
        f"- Files: {summary['files']}",
        f"- Lines: {summary['lines']}",
        f"- Callables: {summary['callables']}",
        f"- Dispositions: `{json.dumps(summary['by_disposition'], sort_keys=True)}`",
        f"- Syntax: `{json.dumps(summary['by_syntax_status'], sort_keys=True)}`",
        f"- Entry signals: `{json.dumps(summary['entry_signals'], sort_keys=True)}`",
        f"- Risk signals: `{json.dumps(summary['risk_signals'], sort_keys=True)}`",
        "",
        "| Path | Disposition | Lines | Syntax | Callables | Entry signals | Risk signals |",
        "| --- | --- | ---: | --- | ---: | --- | --- |",
    ]
    for item in items:
        lines.append(
            "| {path} | {disposition} | {lines} | {syntax} | {callables} | {entries} | {risks} |".format(
                path=item.path.replace("|", "\\|"),
                disposition=item.disposition,
                lines=item.total_lines,
                syntax=item.syntax_status,
                callables=len(item.callables),
                entries=", ".join(item.entry_signals) or "-",
                risks=", ".join(item.risk_signals) or "-",
            )
        )
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = args.repo_root.resolve()
    items = audit_paths(root, tracked_maintained_paths(root))
    try:
        validate_complete(items)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    rendered = render_json(items) if args.format == "json" else render_markdown(items)
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
