from __future__ import annotations

import json
from pathlib import Path

from eimemory.models.records import RecordEnvelope


EXPORTABLE_KINDS = {"memory", "multimodal_memory"}


def should_export_record(record: RecordEnvelope) -> bool:
    quality = record.meta.get("quality") if isinstance(record.meta, dict) else None
    capture_decision = quality.get("capture_decision") if isinstance(quality, dict) else None
    return record.kind in EXPORTABLE_KINDS and record.status != "rejected" and capture_decision != "reject"


def exported_records_dir(root: str | Path) -> Path:
    return Path(root) / "qmd" / "records"


def export_record_markdown(root: str | Path, record: RecordEnvelope) -> Path | None:
    path = exported_records_dir(root) / f"{record.record_id}.md"
    if not should_export_record(record):
        if path.exists():
            path.unlink()
        return None
    target_dir = exported_records_dir(root)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{record.record_id}.md"
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
