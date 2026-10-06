from __future__ import annotations

import pytest

from formextract.layout import analyze
from formextract.model import (
    AMBIGUITY_CONVENTION_UNSUPPORTED,
    CHECKBOX_MARK_FOLLOWS_OPTION,
    CHECKBOX_MARK_PRECEDES_OPTION,
    BBox,
    BindingDraft,
    CheckboxConvention,
    ControlType,
    Element,
    ElementRef,
    Option,
    ReviewReason,
)
from formextract.pipeline import (
    Pipeline,
    PipelineConfig,
    compute_cache_key,
)
from formextract.resolve import DECLARED_CONVENTION_TOKEN, drafts_to_fields
from formextract.store import Store


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
    field_label,
    *,
    conventions=None,
    control_type=ControlType.SINGLE_SELECT,
    model_selected=None,
    model_options=None,
    non_answer_ids=None,
):
    elements = _elements(rows)
    layout = analyze(elements, non_answer_element_ids=non_answer_ids or set())
    by_id = {e.element_id: e for e in elements}
    label, spans = next((l, s) for l, s in rows if l == field_label)

    def ref_for(text):
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
    options = [
        Option(text=t, selected=(t == model_selected) if model_selected is not None else None)
        for t in model_options
    ]

    draft = BindingDraft(
        draft_id="d1",
        label=label,
        control_type=control_type,
        bbox=BBox(),
        options=options,
        answers=[],
        region_id=ref_for(label).region_id,
        source_refs=source_refs,
        confidence=0.95,
    )
    fields = drafts_to_fields(
        [draft],
        layout=layout,
        elements_by_id=by_id,
        tabs=["Checklist"],
        non_answer_element_ids=non_answer_ids or set(),
        checkbox_conventions=conventions,
    )
    return fields[0]


def _convention(tab="Checklist", anchor_pattern=None, convention=CHECKBOX_MARK_FOLLOWS_OPTION):
    return CheckboxConvention(tab=tab, anchor_pattern=anchor_pattern, convention=convention)


def test_convention_config_validated(tmp_path):
    store = Store(tmp_path / "store")
    bad = [
        {"tab": "", "convention": "mark_follows_option"},
        {"tab": "Checklist", "anchor_pattern": "(", "convention": "mark_follows_option"},
        {"tab": "Checklist", "anchor_pattern": "x" * 201, "convention": "mark_follows_option"},
        {"tab": "Checklist", "convention": "sideways"},
        {"tab": "Checklist", "bogus": 1},
        {"convention": "mark_follows_option"},
        ["Checklist"],
    ]
    for entry in bad:
        with pytest.raises(ValueError):
            Pipeline(store, None, PipelineConfig(checkbox_conventions=[entry]))


def test_convention_dict_form_accepted(tmp_path):
    pipe = Pipeline(
        Store(tmp_path / "store"),
        None,
        PipelineConfig(
            checkbox_conventions=[
                {
                    "tab": "Checklist",
                    "anchor_pattern": "q",
                    "convention": "mark_follows_option",
                }
            ]
        ),
    )
    assert pipe._checkbox_conventions == [
        CheckboxConvention(
            tab="Checklist",
            anchor_pattern="q",
            convention=CHECKBOX_MARK_FOLLOWS_OPTION,
        )
    ]


def test_declared_convention_selects():
    rows = [("Q", [(100, "Yes"), (120, "X"), (140, "No")])]
    follows = _resolve_field(
        rows, "Q", conventions=[_convention(convention=CHECKBOX_MARK_FOLLOWS_OPTION)]
    )
    assert [o.selected for o in follows.options] == [True, None]
    assert follows.value_normalized == "Yes"
    assert follows.provenance.review_flag is False
    assert follows.ambiguity is None

    precedes = _resolve_field(
        rows, "Q", conventions=[_convention(convention=CHECKBOX_MARK_PRECEDES_OPTION)]
    )
    assert [o.selected for o in precedes.options] == [None, True]
    assert precedes.value_normalized == "No"
    assert precedes.provenance.review_flag is False
    assert precedes.ambiguity is None


def test_convention_applies_per_anchor_pattern():
    rows = [
        ("Q1", [(100, "Yes"), (120, "X"), (140, "No")]),
        ("Q2", [(100, "Yes"), (120, "X"), (140, "No")]),
    ]
    conventions = [_convention(anchor_pattern="q1", convention=CHECKBOX_MARK_FOLLOWS_OPTION)]
    matched = _resolve_field(rows, "Q1", conventions=conventions)
    other = _resolve_field(rows, "Q2", conventions=conventions)
    assert [o.selected for o in matched.options] == [True, None]
    assert matched.value_normalized == "Yes"
    assert all(o.selected is None for o in other.options)
    assert other.ambiguity is not None


def test_no_matching_convention_stays_ambiguous():
    field = _resolve_field(
        [("Q", [(100, "Yes"), (120, "X"), (140, "No")])],
        "Q",
        conventions=[_convention(tab="OtherTab")],
    )
    assert all(o.selected is None for o in field.options)
    assert field.ambiguity is not None
    assert field.ambiguity.reason == "between_options"
    assert field.provenance.review_flag is True


