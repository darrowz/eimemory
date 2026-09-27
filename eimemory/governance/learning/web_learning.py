from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.intake.safe_transport import safe_urlopen
from eimemory.models.records import RecordEnvelope, ScopeRef

MAX_FETCH_BYTES = 2_000_000
MAX_EVIDENCE_TITLE = 160
MAX_POLICY_UPDATE_CHARS = 320
MAX_REPLAY_EXPECTED_CHARS = 320


def scout_web_learning(
    runtime,
    scope,
    urls: list[str] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    timeout_seconds: int = 8,
) -> dict[str, Any]:
    scope_ref = ScopeRef.from_dict(scope)
    runtime_scope = asdict(scope_ref)
    timeout_seconds = max(1, int(timeout_seconds))

    report_items: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    fetch_errors: list[dict[str, Any]] = []

    for item in _coerce_evidence(evidence or []):
        report_items.append(item)

    for url in urls or []:
        normalized_url = str(url or "").strip()
        if not normalized_url:
            continue
        try:
            report_items.append(_fetch_evidence_item(normalized_url, timeout_seconds=timeout_seconds))
        except Exception as exc:
            detail = str(exc)
            fetch_errors.append(
                {
                    "url": normalized_url,
                    "error": type(exc).__name__,
                    "detail": detail,
                }
            )
            errors.append(
                {
                    "url": normalized_url,
                    "error": type(exc).__name__,
                    "detail": detail,
                }
            )

    hypotheses = [_to_hypothesis(item, index=index) for index, item in enumerate(report_items)]

    report: dict[str, Any] = {
        "ok": not bool(fetch_errors),
        "source": "web_learning_scout",
        "scope": runtime_scope,
        "generated_at": now_iso(),
        "requested_urls": [str(url or "").strip() for url in (urls or [])],
        "provided_evidence_count": len(list(evidence or [])),
        "fetched_url_count": len(urls or []),
        "errors": errors,
        "hypotheses": hypotheses,
    }
    report["hypothesis_count"] = len(hypotheses)

    reflection = _report_record(report, scope=scope_ref)
    runtime.store.append(reflection)

    reflection_id = reflection.record_id
    for hypothesis in hypotheses:
        hypothesis["evidence_record_id"] = reflection_id

    report["reflection_record_id"] = reflection_id
    return report


def _coerce_evidence(raw_evidence: list[dict[str, Any]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or url).strip()
        text = str(item.get("text") or "").strip()
        if not (title or text or url):
            continue
        items.append({"url": url, "title": title, "text": text})
    return items


def _fetch_evidence_item(url: str, *, timeout_seconds: int) -> dict[str, str]:
    with safe_urlopen(
        url,
        timeout=timeout_seconds,
        headers={"Accept": "text/plain, text/html, application/json, application/xml, */*;q=0.8", "User-Agent": "eimemory.web-learning/1.0"},
    ) as response:  # pragma: no branch
        content_type = str(response.headers.get_content_type() or "").strip().lower()
        if content_type and not (
            content_type.startswith("text/")
            or content_type.startswith("application/json")
            or content_type.startswith("application/xml")
            or content_type == "application/rss+xml"
            or content_type == "application/atom+xml"
        ):
            raise ValueError(f"unsupported content type: {content_type}")
        raw = response.read(MAX_FETCH_BYTES + 1)
        if len(raw) > MAX_FETCH_BYTES:
            raise ValueError("response exceeds size limit")
        charset = response.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace").strip()
        if not text:
            raise ValueError("empty body")
        return {
            "url": str(url),
            "title": _title_from_response(text, response=response),
            "text": text,
        }


def _title_from_response(text: str, *, response) -> str:
    title = str(response.headers.get("title") or "").strip()
    if title:
        return title
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    if 20 < len(first_line) <= MAX_EVIDENCE_TITLE:
        return first_line
    return ""


def _to_hypothesis(item: dict[str, str], *, index: int) -> dict[str, Any]:
    url = item.get("url") or ""
    title = item.get("title") or url
    text = item.get("text") or ""
    policy_update = (text or title).strip()
    stable_prefix = _stable_key(url, title)
    summary = _normalize_whitespace(policy_update)[:MAX_POLICY_UPDATE_CHARS]
    expected_snippet = _normalize_whitespace(text or summary)[:MAX_REPLAY_EXPECTED_CHARS]
    return {
        "id": f"web_hyp_{stable_prefix}_{index}",
        "source": "web_scout",
        "risk_level": "medium",
        "source_url": url,
        "candidate_policy": {
            "source": "web_learning_scout",
            "title": title[:MAX_EVIDENCE_TITLE],
            "policy_update": summary,
            "confidence_hint": 0.68,
        },
        "replay_hints": [
            {
                "query": title,
                "expected_text": [expected_snippet] if expected_snippet else [],
                "source_url": url,
            }
        ],
    }


def _stable_key(url: str, title: str) -> str:
    payload = f"{url}|{title}".encode("utf-8", errors="ignore")
    return sha256(payload).hexdigest()[:16]


def _normalize_whitespace(value: str) -> str:
    return " ".join(str(value or "").split())


def _report_record(report: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="reflection",
        title="Web learning scout report",
        summary="Scored web evidence into replay hypotheses",
        detail="Web learning scout report for autonomous evolution pipeline",
        content={
            "report_type": "web_learning_scout",
            "generated_at": report.get("generated_at"),
            "source": report.get("source"),
            "hypothesis_count": int(report.get("hypothesis_count") or 0),
            "errors": report.get("errors") or [],
        },
        scope=scope,
        source="eimemory.web_learning_scout",
        meta={
            "report_type": "web_learning_scout",
            "scope": asdict(scope),
            "hypothesis_count": int(report.get("hypothesis_count") or 0),
            "error_count": len(report.get("errors") or []),
            "requested_urls": report.get("requested_urls") or [],
            "provided_evidence_count": report.get("provided_evidence_count") or 0,
            "generated_at": report.get("generated_at"),
        },
    )
