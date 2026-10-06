from __future__ import annotations

from pathlib import Path

from formextract.layout import analyze
from formextract.model import (
    BBox,
    BindingDraft,
    ControlType,
    Element,
    ElementRef,
    Option,
    ReviewReason,
)
from formextract.resolve import drafts_to_fields


def _elements(rows):
    elements: list[Element] = []
    for i, (label, spans) in enumerate(rows):
        y = i * 30
        elements.append(
            Element(
                element_id=f"label{i}",
                text=label,
                bbox=BBox(page=0, x0=0, y0=y, x1=60, y1=y + 10),
                sheet="Checklist",
            )
        )
        for x, text in spans:
            elements.append(
                Element(
                    element_id=f"r{i}_{x}_{text}",
                    text=text,
                    bbox=BBox(page=0, x0=x, y0=y, x1=x + 10, y1=y + 10),
                    sheet="Checklist",
                )
            )
    return elements


def _resolve_field(
    rows,
    field_label: str,
    *,
    control_type: ControlType = ControlType.SINGLE_SELECT,
    model_selected: str | None = None,
    model_answers=None,
    model_options=None,
):
    elements = _elements(rows)
    layout = analyze(elements)
    by_id = {e.element_id: e for e in elements}
    label, spans = next((l, s) for l, s in rows if l == field_label)

    def ref_for(text: str) -> ElementRef:
        target = next(e for e in elements if e.text == text)
        for key, bands in layout.bands.items():
            col = key % 1000
            for band_id, band in enumerate(bands):
                for seg, eid in enumerate(band):
                    if eid == target.element_id:
                        region = next(
                            r
                            for r in layout.regions
                            if r.column == col and band_id in r.band_ids
                        )
                        return ElementRef(
                            region_id=region.region_id,
                            band_id=band_id,
                            segment_index=seg,
                        )
        raise AssertionError(f"no layout ref for {text!r}")

    source_texts = [label] + [t for _, t in spans]
    source_refs = [ref_for(t) for t in source_texts]
    if model_options is None:
        model_options = [t for _, t in spans if t not in ("X", "✓")]
    options = [Option(text=t, selected=(t == model_selected)) for t in model_options]

    draft = BindingDraft(
        draft_id="d1",
        label=label,
        control_type=control_type,
        bbox=BBox(),
        options=options,
        answers=list(model_answers or []),
        region_id=ref_for(label).region_id,
        source_refs=source_refs,
        confidence=0.95,
    )
    fields = drafts_to_fields(
        [draft], layout=layout, elements_by_id=by_id, tabs=["page 0"]
    )
    return fields[0]


def test_right_only_unique_selects():
    field = _resolve_field([("Q", [(100, "X"), (120, "Option A")])], "Q")
    assert field.value_normalized == "Option A"
    assert [o.selected for o in field.options] == [True]
    assert field.provenance.review_flag is False
    assert field.provenance.heuristic_agreement == ["marker_auto_select"]


def test_x_leading_next_row_group_declines():
    field = _resolve_field(
        [
            ("Q1", [(140, "X")]),
            ("Q2", [(100, "Option A"), (120, "Option B")]),
        ],
        "Q1",
        model_options=["Option A", "Option B"],
    )
    assert all(o.selected is None for o in field.options)
    assert field.provenance.review_flag is True
    assert field.provenance.review_reason is ReviewReason.AMBIGUOUS_MARK
    assert field.value_normalized is None
    assert field.ambiguity is None


def test_right_only_with_competitor_is_ambiguous():
    field = _resolve_field(
        [("Q", [(100, "X"), (120, "Option A"), (140, "Option B")])],
        "Q",
        model_options=["Option A", "Option B"],
    )
    assert all(o.selected is None for o in field.options)
    assert field.provenance.review_flag is True
    assert field.ambiguity is not None
    assert field.ambiguity.reason == "competing_options"
    assert field.value_normalized is None


