from __future__ import annotations

# Core record export is independent of any optional runtime adapter.

import json
from pathlib import Path

from eimemory.core.record_ids import validate_record_id
from eimemory.models.records import RecordEnvelope


EXPORTABLE_KINDS = {"memory", "multimodal_memory"}


def should_export_record(record: RecordEnvelope) -> bool:
    quality = record.meta.get("quality") if isinstance(record.meta, dict) else None
    capture_decision = quality.get("capture_decision") if isinstance(quality, dict) else None
    return record.kind in EXPORTABLE_KINDS and record.status != "rejected" and capture_decision != "reject"


def exported_records_dir(root: str | Path) -> Path:
    return Path(root) / "qmd" / "records"


def _safe_export_path(export_dir: Path, record_id: str) -> Path:
    """Build export_dir / safe_name and assert resolve() stays under export root."""
    safe_name = validate_record_id(record_id)
    export_root = export_dir.resolve()
    path = (export_dir / f"{safe_name}.md").resolve()
    try:
        path.relative_to(export_root)
    except ValueError as exc:
        raise ValueError(f"export_path_escapes_root:{record_id!r}") from exc
    return path


def export_record_markdown(root: str | Path, record: RecordEnvelope) -> Path | None:
    target_dir = exported_records_dir(root)
    path = _safe_export_path(target_dir, record.record_id)
    if not should_export_record(record):
        if path.exists():
            path.unlink()
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    path = _safe_export_path(target_dir, record.record_id)
    path.write_text(render_record_markdown(record), encoding="utf-8")
    return path


def render_record_markdown(record: RecordEnvelope) -> str:
    lines = [
        f"# {record.title or record.record_id}",
        "",
        f"- Record ID: `{record.record_id}`",
        f"- Kind: `{record.kind}`",
        f"- Status: `{record.status}`",
        f"- Source: `{record.source}`",
        f"- Scope: tenant=`{record.scope.tenant_id}` agent=`{record.scope.agent_id}` workspace=`{record.scope.workspace_id}` user=`{record.scope.user_id}`",
        f"- Created At: `{record.time.created_at}`",
    ]
    if record.tags:
        lines.append(f"- Tags: {', '.join(record.tags)}")
    if record.meta:
        lines.append(f"- Meta: `{json.dumps(record.meta, ensure_ascii=False, sort_keys=True)}`")
    if record.summary:
        lines.extend(["", "## Summary", "", record.summary])
    detail = record.detail.strip()
    if detail:
        lines.extend(["", "## Detail", "", detail])
    content_lines = _render_content(record)
    if content_lines:
        lines.extend(["", "## Content", "", *content_lines])
    if record.links:
        lines.extend(["", "## Links", ""])
        for link in record.links:
            lines.append(f"- {link.relation}: `{link.target_kind}:{link.target_id}`")
    if record.evidence:
        lines.extend(["", "## Evidence", ""])
        for item in record.evidence:
            lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"


def _render_content(record: RecordEnvelope) -> list[str]:
    text = str(record.content.get("text") or "").strip()
    if text:
        return [text]
    if not record.content:
        return []
    return ["```json", json.dumps(record.content, ensure_ascii=False, indent=2, sort_keys=True), "```"]
