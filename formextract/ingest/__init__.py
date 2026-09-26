"""Format-adaptive ingestion: pure I/O → Element stream (no geometry/typing logic)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..model import Element
from . import pdf_text, xlsx
from .pdf_image import ingest_pdf_image


@dataclass
class BackendChoice:
    name: str
    reason: str
    has_text_layer: bool | None = None


@dataclass
class IngestResult:
    elements: list[Element]
    backend: BackendChoice
    parser: str
    parser_version: str
    page_count: int | None = None
    sheet_names: list[str] = field(default_factory=list)


def ingest(path: str | Path) -> IngestResult:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        elements, sheets = xlsx.ingest_xlsx(path)
        import openpyxl

        return IngestResult(
            elements=elements,
            backend=BackendChoice("xlsx", "source workbook present (.xlsx)"),
            parser="openpyxl",
            parser_version=openpyxl.__version__,
            page_count=len(sheets),
            sheet_names=sheets,
        )
    if suffix == ".pdf":
        has_text_layer = pdf_text.probe_text_layer(path)
        if has_text_layer:
            elements, page_count = pdf_text.ingest_pdf_text(path)
            import pymupdf as fitz

            return IngestResult(
                elements=elements,
                backend=BackendChoice("pdf_text", "pdf with text layer", has_text_layer=True),
                parser="pymupdf",
                parser_version=fitz.__version__,
                page_count=page_count,
            )
        return ingest_pdf_image(path)
    raise ValueError(f"unsupported document type: {suffix or path.name}")
