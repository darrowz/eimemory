from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import asdict
from datetime import datetime
from hashlib import sha1
from pathlib import Path
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import RecordEnvelope, ScopeRef


CODE_EVOLUTION_SCHEMA_VERSION = "code_evolution_sandbox.v1"


class _DefaultSandboxRunner:
    def prepare_worktree(self, *, branch_name: str, root: Path) -> Path:
        path = root / branch_name
        if path.exists():
            raise FileExistsError(f"sandbox path already exists: {path}")
        root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            Path.cwd(),
            path,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                ".tmp",
                "reports",
                "state",
            ),
        )
        return path


def run_code_sandbox(
    runtime,
    *,
    incident: dict[str, Any],
    scope: dict | None = None,
    create_worktree: bool = False,
    persist_report: bool = False,
    runner: object | None = None,
    worktree_root: str | Path | None = None,
) -> dict[str, Any]:
    normalized = _normalize_incident(incident)
    category = classify_incident(normalized)
    scope_payload = asdict(ScopeRef.from_dict(scope))

    runner = runner or _DefaultSandboxRunner()
    sandbox_plan = None
    if category == "code_fixable":
        branch_name = _build_branch_name(normalized)
        allowed_files = _incident_allowed_files(normalized)
        sandbox_plan = {
            "branch_name": branch_name,
            "allowed_files": allowed_files,
            "verification_commands": _verification_commands(normalized),
            "rollback_notes": _rollback_notes(),
            "worktree_created": bool(create_worktree),
            "worktree_path": None,
            "worktree_root": str(_worktree_root(worktree_root)),
        }
        if create_worktree:
            root = _worktree_root(worktree_root)
            worktree_path = _run_runner(runner, branch_name=branch_name, root=root)
            sandbox_plan["worktree_path"] = str(worktree_path)
    generated_at = now_iso()
    report: dict[str, Any] = {
        "ok": True,
        "report_type": "code_evolution_sandbox",
        "schema_version": CODE_EVOLUTION_SCHEMA_VERSION,
        "generated_at": generated_at,
        "persist_report": bool(persist_report),
        "scope": scope_payload,
        "incident": {
            "incident_id": str(normalized.get("incident_id") or normalized.get("record_id") or ""),
            "incident_type": str(normalized.get("incident_type") or ""),
            "title": str(normalized.get("title") or ""),
            "summary": str(normalized.get("summary") or ""),
        },
        "incident_category": category,
        "incident_category_confidence": _category_confidence(normalized),
        "incident_source": str(normalized.get("source") or normalized.get("source_system") or ""),
        "sandbox_plan": sandbox_plan,
    }
    persisted_record_id = ""
    if persist_report:
        record = _code_evolution_reflection_record(report, scope=ScopeRef.from_dict(scope))
        runtime.store.append(record)
        persisted_record_id = record.record_id
    report["persisted"] = bool(persist_report)
    report["persisted_record_id"] = persisted_record_id
    return report


def classify_incident(incident: dict[str, Any]) -> str:
    incident = dict(incident or {})
    explicit = str(
        incident.get("classification")
        or incident.get("category")
        or incident.get("fix_category")
        or ""
    ).strip().lower()
    if explicit in {"policy_fixable", "config_fixable", "code_fixable", "infra_fixable", "unknown"}:
        return explicit

    incident_type = _coerce_text(incident.get("incident_type"))
    summary = _coerce_text(incident.get("summary"))
    title = _coerce_text(incident.get("title"))
    details = _coerce_text(incident.get("detail"))
    hint = _coerce_text(incident.get("incident_hint"))
    payload = _coerce_text(incident.get("payload"))
    text = " ".join(item for item in (incident_type, title, summary, details, hint, payload, _join_paths(incident.get("files")))).lower()
    if any(keyword in text for keyword in _POLICY_KEYWORDS):
        return "policy_fixable"
    if any(keyword in text for keyword in _CONFIG_KEYWORDS):
        return "config_fixable"
    if any(keyword in text for keyword in _CODE_KEYWORDS):
        return "code_fixable"
    if any(keyword in text for keyword in _INFRA_KEYWORDS):
        return "infra_fixable"
    return "unknown"


