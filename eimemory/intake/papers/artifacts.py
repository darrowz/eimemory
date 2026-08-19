from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from eimemory.intake.papers.pdf_parse import PdfTextExtractionError, extract_pdf_text
from eimemory.intake.safe_transport import safe_urlopen


ARTIFACT_SCHEMA_VERSION = "paper_artifact.v2"
MAX_PDF_BYTES = 25 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_ARTIFACT_DIRECTORY = Path("artifacts") / "papers"


class PaperArtifactError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "paper_artifact_failed")
        self.detail = str(detail or "")
        super().__init__(f"{self.code}: {self.detail}".rstrip(": "))


def materialize_paper_artifacts(root: Path, paper_input: dict[str, Any]) -> dict[str, Any]:
    """Materialize verifiable, immutable PDF evidence under ``root``.

    A caller-supplied file reference is never trusted by itself. The only
    reusable references are ones backed by an immutable manifest made by this
    module and validated again before returning them.
    """
    payload = dict(paper_input or {})
    source = _resolve_pdf_input(payload)
    if source is None:
        return _reuse_or_reject_references(root, payload)

    raw_bytes, source_info = _read_pdf_source(source)
    if not raw_bytes.startswith(b"%PDF-"):
        raise PaperArtifactError("invalid_pdf", "missing PDF header")
    pdf_digest = _sha256(raw_bytes)
    pdf_path = _pdf_path(root, pdf_digest)
    _write_once(pdf_path, raw_bytes)
    pdf_ref = _root_relative(root, pdf_path)
    base = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "pdf_sha256": pdf_digest,
        "pdf_bytes": len(raw_bytes),
        "pdf_blob_ref": pdf_ref,
        "source": source_info,
    }

    try:
        extraction = extract_pdf_text(raw_bytes)
    except PdfTextExtractionError as exc:
        manifest = _seal_manifest(
            root,
            {
                **base,
                "status": "blocked",
                "error_code": exc.code,
                "error_detail": exc.detail,
            },
        )
        return {
            "pdf_blob_ref": pdf_ref,
            "normalized_text_ref": "",
            "artifact": manifest,
        }

    text_bytes = extraction.text.encode("utf-8")
    text_digest = _sha256(text_bytes)
    text_path = _text_path(root, pdf_digest, text_digest)
    _write_once(text_path, text_bytes)
    text_ref = _root_relative(root, text_path)
    manifest = _seal_manifest(
        root,
        {
            **base,
            "status": "ready",
            "normalized_text_ref": text_ref,
            "text_sha256": text_digest,
            "text_bytes": len(text_bytes),
            "page_count": extraction.page_count,
            "pages_with_text": extraction.pages_with_text,
            "parser": extraction.parser,
            "parser_version": extraction.parser_version,
            "quality": {
                "characters": len(extraction.text),
                "pages_with_text_ratio": round(extraction.pages_with_text / max(1, extraction.page_count), 3),
            },
        },
    )
    return {
        "pdf_blob_ref": pdf_ref,
        "normalized_text_ref": text_ref,
        "artifact": manifest,
    }


def load_verified_canonical_text(
    root: Path,
    *,
    pdf_blob_ref: str,
    normalized_text_ref: str,
    artifact: Mapping[str, Any] | dict[str, Any] | None,
) -> str:
    """Load canonical text only after proving the full artifact evidence chain.

    The record's references must match an immutable, content-addressed
    manifest; the manifest in turn must match both the archived PDF and text
    bytes. This is the public reader for runtime extraction and refresh.
    """
    text, _manifest = _load_verified_artifact(
        root,
        pdf_blob_ref=pdf_blob_ref,
        normalized_text_ref=normalized_text_ref,
        artifact=artifact,
    )
    return text


def load_canonical_text(root: Path, normalized_text_ref: str) -> str:
    """Read a root-relative text blob without asserting paper provenance.

    Kept for callers that only need a bounded file read. Paper runtime paths
    must use :func:`load_verified_canonical_text` instead.
    """
    ref = str(normalized_text_ref or "").strip()
    if not ref:
        raise PaperArtifactError("canonical_text_missing")
    path = _resolve_runtime_reference(root, ref)
    try:
        text_bytes = path.read_bytes()
    except OSError as exc:
        raise PaperArtifactError("canonical_text_unreadable", type(exc).__name__) from exc
    return _decode_canonical_text(text_bytes)