def test_right_only_in_other_band_declines():
    field = _resolve_field(
        [
            ("Q1", [(100, "X")]),
            ("Q2", [(100, "Option A")]),
        ],
        "Q1",
        model_options=["Option A"],
    )
    assert all(o.selected is None for o in field.options)
    assert field.provenance.review_flag is True
    assert field.value_normalized is None


def test_right_only_candidate_not_option_like_declines():
    field = _resolve_field(
        [("Q", [(100, "X"), (120, "123")])],
        "Q",
        model_options=["123"],
    )
    assert all(o.selected is None for o in field.options)
    assert field.provenance.review_flag is True
    assert field.value_normalized is None


def test_right_only_outside_field_row_declines():
    spans = [
        (100, "AK"),
        (112, "AL"),
        (124, "AR"),
        (136, "AZ"),
        (148, "CA"),
        (160, "CO"),
        (172, "CT"),
        (184, "X"),
    ]
    field = _resolve_field(
        [("Q", spans)],
        "Q",
        model_options=["AK", "AL", "AR", "AZ", "CA", "CO", "CT"],
    )
    assert all(o.selected is None for o in field.options)
    assert field.provenance.review_flag is True
    assert field.value_normalized is None


def test_between_marker_ambiguous_with_candidates():
    field = _resolve_field(
        [("Q", [(100, "Yes"), (120, "X"), (140, "No")])],
        "Q",
        model_options=["Yes", "No"],
        model_selected="No",
    )
    assert all(o.selected is None for o in field.options)
    assert field.provenance.review_flag is True
    assert field.provenance.review_reason is ReviewReason.AMBIGUOUS_MARK
    assert field.ambiguity is not None
    assert field.ambiguity.reason == "between_options"
    assert field.ambiguity.marker_element_id is not None
    assert len(field.ambiguity.candidate_element_ids) == 2
    assert field.value_normalized is None
    assert field.value_raw is None


def test_ambiguous_answers_not_regenerated_as_false():
    field = _resolve_field(
        [("Q", [(100, "Yes"), (120, "X"), (140, "No")])],
        "Q",
        model_options=["Yes", "No"],
        model_selected="No",
        model_answers=["No"],
    )
    assert field.value_raw is None
    assert field.value_normalized is None


def test_model_value_discarded_for_ambiguous_marker():
    field = _resolve_field(
        [("Q", [(100, "Yes"), (120, "X"), (140, "No")])],
        "Q",
        model_options=["Yes", "No"],
        model_selected="Yes",
        model_answers=["Yes"],
    )
    assert all(o.selected is None for o in field.options)
    assert field.value_normalized is None


def test_review_flag_set_on_ambiguity():
    field = _resolve_field(
        [("Q", [(100, "Yes"), (120, "X"), (140, "No")])],
        "Q",
        model_options=["Yes", "No"],
    )
    assert field.provenance.review_flag is True
    assert field.provenance.review_reason is ReviewReason.AMBIGUOUS_MARK


def test_two_runs_give_the_same_checkbox_state():
    rows = [("Q", [(100, "Yes"), (120, "X"), (140, "No")])]
    first = _resolve_field(rows, "Q", model_options=["Yes", "No"], model_selected="Yes")
    second = _resolve_field(rows, "Q", model_options=["Yes", "No"], model_selected="No")
    assert first.value_normalized == second.value_normalized is None
    assert [o.selected for o in first.options] == [o.selected for o in second.options]
    assert first.ambiguity == second.ambiguity
    assert first.provenance.review_flag == second.provenance.review_flag is True


def test_model_disagreement_flags_review():
    field = _resolve_field(
        [("Q", [(100, "X"), (120, "Option A")])],
        "Q",
        model_options=["Option A", "Option B"],
        model_selected="Option B",
    )
    # The geometric right-only selection still wins…
    assert [o.selected for o in field.options] == [True, False]
    assert field.value_normalized == "Option A"
    # …but the model's disagreement is flagged for review.
    assert field.provenance.review_flag is True
    assert field.provenance.review_reason is ReviewReason.AMBIGUOUS_MARK
    assert field.provenance.heuristic_agreement == ["marker_auto_select"]
