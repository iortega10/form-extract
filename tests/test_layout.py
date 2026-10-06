from __future__ import annotations

from formextract.ingest import ingest
from formextract.layout import analyze
from formextract.model import BBox, ControlType, Element, GlyphKind, RegionType


def _layout(path):
    return analyze(ingest(path).elements)


def test_pdf_column_regions_and_types(spec_pdf):
    layout = _layout(spec_pdf)
    by_key = {(r.column, r.type): r for r in layout.regions}

    label_header = by_key[(0, RegionType.HEADER)]
    label_rows = by_key[(0, RegionType.FIELD_ROW)]
    grid = by_key[(1, RegionType.GRID)]

    assert len(label_header.band_ids) == 1
    assert len(label_rows.band_ids) == 5
    assert len(grid.band_ids) == 2
    assert grid.bbox.x0 >= 300


def test_pdf_anchors_exactly_densest_field_row_column(spec_pdf):
    # Anchor source is the column with the most FIELD_ROW bands (c0 here),
    # selected by density, not by leftmost column index.
    layout = _layout(spec_pdf)
    anchors = [(a.normalized_text, a.occurrence_ordinal) for a in layout.anchors]
    assert anchors == [
        ("which us states do they ship to", 0),
        ("if not all please check those that apply", 0),
        ("do they ship to canada", 0),
        ("vendor status", 0),
        ("order log", 0),
    ]


def test_pdf_cross_marks_classified(spec_pdf):
    layout = _layout(spec_pdf)
    assert len(layout.glyphs) == 4
    assert all(g.kind is GlyphKind.CROSS for g in layout.glyphs)
    # "X All" row: only the X element is a glyph, "All" is not
    glyph_ids = {g.element_id for g in layout.glyphs}
    assert len(glyph_ids) == 4


def test_pdf_hypotheses_ambiguous_controls(spec_pdf):
    layout = _layout(spec_pdf)
    guesses = [tuple(c for c in h.control_guesses) for h in layout.hypotheses]

    assert (ControlType.SINGLE_SELECT, ControlType.MULTI_SELECT) in guesses
    # F1 same-row predicate: Order Log' tall annotation column is no longer a
    # value candidate, so its guess pair moved (bool, multi_select) ->
    # (bool, single_select) (expected fallout, phase1-real-data-fixes.md F1).
    assert (ControlType.BOOL, ControlType.SINGLE_SELECT) in guesses
    ambiguous = [h for h in layout.hypotheses if h.ambiguous]
    assert ambiguous

    canada = next(
        h for h in layout.hypotheses
        if [c.value for c in h.control_guesses] == ["single_select", "multi_select"]
    )
    label_texts = " ".join(
        e.text for e in _elements(spec_pdf) if e.element_id in canada.label_element_ids
    )
    assert "Canada" in label_texts


def _elements(path):
    return ingest(path).elements


def test_pdf_grid_hypothesis(spec_pdf):
    layout = _layout(spec_pdf)
    grids = [h for h in layout.hypotheses if ":grid:" in h.region_id]
    assert grids
    h = grids[0]
    assert h.control_guesses == [ControlType.MULTI_SELECT]
    assert h.label_element_ids
    # grid label side comes from the leftmost column
    assert all(eid.split(":")[1] in {"b1", "b2"} for eid in h.label_element_ids)


def test_xlsx_layout_matches_pdf(spec_pdf, spec_xlsx):
    pdf_layout = _layout(spec_pdf)
    xls_layout = _layout(spec_xlsx)

    assert [r.type for r in xls_layout.regions] == [r.type for r in pdf_layout.regions]
    assert [a.normalized_text for a in xls_layout.anchors] == [
        a.normalized_text for a in pdf_layout.anchors
    ]
    assert len(xls_layout.glyphs) == 4
    assert len(xls_layout.hypotheses) == len(pdf_layout.hypotheses)


def test_region_element_ids_cover_region_bands(spec_pdf):
    layout = _layout(spec_pdf)
    for region in layout.regions:
        assert region.element_ids
        assert region.region_id.startswith("p0:c")


def test_xlsx_widths_gutter_splits_label_and_value_columns(widths_xlsx):
    """The 10-wide empty spacer column must open a real x-gap between labels
    and values; index-only bboxes would see a 1-unit gap and merge both into
    a single column run."""
    layout = _layout(widths_xlsx)

    assert layout.columns == {
        0: ["Widths!1:1", "Widths!2:1"],
        1: ["Widths!1:3", "Widths!1:5", "Widths!2:3", "Widths!2:5", "Widths!3:3"],
    }
    by_col = {r.column: r for r in layout.regions}
    assert set(by_col) == {0, 1}
    assert all(r.type is RegionType.FIELD_ROW for r in layout.regions)
    assert len(by_col[0].band_ids) == 2
    assert len(by_col[1].band_ids) == 3


def test_same_row_value_pairing_skips_tall_annotation_blocks():
    elements = [
        Element(element_id="lbl", text="Label", bbox=BBox(x0=0, y0=0, x1=20, y1=10)),
        Element(element_id="lbl2", text="One", bbox=BBox(x0=30, y0=0, x1=50, y1=10)),
        Element(element_id="val", text="Value", bbox=BBox(x0=120, y0=0, x1=170, y1=10)),
        Element(
            element_id="note",
            text="Tall annotation block running down the page",
            bbox=BBox(x0=245, y0=-20, x1=300, y1=25),
        ),
    ]
    layout = analyze(elements)

    h = next(h for h in layout.hypotheses if h.label_element_ids == ["lbl", "lbl2"])
    assert h.value_element_ids == ["val"]
    assert "note" not in h.value_element_ids
