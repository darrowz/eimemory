from __future__ import annotations

from eimemory.intake.papers.normalize import (
    detect_paper_source_kind,
    normalize_paper_input,
    normalize_paper_source_payload,
)
from eimemory.intake.papers.sources import PaperSource, ingest_paper_source, paper_source_from_payload

__all__ = [
    "PaperSource",
    "detect_paper_source_kind",
    "ingest_paper_source",
    "normalize_paper_input",
    "normalize_paper_source_payload",
    "paper_source_from_payload",
]

