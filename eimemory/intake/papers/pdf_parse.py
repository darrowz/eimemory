from __future__ import annotations

from pathlib import Path


def read_pdf_text_placeholder(path: str | Path) -> str:
    """Return a safe placeholder until an optional PDF parser is configured."""
    pdf_path = Path(path)
    if not pdf_path.exists():
        raise FileNotFoundError(str(pdf_path))
    return ""
