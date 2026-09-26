"""PDF-with-text-layer ingestion → Element stream (word-level bounding boxes)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pymupdf as fitz

from ..model import BBox, Element


def probe_text_layer(path: str | Path) -> bool:
    with fitz.open(str(path)) as doc:
        for page in doc:
            if page.get_text("words"):
                return True
    return False


def _span_styles(page) -> list[tuple[tuple[float, float, float, float], str | None, float | None, int | None]]:
    styles = []
    data = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("text", "").strip():
                    styles.append((span["bbox"], span.get("font"), span.get("size"), span.get("color")))
    return styles


def ingest_pdf_text(path: str | Path) -> tuple[list[Element], int]:
    elements: list[Element] = []
    with fitz.open(str(path)) as doc:
        for page_index, page in enumerate(doc):
            styles = _span_styles(page)
            for x0, y0, x1, y1, word, b_i, l_i, w_i in page.get_text("words"):
                if not word.strip():
                    continue
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                font, size, color = None, None, None
                for s_bbox, s_font, s_size, s_color in styles:
                    sx0, sy0, sx1, sy1 = s_bbox
                    if sx0 - 1 <= cx <= sx1 + 1 and sy0 - 1 <= cy <= sy1 + 1:
                        font, size, color = s_font, s_size, s_color
                        break
                elements.append(
                    Element(
                        element_id=f"p{page_index}:b{b_i}:l{l_i}:w{w_i}",
                        text=word,
                        bbox=BBox(page=page_index, x0=x0, y0=y0, x1=x1, y1=y1),
                        font=font,
                        size=size,
                        color=color,
                    )
                )
        page_count = doc.page_count
    return elements, page_count


def layout_view(path: str | Path) -> str | None:
    """`pdftotext -layout` 2D text view, when the binary is available."""
    exe = shutil.which("pdftotext")
    if exe is None:
        return None
    proc = subprocess.run(
        [exe, "-layout", "-enc", "UTF-8", str(path), "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        return None
    return proc.stdout
