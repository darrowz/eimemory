from __future__ import annotations

# Core record export is independent of any optional runtime adapter.

import json
import stat
from hashlib import sha256
from pathlib import Path

from eimemory.core.record_ids import validate_record_id
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.atomic_file import atomic_write_bytes


EXPORTABLE_KINDS = {"memory", "multimodal_memory"}


def should_export_record(record: RecordEnvelope) -> bool:
    quality = record.meta.get("quality") if isinstance(record.meta, dict) else None
    capture_decision = quality.get("capture_decision") if isinstance(quality, dict) else None
    return record.kind in EXPORTABLE_KINDS and record.status != "rejected" and capture_decision != "reject"


def exported_records_dir(root: str | Path) -> Path:
    return Path(root) / "qmd" / "records"


def _scope_partition(scope: ScopeRef) -> str:
    """Versioned, unambiguous encoding of the exact projection scope.

    Delimiter joining is not injective when a scope field contains the
    delimiter. JSON preserves field boundaries; the full digest avoids the
    previous 48-bit truncation. Existing projections require an offline
    rebuild from SQLite before the downstream index is switched to v2.
    """
    raw = json.dumps(
        [scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "v2-" + sha256(raw.encode("utf-8")).hexdigest()


def _safe_export_path(export_dir: Path, record_id: str) -> Path:
    """Keep the leaf lexical so atomic_write_bytes can reject unsafe links.

    resolve() on a leaf would hide an in-partition symlink and redirect both
    writes and rejected-record deletion to a different record's projection.
    Parent directories must remain trusted against concurrent replacement.
    """
    safe_name = validate_record_id(record_id)
    path = export_dir / f"{safe_name}.md"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("export_leaf_must_be_single_link_regular_file")
    return path


def _export_partition_dir(root: str | Path, partition: str) -> Path:
    # The supplied application root is trusted. Derived child directories
    # must not alias another scope or leave the export tree through a link.
    directory = Path(root).resolve()
    for component in ("qmd", "records", partition):
        directory = directory / component
        try:
            info = directory.lstat()
        except FileNotFoundError:
            continue
        reparse = bool(getattr(info, "st_file_attributes", 0)
                       & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or reparse:
            raise ValueError("export_parent_must_be_real_directory")
    return directory


def export_record_markdown(root: str | Path, record: RecordEnvelope) -> Path | None:
    # L05: partition by scope so cross-scope same-record_id projections don't
    # overwrite each other.
    partition = _scope_partition(record.scope)
    target_dir = _export_partition_dir(root, partition)
    path = _safe_export_path(target_dir, record.record_id)
    if not should_export_record(record):
        if path.exists():
            path.unlink()
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir = _export_partition_dir(root, partition)
    path = _safe_export_path(target_dir, record.record_id)
    atomic_write_bytes(path, render_record_markdown(record).encode("utf-8"))
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
