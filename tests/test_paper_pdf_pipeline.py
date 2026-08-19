from __future__ import annotations

import json
from pathlib import Path

import pytest

from eimemory.api.runtime import Runtime
from eimemory.intake.papers import artifacts
from eimemory.intake.papers.artifacts import (
    PaperArtifactError,
    load_canonical_text,
    load_verified_canonical_text,
    materialize_paper_artifacts,
)
from eimemory.intake.papers.pdf_parse import PdfTextExtraction, PdfTextExtractionError, extract_pdf_text


def _minimal_pdf(text: str = "") -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = (
        f"BT\n/F1 12 Tf\n72 720 Td\n({escaped}) Tj\nET\n".encode("ascii")
        if text
        else b""
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii"))
        payload.extend(obj)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(payload)


def _write_pdf(path: Path, text: str = "OpenClaw canonical source text preserves operational evidence.") -> Path:
    path.write_bytes(_minimal_pdf(text))
    return path


def test_extract_pdf_text_reads_embedded_text() -> None:
    pytest.importorskip("pypdf")

    result = extract_pdf_text(_minimal_pdf("OpenClaw canonical source text preserves operational evidence."))

    assert result.parser == "pypdf"
    assert result.page_count == 1
    assert result.pages_with_text == 1
    assert "canonical source text" in result.text


def test_materialize_pdf_archives_raw_text_and_manifest_idempotently(tmp_path) -> None:
    pytest.importorskip("pypdf")
    root = tmp_path / "runtime"
    source_path = _write_pdf(tmp_path / "paper.pdf")

    first = materialize_paper_artifacts(root, {"pdf_file": str(source_path)})
    second = materialize_paper_artifacts(root, {"pdf_file": str(source_path)})

    assert first["artifact"]["status"] == "ready"
    assert first["pdf_blob_ref"] == second["pdf_blob_ref"]
    assert first["normalized_text_ref"] == second["normalized_text_ref"]
    assert not Path(first["pdf_blob_ref"]).is_absolute()
    assert (root / first["pdf_blob_ref"]).is_file()
    assert "canonical source text" in load_canonical_text(root, first["normalized_text_ref"])
    assert "canonical source text" in load_verified_canonical_text(
        root,
        pdf_blob_ref=first["pdf_blob_ref"],
        normalized_text_ref=first["normalized_text_ref"],
        artifact=first["artifact"],
    )
    manifest = json.loads((root / first["artifact"]["manifest_ref"]).read_text(encoding="utf-8"))
    assert manifest["pdf_sha256"] == first["artifact"]["pdf_sha256"]
    assert manifest["text_sha256"] == first["artifact"]["text_sha256"]


def test_materialize_parser_failure_can_upgrade_to_ready_artifact(tmp_path, monkeypatch) -> None:
    root = tmp_path / "runtime"
    source_path = _write_pdf(tmp_path / "paper.pdf")

    def unavailable(_data: bytes) -> PdfTextExtraction:
        raise PdfTextExtractionError("parser_unavailable", "install eimemory[pdf]")

    monkeypatch.setattr(artifacts, "extract_pdf_text", unavailable)
    blocked = materialize_paper_artifacts(root, {"pdf_file": str(source_path)})

    monkeypatch.setattr(
        artifacts,
        "extract_pdf_text",
        lambda _data: PdfTextExtraction(
            text="OpenClaw canonical source text preserves operational evidence.",
            page_count=1,
            pages_with_text=1,
            parser="test",
            parser_version="1",
        ),
    )
    ready = materialize_paper_artifacts(root, {"pdf_file": str(source_path)})

    assert blocked["artifact"]["status"] == "blocked"
    assert blocked["normalized_text_ref"] == ""
    assert ready["artifact"]["status"] == "ready"
    manifest = json.loads((root / ready["artifact"]["manifest_ref"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "ready"
    assert manifest["parser"] == "test"


def test_materialized_ready_artifacts_never_repoint_old_text(tmp_path, monkeypatch) -> None:
    root = tmp_path / "runtime"
    source_path = _write_pdf(tmp_path / "paper.pdf")
    first_text = "OpenClaw first canonical source text preserves operational evidence."
    second_text = "OpenClaw revised canonical source text preserves different operational evidence."

    monkeypatch.setattr(
        artifacts,
        "extract_pdf_text",
        lambda _data: PdfTextExtraction(first_text, 1, 1, "test", "1"),
    )
    first = materialize_paper_artifacts(root, {"pdf_file": str(source_path)})
    first_manifest_path = root / first["artifact"]["manifest_ref"]
    first_manifest = json.loads(first_manifest_path.read_text(encoding="utf-8"))

    monkeypatch.setattr(
        artifacts,
        "extract_pdf_text",
        lambda _data: PdfTextExtraction(second_text, 1, 1, "test", "2"),
    )
    second = materialize_paper_artifacts(root, {"pdf_file": str(source_path)})

    assert first["normalized_text_ref"] != second["normalized_text_ref"]
    assert first["artifact"]["manifest_ref"] != second["artifact"]["manifest_ref"]
    assert json.loads(first_manifest_path.read_text(encoding="utf-8")) == first_manifest
    assert load_verified_canonical_text(
        root,
        pdf_blob_ref=first["pdf_blob_ref"],
        normalized_text_ref=first["normalized_text_ref"],
        artifact=first["artifact"],
    ) == first_text
    assert load_verified_canonical_text(
        root,
        pdf_blob_ref=second["pdf_blob_ref"],
        normalized_text_ref=second["normalized_text_ref"],
        artifact=second["artifact"],
    ) == second_text


def test_unverified_caller_references_are_blocked_and_never_reused(tmp_path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "unrelated.txt").write_text(
        "This runtime file is unrelated and must never become paper evidence.",
        encoding="utf-8",
    )

    result = materialize_paper_artifacts(
        root,
        {
            "url": "https://example.test/papers/one",
            "pdf_blob_ref": "unrelated.pdf",
            "normalized_text_ref": "unrelated.txt",
        },
    )

    assert result["pdf_blob_ref"] == ""
    assert result["normalized_text_ref"] == ""
    assert result["artifact"]["status"] == "blocked"
    assert result["artifact"]["error_code"] == "untrusted_artifact_reference"


def test_verified_module_created_references_can_be_reused(tmp_path, monkeypatch) -> None:
    root = tmp_path / "runtime"
    source_path = _write_pdf(tmp_path / "paper.pdf")
    monkeypatch.setattr(
        artifacts,
        "extract_pdf_text",
        lambda _data: PdfTextExtraction(
            "OpenClaw canonical source text preserves operational evidence.", 1, 1, "test", "1"
        ),
    )
    first = materialize_paper_artifacts(root, {"pdf_file": str(source_path)})

    reused = materialize_paper_artifacts(
        root,
        {
            "pdf_blob_ref": first["pdf_blob_ref"],
            "normalized_text_ref": first["normalized_text_ref"],
            "metadata": {"artifact": first["artifact"]},
        },
    )

    assert reused == first


def test_ingest_persists_pdf_artifact_failure_as_one_blocked_source(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        from eimemory.intake.papers import sources

        def fail_materialization(_root, _payload):
            raise PaperArtifactError("pdf_fetch_failed", "unit test")

        monkeypatch.setattr(sources, "materialize_paper_artifacts", fail_materialization)
        source = runtime.ingest_paper_source(
            {
                "pdf_url": "https://example.test/papers/one.pdf",
                "title": "Remote source",
            },
            scope={"agent_id": "papers"},
        )

        assert source.status == "blocked"
        assert source.content["pdf_blob_ref"] == ""
        assert source.content["normalized_text_ref"] == ""
        assert source.content["metadata"]["artifact"]["error_code"] == "pdf_fetch_failed"
    finally:
        runtime.close()


def test_reingest_upgrades_a_blocked_source_when_canonical_artifact_becomes_ready(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "papers"}
    source_path = _write_pdf(tmp_path / "paper.pdf")
    try:
        from eimemory.intake.papers import sources

        real_materialize = sources.materialize_paper_artifacts

        def unavailable(_root, _payload):
            raise PaperArtifactError("parser_unavailable", "unit test")

        monkeypatch.setattr(sources, "materialize_paper_artifacts", unavailable)
        blocked = runtime.ingest_paper_source({"pdf_file": str(source_path), "title": "Retry source"}, scope=scope)
        monkeypatch.setattr(sources, "materialize_paper_artifacts", real_materialize)
        ready = runtime.ingest_paper_source({"pdf_file": str(source_path), "title": "Retry source"}, scope=scope)

        assert blocked.record_id == ready.record_id
        assert blocked.status == "blocked"
        assert ready.status == "active"
        assert ready.content["normalized_text_ref"]
        persisted = runtime.store.list_records(kinds=["paper_source"], scope=scope)
        assert len(persisted) == 1
        assert persisted[0].status == "active"
    finally:
        runtime.close()


def test_materialize_rejects_non_pdf_and_marks_image_only_pdf_blocked(tmp_path, monkeypatch) -> None:
    root = tmp_path / "runtime"
    invalid = tmp_path / "invalid.pdf"
    invalid.write_bytes(b"not a pdf")

    with pytest.raises(PaperArtifactError, match="invalid_pdf"):
        materialize_paper_artifacts(root, {"pdf_file": str(invalid)})

    image_only = _write_pdf(tmp_path / "image-only.pdf", text="")

    def ocr_required(_data: bytes) -> PdfTextExtraction:
        raise PdfTextExtractionError("ocr_required", "no sufficient embedded text")

    monkeypatch.setattr(artifacts, "extract_pdf_text", ocr_required)
    blocked = materialize_paper_artifacts(root, {"pdf_file": str(image_only)})

    assert blocked["artifact"]["status"] == "blocked"
    assert blocked["artifact"]["error_code"] == "ocr_required"
    assert blocked["normalized_text_ref"] == ""


def test_runtime_extracts_only_materialized_canonical_pdf_text(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "papers", "workspace_id": "canonical"}
    try:
        source_path = _write_pdf(tmp_path / "paper.pdf")
        source = runtime.ingest_paper_source(
            {
                "pdf_file": str(source_path),
                "title": "Canonical OpenClaw Evidence",
                "abstract": "A durable source artifact.",
            },
            scope=scope,
        )
        extraction = runtime.extract_paper_source_memory(paper_source_id=source.record_id, scope=scope)

        assert source.content["metadata"]["artifact"]["status"] == "ready"
        assert source.content["normalized_text_ref"].startswith("artifacts/papers/")
        assert extraction.extract.metadata["content_origin"] == "canonical_pdf_text"
        assert "canonical source text" in extraction.extract.body
    finally:
        runtime.close()


def test_runtime_refuses_unverified_canonical_text_reference(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "papers", "workspace_id": "untrusted"}
    try:
        (runtime.store.root / "unrelated.txt").write_text(
            "This runtime file must not be promoted into canonical paper evidence.",
            encoding="utf-8",
        )
        source = runtime.ingest_paper_source(
            {
                "url": "https://example.test/papers/untrusted",
                "title": "Untrusted reference",
                "pdf_blob_ref": "unrelated.pdf",
                "normalized_text_ref": "unrelated.txt",
            },
            scope=scope,
        )

        assert source.status == "blocked"
        with pytest.raises(PaperArtifactError, match="paper_source_not_eligible"):
            runtime.extract_paper_source_memory(paper_source_id=source.record_id, scope=scope)
    finally:
        runtime.close()