def _normalize_incident(incident: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(incident, dict):
        return {}

    normalized = dict(incident)
    content = normalized.get("content")
    if isinstance(content, dict):
        payload = content.get("payload")
        if isinstance(payload, dict):
            for key, value in payload.items():
                normalized.setdefault(key, value)
        for key in ("payload", "detail", "summary", "title"):
            if key not in normalized and key in content:
                normalized[key] = content[key]
        normalized["content"] = dict(content)
    normalized.setdefault("incident_type", _coerce_text(normalized.get("incident_type") or normalized.get("type")))
    normalized.setdefault("title", normalized.get("title") or normalized.get("name") or "")
    normalized.setdefault("summary", normalized.get("summary") or normalized.get("description") or "")
    if "incident_id" not in normalized and "record_id" in normalized:
        normalized["incident_id"] = str(normalized.get("record_id") or "")
    return normalized


def _run_runner(runner: object, *, branch_name: str, root: Path) -> Path:
    if not hasattr(runner, "prepare_worktree"):
        raise ValueError("invalid sandbox runner: prepare_worktree missing")
    return runner.prepare_worktree(branch_name=branch_name, root=root)  # type: ignore[call-arg]


def _worktree_root(raw_root: str | Path | None) -> Path:
    base = Path(raw_root) if raw_root is not None else Path(tempfile.gettempdir()) / "eimemory_code_sandbox"
    return base


def _build_branch_name(incident: dict[str, Any]) -> str:
    incident_id = _coerce_text(
        incident.get("incident_id")
        or incident.get("incident_type")
        or incident.get("record_id")
        or incident.get("title")
        or "incident"
    )
    safe_seed = re.sub(r"[^a-z0-9]+", "-", incident_id.lower())[:42].strip("-")
    if not safe_seed:
        safe_seed = "incident"
    digest = sha1(json.dumps(incident, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:8]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"code-fix-{safe_seed}-{timestamp}-{digest}"


def _incident_allowed_files(incident: dict[str, Any]) -> list[str]:
    provided = _coerce_list(incident.get("files") or incident.get("paths"))
    sanitized = []
    for item in provided:
        text = _coerce_text(item)
        if not text:
            continue
        path = text.replace("\\", "/")
        if path.endswith(".py") or "tests/" in path or "eimemory/" in path:
            sanitized.append(path)
    if not sanitized:
        sanitized = [
            "eimemory/**/*.py",
            "tests/**/*.py",
        ]
    deduped: list[str] = []
    for item in sanitized:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _verification_commands(incident: dict[str, Any]) -> list[str]:
    _ = incident
    return [
        "python -m compileall eimemory",
        "python -m pytest -q tests",
    ]


def _rollback_notes() -> list[str]:
    return [
        "No commit, push, merge, or production deployment is performed in sandbox mode.",
        "Discard the generated worktree after review if changes are not needed.",
    ]


def _category_confidence(incident: dict[str, Any]) -> float:
    incident_type = _coerce_text(incident.get("incident_type"))
    summary = _coerce_text(incident.get("summary"))
    title = _coerce_text(incident.get("title"))
    if incident_type and summary and title:
        return 0.9
    if incident_type and (summary or title):
        return 0.8
    return 0.6


def _coerce_text(value: Any) -> str:
    return str(value or "").strip()


def _coerce_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if value is None:
        return []
    return [value]


def _join_paths(value: Any) -> str:
    return " ".join(str(item) for item in _coerce_list(value))


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted(_json_safe(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _code_evolution_reflection_record(report: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope:
    generated_at = now_iso()
    summary = (
        f"Code sandbox report: category={report.get('incident_category')}, "
        f"code_candidate={bool(report.get('sandbox_plan'))}"
    )
    return RecordEnvelope.create(
        kind="reflection",
        title="Code evolution sandbox report",
        status="active",
        summary=summary,
        detail=summary,
        content={"report": _json_safe(report)},
        tags=["code-evolution", "sandbox"],
        source="eimemory.code_evolution",
        scope=scope,
        provenance={
            "report_type": "code_evolution_sandbox",
            "generated_at": generated_at,
            "schema_version": CODE_EVOLUTION_SCHEMA_VERSION,
        },
        meta={
            "report_type": "code_evolution_sandbox",
            "generated_at": generated_at,
            "schema_version": CODE_EVOLUTION_SCHEMA_VERSION,
            "incident_category": str(report.get("incident_category") or "unknown"),
            "persisted": bool(report.get("persist_report")),
            "worktree_created": bool(report.get("sandbox_plan") and report["sandbox_plan"].get("worktree_created")),
        },
    )


_POLICY_KEYWORDS = (
    "policy",
    "prompt policy",
    "policy suggestion",
    "policy_rule",
    "policy rule",
    "response_policy",
    "retrieval_policy",
    "policy fix",
)

_CONFIG_KEYWORDS = (
    "config",
    "configuration",
    "setting",
    "settings",
    "environment variable",
    "env var",
    "feature flag",
    "flag",
    "yaml",
    "toml",
    "ini",
    "json schema",
    "schema",
)

_CODE_KEYWORDS = (
    "traceback",
    "exception",
    "attributeerror",
    "typeerror",
    "valueerror",
    "keyerror",
    "indexerror",
    "syntaxerror",
    "syntax error",
    "function",
    "method",
    "class ",
    "import",
    "nullreference",
    "crash",
    "bug",
    "stack",
    "runtime",
)

_INFRA_KEYWORDS = (
    "deploy",
    "deployment",
    "release",
    "infra",
    "infrastructure",
    "service",
    "service restart",
    "rollback",
    "pipeline",
    "k8s",
    "kubernetes",
    "docker",
    "systemd",
    "network",
)
