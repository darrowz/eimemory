from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import re


class PdfTextExtractionError(ValueError):
    """Raised when a PDF cannot supply trustworthy machine-readable text."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "pdf_extract_failed")
        self.detail = str(detail or "")
        super().__init__(f"{self.code}: {self.detail}".rstrip(": "))


@dataclass(frozen=True, slots=True)
class PdfTextExtraction:
    text: str
    page_count: int
    pages_with_text: int
    parser: str
    parser_version: str


def extract_pdf_text(data: bytes) -> PdfTextExtraction:
    """Extract canonical text from a real PDF or fail explicitly.

    Image-only PDFs deliberately return ``ocr_required`` rather than an empty
    body, so downstream knowledge extraction can never mistake missing text for
    source evidence.
    """
    if not isinstance(data, (bytes, bytearray)) or not bytes(data).startswith(b"%PDF-"):
        raise PdfTextExtractionError("invalid_pdf", "missing PDF header")
    try:
        import pypdf
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise PdfTextExtractionError("parser_unavailable", "install eimemory[pdf]") from exc
    try:
        reader = PdfReader(BytesIO(bytes(data)), strict=False)
    except Exception as exc:
        raise PdfTextExtractionError("invalid_pdf", type(exc).__name__) from exc

    page_text: list[str] = []
    pages_with_text = 0
    for page in reader.pages:
        try:
            value = page.extract_text() or ""
        except Exception as exc:
            raise PdfTextExtractionError("page_extract_failed", type(exc).__name__) from exc
        cleaned = _canonicalize_page_text(value)
        if cleaned:
            pages_with_text += 1
            page_text.append(cleaned)
    text = "\n\n".join(page_text).strip()
    if len("".join(text.split())) < 16:
        raise PdfTextExtractionError("ocr_required", "no sufficient embedded text")
    return PdfTextExtraction(
        text=text,
        page_count=len(reader.pages),
        pages_with_text=pages_with_text,
        parser="pypdf",
        parser_version=str(getattr(pypdf, "__version__", "unknown")),
    )


def _canonicalize_page_text(value: object) -> str:
    lines = []
    for raw_line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)
