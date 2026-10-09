"""0.6.1-A: a resolver mark decision makes `field.answers` agree with
`options[].selected` (and keeps the model's original claim on the provenance).

Hand-derived table (synthetic rows, hand-typed elements through ``analyze``;
the model reply's `options[].selected` and `answers` are the two inputs):

| case | row | convention | model selected / answers | resolver | field.answers | value_raw | provenance.model_answers |
|---|---|---|---|---|---|---|---|
| a | Q \\| Yes \\| X \\| No | mark_follows | No / ["No"] | declared_convention -> Yes | ["Yes"] | "Yes" | ["No"] |
| b | Q \\| Yes \\| X \\| No | none | No / ["No"] | ambiguous (BETWEEN) | ["No"] | None | None |
| c | Q \\| A \\| X \\| B \\| C | mark_follows | B / ["B"] | declared_convention -> A | ["A"] | "A" | ["B"] |
| d | Q \\| A \\| B | none | A / ["A"] | none | ["A"] | "A" | None |
| e | stacked multi Alpha/Beta, both marks | none | - / ["Alpha"] | auto_select -> Alpha,Beta | ["Alpha","Beta"] | "Alpha, Beta" | ["Alpha"] |
| f | Q \\| Yes \\| X \\| No | mark_follows | Yes / ["Yes"] | declared_convention -> Yes | ["Yes"] | "Yes" | None |

The override is the change; cases (a)/(c)/(e) fail if it is dropped (the model's
own claim would remain on `answers`).
"""
from __future__ import annotations

from formextract.layout import analyze
from formextract.model import (
    CHECKBOX_MARK_FOLLOWS_OPTION,
    BBox,
    BindingDraft,
    CheckboxConvention,
    ControlType,
    Element,
    ElementRef,
    Option,
)
from formextract.resolve import drafts_to_fields


def _element(element_id: str, text: str, x: float, y: float = 0.0) -> Element:
    return Element(
        element_id=element_id,
        text=text,
        bbox=BBox(page=0, x0=x, y0=y, x1=x + 10, y1=y + 10),
        sheet="Sheet1",
    )


def _ref_for(layout, element_id: str) -> ElementRef:
    for key, bands in layout.bands.items():
        column = key % 1000
        for band_id, band in enumerate(bands):
            for seg, eid in enumerate(band):
                if eid == element_id:
                    region = next(
                        r
                        for r in layout.regions
                        if r.column == column and band_id in r.band_ids
                    )
                    return ElementRef(
                        region_id=region.region_id,
                        band_id=band_id,
                        segment_index=seg,
                    )
    raise AssertionError(element_id)


def _resolve(
    elements,
    *,
    label,
    cited,
    options,
    control_type=ControlType.SINGLE_SELECT,
    model_selected=(),
    model_answers=(),
    conventions=None,
):
    layout = analyze(elements)
    by_id = {e.element_id: e for e in elements}
    refs = [_ref_for(layout, eid) for eid in cited]
    model_options = [
        Option(text=t, selected=(t in model_selected)) for t in options
    ]
    draft = BindingDraft(
        draft_id="d1",
        label=label,
        control_type=control_type,
        bbox=BBox(),
        options=model_options,
        answers=list(model_answers),
        region_id=refs[0].region_id,
        source_refs=refs,
        confidence=0.95,
    )
    return drafts_to_fields(
        [draft],
        layout=layout,
        elements_by_id=by_id,
        tabs=["Sheet1"],
        checkbox_conventions=conventions,
    )[0]


_FOLLOWS = [CheckboxConvention(tab="Sheet1", convention=CHECKBOX_MARK_FOLLOWS_OPTION)]


def _between_row(label, spans):
    return [_element("label", label, 0)] + [
        _element(f"c{x}_{t}", t, x) for x, t in spans
    ]


_BETWEEN = [(100, "Yes"), (120, "X"), (140, "No")]
_BETWEEN_CITED = ["label", "c100_Yes", "c120_X", "c140_No"]