def _reuse_or_reject_references(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    pdf_ref = str(payload.get("pdf_blob_ref") or "").strip()
    text_ref = str(payload.get("normalized_text_ref") or "").strip()
    artifact = _provided_artifact(payload)
    if not (pdf_ref or text_ref or artifact):
        return {
            "pdf_blob_ref": "",
            "normalized_text_ref": "",
            "artifact": {"schema_version": ARTIFACT_SCHEMA_VERSION, "status": "not_requested"},
        }
    try:
        _text, manifest = _load_verified_artifact(
            root,
            pdf_blob_ref=pdf_ref,
            normalized_text_ref=text_ref,
            artifact=artifact,
        )
    except PaperArtifactError as exc:
        return {
            "pdf_blob_ref": "",
            "normalized_text_ref": "",
            "artifact": {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "status": "blocked",
                "error_code": "untrusted_artifact_reference",
                "error_detail": exc.code,
            },
        }
    return {
        "pdf_blob_ref": str(manifest["pdf_blob_ref"]),
        "normalized_text_ref": str(manifest["normalized_text_ref"]),
        "artifact": manifest,
    }


def _provided_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    candidates = [payload.get("artifact")]
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        candidates.append(metadata.get("artifact"))
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return {str(key): value for key, value in candidate.items()}
    return {}


def _load_verified_artifact(
    root: Path,
    *,
    pdf_blob_ref: str,
    normalized_text_ref: str,
    artifact: Mapping[str, Any] | dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    supplied = {str(key): value for key, value in dict(artifact or {}).items()}
    if str(supplied.get("schema_version") or "") != ARTIFACT_SCHEMA_VERSION:
        raise PaperArtifactError("artifact_schema_invalid")
    if str(supplied.get("status") or "").lower() != "ready":
        raise PaperArtifactError("artifact_not_ready")
    manifest_ref = str(supplied.get("manifest_ref") or "").strip()
    if not manifest_ref:
        raise PaperArtifactError("artifact_manifest_missing")
    manifest_path = _resolve_artifact_reference(root, manifest_ref, "manifest")
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaperArtifactError("artifact_manifest_unreadable", type(exc).__name__) from exc
    if not isinstance(loaded, dict):
        raise PaperArtifactError("artifact_manifest_invalid")
    manifest = {str(key): value for key, value in loaded.items()}
    _validate_manifest(root, manifest, manifest_ref)
    _validate_supplied_artifact(supplied, manifest)

    supplied_pdf_ref = str(pdf_blob_ref or "").strip()
    supplied_text_ref = str(normalized_text_ref or "").strip()
    if supplied_pdf_ref != str(manifest["pdf_blob_ref"]) or supplied_text_ref != str(manifest["normalized_text_ref"]):
        raise PaperArtifactError("artifact_reference_mismatch")

    pdf_path = _resolve_artifact_reference(root, supplied_pdf_ref, "pdf")
    text_path = _resolve_artifact_reference(root, supplied_text_ref, "text")
    try:
        pdf_bytes = pdf_path.read_bytes()
        text_bytes = text_path.read_bytes()
    except OSError as exc:
        raise PaperArtifactError("artifact_blob_unreadable", type(exc).__name__) from exc
    if _sha256(pdf_bytes) != str(manifest["pdf_sha256"]):
        raise PaperArtifactError("artifact_pdf_digest_mismatch")
    if _sha256(text_bytes) != str(manifest["text_sha256"]):
        raise PaperArtifactError("artifact_text_digest_mismatch")
    return _decode_canonical_text(text_bytes), manifest


def _validate_manifest(root: Path, manifest: dict[str, Any], manifest_ref: str) -> None:
    if str(manifest.get("schema_version") or "") != ARTIFACT_SCHEMA_VERSION:
        raise PaperArtifactError("artifact_manifest_schema_invalid")
    if str(manifest.get("status") or "").lower() != "ready":
        raise PaperArtifactError("artifact_manifest_not_ready")
    if str(manifest.get("manifest_ref") or "") != manifest_ref:
        raise PaperArtifactError("artifact_manifest_reference_mismatch")
    pdf_digest = str(manifest.get("pdf_sha256") or "").lower()
    text_digest = str(manifest.get("text_sha256") or "").lower()
    manifest_digest = str(manifest.get("manifest_sha256") or "").lower()
    if not (_is_sha256(pdf_digest) and _is_sha256(text_digest) and _is_sha256(manifest_digest)):
        raise PaperArtifactError("artifact_manifest_digest_invalid")
    expected_pdf_ref = _root_relative(root, _pdf_path(root, pdf_digest))
    expected_text_ref = _root_relative(root, _text_path(root, pdf_digest, text_digest))
    expected_manifest_ref = _root_relative(root, _ready_manifest_path(root, pdf_digest, text_digest, manifest_digest))
    if str(manifest.get("pdf_blob_ref") or "") != expected_pdf_ref:
        raise PaperArtifactError("artifact_pdf_reference_invalid")
    if str(manifest.get("normalized_text_ref") or "") != expected_text_ref:
        raise PaperArtifactError("artifact_text_reference_invalid")
    if manifest_ref != expected_manifest_ref:
        raise PaperArtifactError("artifact_manifest_path_invalid")
    if _manifest_digest(manifest) != manifest_digest:
        raise PaperArtifactError("artifact_manifest_digest_mismatch")
    _resolve_artifact_reference(root, expected_pdf_ref, "pdf")
    _resolve_artifact_reference(root, expected_text_ref, "text")


def _validate_supplied_artifact(supplied: dict[str, Any], manifest: dict[str, Any]) -> None:
    for key in (
        "schema_version",
        "status",
        "manifest_ref",
        "manifest_sha256",
        "pdf_blob_ref",
        "pdf_sha256",
        "normalized_text_ref",
        "text_sha256",
    ):
        if str(supplied.get(key) or "") != str(manifest.get(key) or ""):
            raise PaperArtifactError("artifact_manifest_mismatch", key)


def _seal_manifest(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    base = {str(key): value for key, value in payload.items() if key not in {"manifest_ref", "manifest_sha256"}}
    manifest_digest = _manifest_digest(base)
    pdf_digest = str(base.get("pdf_sha256") or "")
    if not _is_sha256(pdf_digest):
        raise PaperArtifactError("artifact_pdf_digest_invalid")
    status = str(base.get("status") or "").lower()
    if status == "ready":
        text_digest = str(base.get("text_sha256") or "")
        if not _is_sha256(text_digest):
            raise PaperArtifactError("artifact_text_digest_invalid")
        manifest_path = _ready_manifest_path(root, pdf_digest, text_digest, manifest_digest)
    elif status == "blocked":
        manifest_path = _blocked_manifest_path(root, pdf_digest, manifest_digest)
    else:
        raise PaperArtifactError("artifact_status_invalid", status)
    manifest = {
        **base,
        "manifest_sha256": manifest_digest,
        "manifest_ref": _root_relative(root, manifest_path),
    }
    _write_once(manifest_path, _canonical_json(manifest))
    return manifest


def _manifest_digest(payload: Mapping[str, Any]) -> str:
    canonical = {
        str(key): value
        for key, value in dict(payload).items()
        if key not in {"manifest_ref", "manifest_sha256"}
    }
    return _sha256(_canonical_json(canonical))


def _canonical_json(payload: Mapping[str, Any] | dict[str, Any]) -> bytes:
    return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return sha256(data).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _decode_canonical_text(text_bytes: bytes) -> str:
    try:
        text = text_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PaperArtifactError("canonical_text_invalid", "UnicodeDecodeError") from exc
    if len("".join(text.split())) < 16:
        raise PaperArtifactError("canonical_text_invalid")
    return text


def _resolve_pdf_input(payload: dict[str, Any]) -> tuple[str, str] | None:
    for key in ("pdf_file", "pdf_path"):
        value = str(payload.get(key) or "").strip()
        if value:
            return "local", value
    pdf_url = str(payload.get("pdf_url") or "").strip()
    if pdf_url:
        return "remote", pdf_url
    if str(payload.get("source_kind") or "").strip().lower() == "pdf":
        value = str(payload.get("canonical_url") or payload.get("paper_url") or payload.get("url") or "").strip()
        if value.startswith(("http://", "https://")):
            return "remote", value
    return None


def _read_pdf_source(source: tuple[str, str]) -> tuple[bytes, dict[str, Any]]:
    kind, location = source
    if kind == "local":
        path = Path(location).expanduser()
        try:
            if not path.is_file():
                raise PaperArtifactError("pdf_file_missing", str(path))
            if path.stat().st_size > MAX_PDF_BYTES:
                raise PaperArtifactError("pdf_too_large", str(path.stat().st_size))
            data = path.read_bytes()
        except OSError as exc:
            raise PaperArtifactError("pdf_file_unreadable", type(exc).__name__) from exc
        return data, {"kind": "local", "path_name": path.name}

    try:
        with safe_urlopen(
            location,
            timeout=15.0,
            max_redirects=3,
            headers={"User-Agent": "eimemory-paper-intake/1"},
        ) as response:
            declared_length = str(response.headers.get("Content-Length") or "").strip()
            if declared_length.isdigit() and int(declared_length) > MAX_PDF_BYTES:
                raise PaperArtifactError("pdf_too_large", declared_length)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise PaperArtifactError("pdf_too_large", str(total))
                chunks.append(chunk)
            return b"".join(chunks), {
                "kind": "remote",
                "final_url": response.geturl(),
                "content_type": str(response.headers.get("Content-Type") or ""),
                "etag": str(response.headers.get("ETag") or ""),
                "peer_ip": response.peer_ip,
            }
    except PaperArtifactError:
        raise
    except Exception as exc:
        raise PaperArtifactError("pdf_fetch_failed", type(exc).__name__) from exc


def _artifact_root(root: Path) -> Path:
    return Path(root).resolve() / _ARTIFACT_DIRECTORY


def _pdf_path(root: Path, pdf_digest: str) -> Path:
    return _artifact_root(root) / "pdfs" / f"{pdf_digest}.pdf"


def _text_path(root: Path, pdf_digest: str, text_digest: str) -> Path:
    return _artifact_root(root) / "text" / pdf_digest[:16] / f"{text_digest}.txt"


def _ready_manifest_path(root: Path, pdf_digest: str, text_digest: str, manifest_digest: str) -> Path:
    return _artifact_root(root) / "manifests" / pdf_digest[:16] / text_digest[:16] / f"{manifest_digest}.json"


def _blocked_manifest_path(root: Path, pdf_digest: str, manifest_digest: str) -> Path:
    return _artifact_root(root) / "manifests" / pdf_digest[:16] / "blocked" / f"{manifest_digest}.json"


def _write_once(path: Path, data: bytes) -> None:
    """Create an immutable blob, rejecting an impossible name/content collision."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
        return
    except FileExistsError:
        pass
    except OSError as exc:
        raise PaperArtifactError("artifact_write_failed", type(exc).__name__) from exc
    try:
        existing = path.read_bytes()
    except OSError as exc:
        raise PaperArtifactError("artifact_unreadable", type(exc).__name__) from exc
    if existing != data:
        raise PaperArtifactError("artifact_immutable_conflict", path.name)


def _root_relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(Path(root).resolve()).as_posix()


def _resolve_artifact_reference(root: Path, ref: str, kind: str) -> Path:
    path = _resolve_runtime_reference(root, ref)
    artifact_root = _artifact_root(root).resolve()
    try:
        path.relative_to(artifact_root)
    except ValueError as exc:
        raise PaperArtifactError(f"artifact_{kind}_reference_outside_store") from exc
    return path


def _resolve_runtime_reference(root: Path, ref: str) -> Path:
    candidate = Path(ref)
    if candidate.is_absolute():
        raise PaperArtifactError("canonical_text_reference_not_relative")
    root_path = Path(root).resolve()
    resolved = (root_path / candidate).resolve()
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise PaperArtifactError("canonical_text_reference_unsafe") from exc
    return resolved