def test_model_value_still_discarded_with_a_convention():
    field = _resolve_field(
        [("Q", [(100, "Yes"), (120, "X"), (140, "No")])],
        "Q",
        conventions=[_convention(convention=CHECKBOX_MARK_FOLLOWS_OPTION)],
        model_selected="No",
    )
    assert [o.selected for o in field.options] == [True, None]
    assert field.value_normalized == "Yes"


def test_most_specific_convention_wins():
    conventions = [
        _convention(anchor_pattern=None, convention=CHECKBOX_MARK_FOLLOWS_OPTION),
        _convention(anchor_pattern="q", convention=CHECKBOX_MARK_PRECEDES_OPTION),
    ]
    field = _resolve_field(
        [("Q", [(100, "Yes"), (120, "X"), (140, "No")])], "Q", conventions=conventions
    )
    assert [o.selected for o in field.options] == [None, True]
    assert field.value_normalized == "No"


def test_conflicting_equal_specificity_conventions_raise_at_construction(tmp_path):
    with pytest.raises(ValueError):
        Pipeline(
            Store(tmp_path / "store"),
            None,
            PipelineConfig(
                checkbox_conventions=[
                    {
                        "tab": "Checklist",
                        "anchor_pattern": "q",
                        "convention": "mark_precedes_option",
                    },
                    {
                        "tab": "Checklist",
                        "anchor_pattern": "q",
                        "convention": "mark_follows_option",
                    },
                ]
            ),
        )


def test_convention_contradicted_by_geometry_flags_and_selects_nothing():
    field = _resolve_field(
        [("Q", [(100, "X"), (120, "Option A")])],
        "Q",
        conventions=[_convention(convention=CHECKBOX_MARK_FOLLOWS_OPTION)],
    )
    assert all(o.selected is None for o in field.options)
    assert field.value_normalized is None
    assert field.provenance.review_flag is True
    assert field.provenance.review_reason is ReviewReason.AMBIGUOUS_MARK
    assert field.ambiguity is not None
    assert field.ambiguity.reason == AMBIGUITY_CONVENTION_UNSUPPORTED


def test_convention_never_applies_to_right_only():
    field = _resolve_field(
        [("Q", [(100, "X"), (120, "Option A")])],
        "Q",
        conventions=[_convention(convention=CHECKBOX_MARK_PRECEDES_OPTION)],
    )
    assert [o.selected for o in field.options] == [True]
    assert field.value_normalized == "Option A"
    assert field.provenance.review_flag is False
    assert field.provenance.heuristic_agreement == ["marker_auto_select"]


def test_convention_never_applies_in_non_answer_column():
    rows = [("Q", [(100, "Yes"), (120, "X"), (140, "No")])]
    elements = _elements(rows)
    x_id = next(e.element_id for e in elements if e.text == "X")
    field = _resolve_field(
        rows,
        "Q",
        conventions=[_convention(convention=CHECKBOX_MARK_FOLLOWS_OPTION)],
        non_answer_ids={x_id},
    )
    assert all(o.selected is None for o in field.options)
    assert field.ambiguity is None


def test_cache_key_varies_with_conventions():
    base = {
        "content_hash": "c",
        "pipeline_version": "1",
        "schema_version": "2",
        "prompt_version": "2",
        "model": "m",
        "params": {"temperature": 0},
    }
    a = compute_cache_key(
        **base,
        checkbox_conventions=(_convention(convention=CHECKBOX_MARK_FOLLOWS_OPTION),),
    )
    b = compute_cache_key(
        **base,
        checkbox_conventions=(_convention(convention=CHECKBOX_MARK_PRECEDES_OPTION),),
    )
    c = compute_cache_key(**base, checkbox_conventions=())
    assert len({a, b, c}) == 3


def test_cache_key_stable_under_convention_order():
    base = {
        "content_hash": "c",
        "pipeline_version": "1",
        "schema_version": "2",
        "prompt_version": "2",
        "model": "m",
        "params": {"temperature": 0},
    }
    first = compute_cache_key(
        **base,
        checkbox_conventions=(
            {"tab": "Checklist", "anchor_pattern": None, "convention": "mark_follows_option"},
            {"tab": "Checklist", "anchor_pattern": "q", "convention": "mark_precedes_option"},
        ),
    )
    second = compute_cache_key(
        **base,
        checkbox_conventions=(
            {"tab": "Checklist", "anchor_pattern": "q", "convention": "mark_precedes_option"},
            {"tab": "Checklist", "anchor_pattern": None, "convention": "mark_follows_option"},
        ),
    )
    assert first == second


def test_convention_source_recorded_in_provenance():
    field = _resolve_field(
        [("Q", [(100, "Yes"), (120, "X"), (140, "No")])],
        "Q",
        conventions=[_convention(convention=CHECKBOX_MARK_FOLLOWS_OPTION)],
    )
    assert DECLARED_CONVENTION_TOKEN in field.provenance.heuristic_agreement
