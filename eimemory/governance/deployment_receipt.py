from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


MAX_HEALTH_RESPONSE_BYTES = 64 * 1024
RELEASE_IDENTITY_PATHS = ("pyproject.toml", "eimemory/version.py")


def verify_and_record_deployment(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    repo_root: str | Path,
    current_link: str | Path,
    health_url: str,
    prior_commit: str = "",
) -> dict[str, Any]:
    """Cross-check a live immutable release and persist its executed receipt."""

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    repo = Path(repo_root).expanduser().resolve()
    link = Path(current_link).expanduser().absolute()
    normalized_health_url = _normalize_health_url(str(health_url or ""))
    if not normalized_health_url:
        return {"ok": False, "error": "health_url_scheme_not_allowed"}
    if not (repo / ".git").exists():
        return {"ok": False, "error": "repo_not_git_checkout"}
    head = _git(repo, "rev-parse", "HEAD")
    if not head:
        return {"ok": False, "error": "repo_head_unavailable"}
    version = _project_version(repo)
    if not version:
        return {"ok": False, "error": "repo_version_unavailable"}
    rollback_commit = str(prior_commit or "").strip()
    if not _is_rollback_ancestor(repo, rollback_commit, head):
        return {"ok": False, "error": "prior_commit_not_rollback_ancestor"}
    try:
        is_link = link.is_symlink() or bool(getattr(link, "is_junction", lambda: False)())
        if not is_link:
            return {"ok": False, "error": "current_link_not_symlink"}
        release = link.resolve(strict=True)
    except OSError:
        return {"ok": False, "error": "current_link_unresolvable"}
    try:
        trusted_releases_root = (link.parent / "releases").resolve(strict=True)
        expected_release = (trusted_releases_root / head).resolve(strict=True)
    except OSError:
        return {"ok": False, "error": "current_release_untrusted"}
    if (
        not release.is_dir()
        or release != expected_release
        or release.name != head
        or not expected_release.is_relative_to(trusted_releases_root)
    ):
        return {"ok": False, "error": "current_release_untrusted"}
    release_identity_error = _release_identity_error(repo, release, head=head)
    if release_identity_error:
        return release_identity_error
    health = _fetch_health(normalized_health_url)
    if health.get("_fetch_error"):
        if health["_fetch_error"] == "health_response_too_large":
            return {"ok": False, "error": "health_response_too_large"}
        return {"ok": False, "error": "health_fetch_failed", "detail": health["_fetch_error"]}
    identity_error = _health_identity_error(
        health,
        head=head,
        version=version,
        current_link=link,
        release_path=release,
    )
    if identity_error:
        return {"ok": False, "error": identity_error}

    rollback_commands = [
        ["bash", str(repo / "deploy" / "install_immutable_release.sh"), rollback_commit],
        ["systemctl", "--user", "restart", "eimemory-rpc.service"],
        ["curl", "-fsS", normalized_health_url],
    ]
    rollback_command = json.dumps(rollback_commands, ensure_ascii=False, separators=(",", ":"))
    candidate_id = f"deployment:{head}"
    side_effect = {
        "ok": True,
        "production_applied": True,
        "deployment_executed": True,
        "verification": {
            "ok": True,
            "skipped": False,
            "repo_head": head,
            "project_version": version,
            "prior_commit": rollback_commit,
        },
        "deployment": {
            "ok": True,
            "skipped": False,
            "current_link": str(link),
            "release_path": str(release),
        },
        "post_deploy_health": {
            "ok": True,
            "skipped": False,
            "url": normalized_health_url,
            "commit": head,
            "version": version,
            "current_link": str(link),
            "release_path": str(release),
            "checks": dict(health.get("checks") or {}),
        },
        "commit": {"ok": True, "commit_sha": head},
        "release": {"version": version, "release_path": str(release)},
        "rollback_evidence": {
            "prior_commit_sha": rollback_commit,
            "rollback_command": rollback_command,
            "commands": rollback_commands,
            "strategy": "install_prior_immutable_release_restart_and_health_check",
            "verified_ancestor": True,
        },
    }
    record = append_learning_record_once(
        runtime,
        kind="promotion_request",
        title=f"Verified deployment receipt {head[:12]}",
        summary=f"Executed deployment {version} at {release} matched repo HEAD and live health.",
        scope=scope_ref,
        loop_id=f"deployment_receipt_{head[:12]}",
        step_name="deployment_receipt",
        semantic_key=stable_semantic_key(
            "deployment_receipt",
            scope_ref,
            head,
            version,
            str(release),
            rollback_commit,
            _normalized_path_key(link),
            normalized_health_url,
        ),
        authority_tier="L0",
        status="deployed",
        content={
            "report_type": "deployment_receipt",
            "candidate_id": candidate_id,
            "promotion_target": "code_patch",
            "action": "code_patch",
            "gate": {"ok": True, "receipt_verified": True},
            "side_effect": side_effect,
        },
        meta={
            "report_type": "deployment_receipt",
            "candidate_id": candidate_id,
            "promotion_target": "code_patch",
            "action": "code_patch",
            "gate_ok": True,
            "side_effect_ok": True,
            "commit_sha": head,
            "version": version,
            "release_path": str(release),
            "current_link": str(link),
            "health_url": normalized_health_url,
        },
        evidence=[head, rollback_commit],
        source="eimemory.deployment_receipt",
    )
    return _deployment_receipt_response(record)


