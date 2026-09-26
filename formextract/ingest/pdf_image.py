"""Vision-LLM fallback tier for flattened/scanned/no-text-layer documents (Phase 5)."""
from __future__ import annotations

from pathlib import Path


def ingest_pdf_image(path: str | Path) -> "IngestResult":
    raise NotImplementedError(
        "Phase 5 fallback tier not implemented: document has no text layer "
        f"({Path(path).name}); vision-LLM page-image ingestion ships in Phase 5."
    )