def test_a_answers_follow_the_declared_convention_not_the_model():
    field = _resolve(
        _between_row("Q", _BETWEEN),
        label="Q",
        cited=_BETWEEN_CITED,
        options=["Yes", "No"],
        model_selected={"No"},
        model_answers=["No"],
        conventions=_FOLLOWS,
    )
    # The resolver selected Yes, so answers mirrors it in option order.
    assert [o.selected for o in field.options] == [True, None]
    assert field.answers == ["Yes"]
    assert field.value_raw == "Yes"
    # ...and the model's own claim is kept on the provenance.
    assert field.provenance.model_answers == ["No"]


def test_b_no_convention_stays_unchanged():
    field = _resolve(
        _between_row("Q", _BETWEEN),
        label="Q",
        cited=_BETWEEN_CITED,
        options=["Yes", "No"],
        model_selected={"No"},
        model_answers=["No"],
    )
    # An ambiguous mark makes no decision: the model's answers stand.
    assert all(o.selected is None for o in field.options)
    assert field.answers == ["No"]
    assert field.value_raw is None
    assert field.provenance.model_answers is None


def test_c_answers_follow_the_option_the_mark_follows():
    field = _resolve(
        _between_row("Q", [(100, "A"), (120, "X"), (140, "B"), (160, "C")]),
        label="Q",
        cited=["label", "c100_A", "c120_X", "c140_B", "c160_C"],
        options=["A", "B", "C"],
        model_selected={"B"},
        model_answers=["B"],
        conventions=_FOLLOWS,
    )
    assert [o.selected for o in field.options] == [True, None, None]
    assert field.answers == ["A"]
    assert field.value_raw == "A"
    assert field.provenance.model_answers == ["B"]


def test_d_an_unmarked_row_is_unchanged():
    field = _resolve(
        _between_row("Q", [(100, "A"), (120, "B")]),
        label="Q",
        cited=["label", "c100_A", "c120_B"],
        options=["A", "B"],
        model_selected={"A"},
        model_answers=["A"],
    )
    assert field.answers == ["A"]
    assert field.value_raw == "A"
    assert field.provenance.model_answers is None


def _stacked(label, options, marks, *, marker_x=80, option_x=100, row_h=30):
    els = [_element("r0:label", label, 0, 0)]
    for i, text in enumerate(options):
        y = (i + 1) * row_h
        if i in marks:
            els.append(_element(f"r{i + 1}:mark", "X", marker_x, y))
        els.append(_element(f"r{i + 1}:opt", text, option_x, y))
    return els


_STACK_CITED = ["r0:label", "r1:mark", "r1:opt", "r2:mark", "r2:opt"]


def test_e_a_multi_that_selects_two_options_names_both_in_order():
    field = _resolve(
        _stacked("Pick many", ["Alpha", "Beta"], marks={0, 1}),
        label="Pick many",
        cited=_STACK_CITED,
        options=["Alpha", "Beta"],
        control_type=ControlType.MULTI_SELECT,
        model_answers=["Alpha"],
    )
    assert [o.selected for o in field.options] == [True, True]
    assert field.answers == ["Alpha", "Beta"]
    assert field.value_raw == "Alpha, Beta"
    assert field.provenance.model_answers == ["Alpha"]


def test_f_a_row_the_model_got_right_has_no_provenance_note():
    field = _resolve(
        _between_row("Q", _BETWEEN),
        label="Q",
        cited=_BETWEEN_CITED,
        options=["Yes", "No"],
        model_selected={"Yes"},
        model_answers=["Yes"],
        conventions=_FOLLOWS,
    )
    assert [o.selected for o in field.options] == [True, None]
    assert field.answers == ["Yes"]
    assert field.provenance.model_answers is None


def test_override_removed_would_leave_the_models_wrong_answer():
    """The mutation guard: the model claimed No, the resolver selected Yes.

    A build with the override dropped leaves ``answers == ["No"]`` here.
    """
    field = _resolve(
        _between_row("Q", _BETWEEN),
        label="Q",
        cited=_BETWEEN_CITED,
        options=["Yes", "No"],
        model_selected={"No"},
        model_answers=["No"],
        conventions=_FOLLOWS,
    )
    assert field.answers == ["Yes"]
    assert field.answers != ["No"]
    assert field.provenance.model_answers == ["No"]
