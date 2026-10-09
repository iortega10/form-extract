"""0.6.1-B: a lines label built from mark-only ``L=`` cells is never emitted
empty silently - it is repaired from the row's sole non-mark text cell (when
there is exactly one) and review-flagged either way.

Hand-derived table (a synthetic row ``X | <text>``, i.e. a mark cell and one
text cell; ``r.s`` are the lattice coordinates of the cited cells):

| reply | label | options | answers / value_raw | review |
|---|---|---|---|---|
| ``bool L=<text> O=<mark>`` | the text | [] | [] / None | not flagged |
| ``bool L=<mark> O=<text>`` | the text (repaired) | [] (the cell is no longer an option) | [] / None | flagged |
| ``text L=<mark> A=<text>`` | the text (repaired) | [] | [] / None (the cell is no longer the answer) | flagged |
| ``bool L=<mark> O=<a>,<b>`` (two text cells) | "" (not repaired) | [a, b] | [] / None | flagged |

The fixture text is our own wording, never a client's.
"""
from __future__ import annotations

from openpyxl import Workbook

from formextract.ingest import ingest
from formextract.layout import analyze
from formextract.model import ReviewReason
from formextract.resolve import (
    LLMResponse,
    ProjectionChunk,
    drafts_to_fields,
    parse_lines_response,
)
from formextract.rows import build_all_rows

LABEL = "Locked Rollup Count"


def _workbook(tmp_path, cells):
    path = tmp_path / "empty-label.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Tiny"
    for cell, value in cells:
        ws[cell] = value
    wb.save(path)
    wb.close()
    return path


def _geometry(path):
    ing = ingest(path)
    layout = analyze(ing.elements)
    by_id = {e.element_id: e for e in ing.elements}
    lattice = build_all_rows(layout, by_id)[0]
    refs = {}
    for eid in by_id:
        row = lattice.row_of_element(eid)
        if row is not None:
            refs[eid] = f"{row}.{lattice.rows[row].elements.index(eid)}"
    return layout, by_id, lattice, refs


def _parse(path, text):
    layout, by_id, lattice, _ = _geometry(path)
    chunk = ProjectionChunk(key="Tiny", text="", page=0)
    if not text.endswith("end"):
        text = text + "\nend"
    drafts, _disps, errors, _stats = parse_lines_response(
        chunk,
        lattice,
        LLMResponse(text=text, model="canned", params={}, finish_reason="stop"),
        layout=layout,
        elements_by_id=by_id,
    )
    assert errors == []
    return drafts[0], layout, by_id


def _field(draft, layout, by_id):
    return drafts_to_fields(
        [draft], layout=layout, elements_by_id=by_id, tabs=["Tiny"]
    )[0]


def test_bool_text_then_mark_keeps_the_label(tmp_path):
    path = _workbook(tmp_path, [("A1", "X"), ("B1", LABEL)])
    layout, by_id, _lattice, refs = _geometry(path)
    draft, _layout, _by = _parse(
        path, "bool L=%s O=%s" % (refs["Tiny!1:2"], refs["Tiny!1:1"])
    )
    field = _field(draft, layout, by_id)
    # The label was already non-empty, so Part B changes nothing here.
    assert field.label_text == LABEL
    assert field.options == []


def test_bool_mark_then_text_repairs_the_label_and_flags(tmp_path):
    path = _workbook(tmp_path, [("A1", "X"), ("B1", LABEL)])
    layout, by_id, _lattice, refs = _geometry(path)
    draft, _layout, _by = _parse(
        path, "bool L=%s O=%s" % (refs["Tiny!1:1"], refs["Tiny!1:2"])
    )
    field = _field(draft, layout, by_id)
    # The sole non-mark text cell becomes the label; it is dropped as an option.
    assert field.label_text == LABEL
    assert field.options == []
    assert field.provenance.review_flag is True


def test_text_mark_then_answer_repairs_the_label_and_drops_the_answer(tmp_path):
    path = _workbook(tmp_path, [("A1", "X"), ("B1", LABEL)])
    layout, by_id, _lattice, refs = _geometry(path)
    draft, _layout, _by = _parse(
        path, "text L=%s A=%s" % (refs["Tiny!1:1"], refs["Tiny!1:2"])
    )
    # The repaired cell is the label and is no longer the answer.
    assert draft.label == LABEL
    assert draft.answers == []
    field = _field(draft, layout, by_id)
    assert field.label_text == LABEL
    assert field.answers == []
    assert field.value_raw is None
    assert field.provenance.review_flag is True


def test_two_text_cells_are_left_empty_and_flagged(tmp_path):
    path = _workbook(
        tmp_path, [("A1", "X"), ("B1", "Alpha Count"), ("C1", "Beta Count")]
    )
    layout, by_id, _lattice, refs = _geometry(path)
    draft, _layout, _by = _parse(
        path,
        "bool L=%s O=%s,%s"
        % (refs["Tiny!1:1"], refs["Tiny!1:2"], refs["Tiny!1:3"]),
    )
    # Two candidates is ambiguous: the label stays empty but is never silent.
    assert draft.label == ""
    assert [o.text for o in draft.options] == ["Alpha Count", "Beta Count"]
    assert draft.review_reason is ReviewReason.AMBIGUOUS_ROLE
    field = _field(draft, layout, by_id)
    assert field.label_text == ""
    assert field.provenance.review_flag is True


def test_repair_removed_would_leave_an_empty_label(tmp_path):
    """The mutation guard: the repaired label is the sole text cell's text."""
    path = _workbook(tmp_path, [("A1", "X"), ("B1", LABEL)])
    _layout, _by, _lattice, refs = _geometry(path)
    draft, _layout, _by = _parse(
        path, "bool L=%s O=%s" % (refs["Tiny!1:1"], refs["Tiny!1:2"])
    )
    assert draft.label == LABEL
    assert draft.label != ""
