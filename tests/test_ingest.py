from __future__ import annotations

import pytest

from formextract.ingest import ingest


def test_pdf_word_level_text_layer(spec_pdf):
    result = ingest(spec_pdf)
    assert result.backend.name == "pdf_text"
    assert result.backend.has_text_layer is True
    assert result.parser == "pymupdf"
    assert result.page_count == 1

    texts = [e.text for e in result.elements]
    assert "Which" in texts and "CHECKLIST" in texts
    word = next(e for e in result.elements if e.text == "Which")
    assert word.font == "Helvetica"
    assert word.size == 10.0
    assert word.color == 0
    assert word.bbox.page == 0
    assert 0 <= word.bbox.x0 < word.bbox.x1 <= 612
    assert 0 <= word.bbox.y0 < word.bbox.y1 <= 792


def test_pdf_words_keep_font_size_differences(spec_pdf):
    result = ingest(spec_pdf)
    sizes = {e.size for e in result.elements}
    assert 14.0 in sizes and 10.0 in sizes


def test_xlsx_cells_sheets_and_merged(spec_xlsx):
    result = ingest(spec_xlsx)
    assert result.backend.name == "xlsx"
    assert result.sheet_names == ["Checklist"]
    assert result.page_count == 1

    merged = next(e for e in result.elements if e.merged)
    assert merged.text.startswith("X Support Order Log")
    assert merged.bbox.x0 == 5 and merged.bbox.x1 == 11

    title = next(e for e in result.elements if e.text == "VENDOR COMPLIANCE CHECKLIST")
    assert title.size == 14.0
    assert title.sheet == "Checklist"

    plain = next(e for e in result.elements if e.text == "Do they ship to Canada?")
    assert plain.merged is False
    assert (plain.bbox.x0, plain.bbox.x1) == (0, 1)
    assert (plain.bbox.y0, plain.bbox.y1) == (6, 7)


def test_unsupported_extension_raises(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("not a form", encoding="utf-8")
    with pytest.raises(ValueError):
        ingest(p)


def test_xlsx_width_aware_bboxes_in_width_units(widths_xlsx):
    result = ingest(widths_xlsx)
    by_text = {e.text: e for e in result.elements}

    label = by_text["Label One"].bbox
    value = by_text["Value One"].bbox
    note = by_text["Note One"].bbox
    merged = by_text["Merged"]

    # Cumulative widths: A=1, B=10 (empty spacer), C=4 (sheet default),
    # D=1 and E=1 via a single range-defined entry keyed by "D" only.
    # Index-based bboxes would say (2,3)/(4,5) instead.
    assert (label.x0, label.x1) == (0.0, 1.0)
    assert (value.x0, value.x1) == (11.0, 15.0)
    assert (note.x0, note.x1) == (16.0, 17.0)
    assert merged.merged is True
    assert (merged.bbox.x0, merged.bbox.x1) == (11.0, 16.0)
    # y stays in raw row-index units (layout never mixes x and y).
    assert (label.y0, label.y1) == (0.0, 1.0)
    assert (merged.bbox.y0, merged.bbox.y1) == (2.0, 3.0)