def _deployment_receipt_response(record: Any) -> dict[str, Any]:
    content = record.content if isinstance(getattr(record, "content", None), dict) else {}
    side_effect = content.get("side_effect") if isinstance(content.get("side_effect"), dict) else {}
    verification = side_effect.get("verification") if isinstance(side_effect.get("verification"), dict) else {}
    deployment = side_effect.get("deployment") if isinstance(side_effect.get("deployment"), dict) else {}
    health = side_effect.get("post_deploy_health") if isinstance(side_effect.get("post_deploy_health"), dict) else {}
    return {
        "ok": True,
        "report_type": "deployment_receipt",
        "scope": asdict(record.scope),
        "commit": str((side_effect.get("commit") or {}).get("commit_sha") or ""),
        "version": str((side_effect.get("release") or {}).get("version") or ""),
        "current_link": str(deployment.get("current_link") or ""),
        "release_path": str(deployment.get("release_path") or ""),
        "prior_commit": str(verification.get("prior_commit") or ""),
        "health_url": str(health.get("url") or ""),
        "promotion_request_id": record.record_id,
    }


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _project_version(repo: Path) -> str:
    try:
        payload = tomllib.loads(_git(repo, "show", "HEAD:pyproject.toml"))
    except tomllib.TOMLDecodeError:
        return ""
    version = str((payload.get("project") or {}).get("version") or "").strip()
    version_module = _git(repo, "show", "HEAD:eimemory/version.py")
    if version_module:
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', version_module, re.MULTILINE)
        if match is None or match.group(1).strip() != version:
            return ""
    return version


def _is_rollback_ancestor(repo: Path, prior_commit: str, head: str) -> bool:
    if not prior_commit or prior_commit == head:
        return False
    resolved = _git(repo, "rev-parse", f"{prior_commit}^{{commit}}")
    if resolved != prior_commit:
        return False
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", prior_commit, head],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _fetch_health(url: str) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=5) as response:
            content_length = str(response.headers.get("Content-Length") or "").strip()
            if content_length and int(content_length) > MAX_HEALTH_RESPONSE_BYTES:
                return {"_fetch_error": "health_response_too_large"}
            body = response.read(MAX_HEALTH_RESPONSE_BYTES + 1)
            if len(body) > MAX_HEALTH_RESPONSE_BYTES:
                return {"_fetch_error": "health_response_too_large"}
            payload = json.loads(body.decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"_fetch_error": f"{type(exc).__name__}: {exc}"}
    return payload if isinstance(payload, dict) else {"_fetch_error": "health_payload_not_object"}


def _normalize_health_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or "").strip())
        port = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def _normalized_path_key(path: Path) -> str:
    return str(path.expanduser().absolute()).replace("\\", "/").rstrip("/").casefold()


def _release_identity_error(repo: Path, release: Path, *, head: str) -> dict[str, Any]:
    for relative_path in RELEASE_IDENTITY_PATHS:
        expected = _git_blob(repo, head=head, relative_path=relative_path)
        if expected is None:
            return {"ok": False, "error": "repo_release_identity_unavailable", "path": relative_path}
        try:
            observed = (release / relative_path).read_bytes()
        except OSError:
            return {"ok": False, "error": "release_identity_mismatch", "path": relative_path}
        if observed != expected:
            return {"ok": False, "error": "release_identity_mismatch", "path": relative_path}
    return {}


def _git_blob(repo: Path, *, head: str, relative_path: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "show", f"{head}:{relative_path}"],
            cwd=repo,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bytes(result.stdout) if result.returncode == 0 else None


def _health_identity_error(
    health: dict[str, Any],
    *,
    head: str,
    version: str,
    current_link: Path,
    release_path: Path,
) -> str:
    paths = health.get("paths") if isinstance(health.get("paths"), dict) else {}
    checks = health.get("checks") if isinstance(health.get("checks"), dict) else {}
    required = [health.get("commit"), health.get("version"), paths.get("current"), paths.get("release")]
    if health.get("ok") is not True or checks.get("ready") is not True or not all(str(value or "").strip() for value in required):
        return "health_identity_missing"
    if str(health.get("commit")) != head:
        return "health_commit_mismatch"
    if str(health.get("version")) != version:
        return "health_version_mismatch"
    if _absolute_path(paths.get("current")) != _absolute_path(current_link):
        return "health_current_link_mismatch"
    if _resolved_path(paths.get("release")) != _resolved_path(release_path):
        return "health_release_mismatch"
    return ""


def _absolute_path(value: Any) -> str:
    return str(Path(str(value or "")).expanduser().absolute()).casefold()


def _resolved_path(value: Any) -> str:
    try:
        return str(Path(str(value or "")).expanduser().resolve(strict=True)).casefold()
    except OSError:
        return ""
