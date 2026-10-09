"""T8: the geometric mark decision selects the union of a field's right-only
single-candidate markers (a vertically stacked multi-select).

A dense row ``X A  X B`` never hit the old ``right_only[0]``-only rule (interior
marks classify BETWEEN, the first sees several candidates: COMPETING). A stacked
multi-select groups one ``X Option`` per band, so every marker is RIGHT_ONLY with
a single candidate and only the first was ever applied. These tests pin the
union, its short-circuits, the model cross-check, the determinism and the gold
``stacked_multi`` score. Built with hand-typed elements through ``analyze`` --
no workbook, no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

from formextract.layout import analyze, strip_marks
from formextract.model import (
    BBox,
    BindingDraft,
    CHECKBOX_MARK_PRECEDES_OPTION,
    CheckboxConvention,
    ControlType,
    Element,
    ElementRef,
    Option,
    ReviewReason,
    to_dict,
    to_json,
)
from formextract import resolve as R
from formextract.resolve import drafts_to_fields

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "tools") not in sys.path:
    sys.path.insert(0, str(REPO / "tools"))

import make_gold  # noqa: E402
import score_gold  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _element(element_id: str, text: str, x: float, y: float) -> Element:
    return Element(
        element_id=element_id,
        text=text,
        bbox=BBox(page=0, x0=x, y0=y, x1=x + 10, y1=y + 10),
        sheet="Sheet1",
    )


def _stacked_elements(label, options, marks, *, marker_x=80, option_x=100, row_h=30):
    """A label row, then one row per option, with a marker on the marked rows."""
    els = [_element("r0:label", label, 0, 0)]
    for i, text in enumerate(options):
        y = (i + 1) * row_h
        if i in marks:
            els.append(_element(f"r{i+1}:mark", "X", marker_x, y))
        els.append(_element(f"r{i+1}:opt_{i}", text, option_x, y))
    return els


def _ref_for(layout, element_id: str) -> ElementRef:
    for key, bands in layout.bands.items():
        column = key % 1000
        for band_id, band in enumerate(bands):
            for segment, eid in enumerate(band):
                if eid == element_id:
                    region = next(
                        r
                        for r in layout.regions
                        if r.column == column and band_id in r.band_ids
                    )
                    return ElementRef(
                        region_id=region.region_id,
                        band_id=band_id,
                        segment_index=segment,
                    )
    raise AssertionError(element_id)


def _draft(elements, *, label, cited, options, control_type, model_options, region_element):
    layout = analyze(elements)
    by_id = {e.element_id: e for e in elements}
    refs = [_ref_for(layout, eid) for eid in cited]
    region_ref = _ref_for(layout, region_element or cited[0])
    draft = BindingDraft(
        draft_id="d1",
        label=label,
        control_type=control_type,
        bbox=BBox(),
        options=list(options),
        answers=[],
        region_id=region_ref.region_id,
        source_refs=refs,
        confidence=0.95,
    )
    return layout, by_id, draft


def _run_field(
    elements,
    *,
    label,
    cited,
    options,
    control_type=ControlType.MULTI_SELECT,
    model_options=None,
    model_selected=(),
    non_answer_ids=None,
    conventions=None,
    region_element=None,
):
    text_options = options if model_options is None else model_options
    opts = [Option(text=t, selected=(t in model_selected)) for t in text_options]
    layout, by_id, draft = _draft(
        elements,
        label=label,
        cited=cited,
        options=opts,
        control_type=control_type,
        model_options=text_options,
        region_element=region_element,
    )
    fields = drafts_to_fields(
        [draft],
        layout=layout,
        elements_by_id=by_id,
        tabs=["page 0"],
        non_answer_element_ids=non_answer_ids,
        checkbox_conventions=conventions,
    )
    return fields[0]


def _decision(elements, *, label, cited, options, non_answer_ids=None):
    layout, by_id, draft = _draft(
        elements,
        label=label,
        cited=cited,
        options=[Option(text=t) for t in options],
        control_type=ControlType.MULTI_SELECT,
        model_options=options,
        region_element=None,
    )
    return R._geometric_mark_decision(
        draft, list(cited), layout, by_id, set(non_answer_ids or ())
    )


_STACKED3 = ["Option A", "Option B", "Option C"]
_STACKED3_CITED = [
    "r0:label",
    "r1:mark",
    "r1:opt_0",
    "r2:opt_1",
    "r3:mark",
    "r3:opt_2",
]


# ---------------------------------------------------------------------------
# The defect and the union
# ---------------------------------------------------------------------------


def test_stacked_two_marks_select_both():
    """(a) A stacked multi-select with two marks keeps both (was: only the first)."""
    elements = _stacked_elements("Pick many", _STACKED3, marks={0, 2})
    field = _run_field(
        elements, label="Pick many", cited=_STACKED3_CITED, options=_STACKED3
    )
    assert [o.selected for o in field.options] == [True, False, True]
    assert field.value_normalized == "Option A, Option C"
    assert field.provenance.review_flag is False
    assert field.provenance.heuristic_agreement == ["marker_auto_select"]


def test_two_marks_on_one_option_select_once():
    """(b) Two markers pointing at one option select it once."""
    elements = [
        _element("r0:label", "Pick many", 0, 0),
        _element("r1:mark0", "X", 80, 30),
        _element("r1:mark1", "X", 90, 30),
        _element("r1:opt", "Option A", 100, 30),
    ]
    field = _run_field(
        elements,
        label="Pick many",
        cited=["r0:label", "r1:mark0", "r1:mark1", "r1:opt"],
        options=["Option A"],
    )
    assert [o.selected for o in field.options] == [True]
    assert field.value_normalized == "Option A"
    assert field.provenance.review_flag is False


def test_three_stacked_marks_all_selected():
    """(c) Three stacked marks select all three."""
    elements = _stacked_elements("Pick many", _STACKED3, marks={0, 1, 2})
    field = _run_field(
        elements,
        label="Pick many",
        cited=[
            "r0:label",
            "r1:mark",
            "r1:opt_0",
            "r2:mark",
            "r2:opt_1",
            "r3:mark",
            "r3:opt_2",
        ],
        options=_STACKED3,
    )
    assert [o.selected for o in field.options] == [True, True, True]
    assert field.value_normalized == "Option A, Option B, Option C"


def test_declining_marker_does_not_cancel_a_selectable_one():
    """A marker whose single candidate is missing declines for itself: the
    selectable marker still wins and the field is flagged for review."""
    elements = _stacked_elements("Pick many", _STACKED3, marks={0, 2})
    layout = analyze(elements)
    by_id = {e.element_id: e for e in elements}
    del by_id["r3:opt_2"]  # the second marker's candidate is missing
    refs = [_ref_for(layout, eid) for eid in _STACKED3_CITED]
    draft = BindingDraft(
        draft_id="d1",
        label="Pick many",
        control_type=ControlType.MULTI_SELECT,
        bbox=BBox(),
        options=[Option(text=t) for t in _STACKED3],
        answers=[],
        region_id=refs[0].region_id,
        source_refs=refs,
        confidence=0.95,
    )
    decision = R._geometric_mark_decision(
        draft, list(_STACKED3_CITED), layout, by_id, set()
    )
    assert decision.kind == "auto_select"
    assert decision.option_texts == ["Option A"]
    assert decision.declined_marker_element_ids == ["r3:mark"]

    field = drafts_to_fields(
        [draft], layout=layout, elements_by_id=by_id, tabs=["page 0"]
    )[0]
    assert [o.selected for o in field.options] == [True, False, False]
    assert field.value_normalized == "Option A"
    assert field.provenance.review_flag is True
    assert field.provenance.review_reason is ReviewReason.AMBIGUOUS_MARK


def test_zero_marks_unchanged():
    """(d) No marks: the model's own selections pass through untouched."""
    elements = _stacked_elements("Pick many", _STACKED3, marks=set())
    field = _run_field(
        elements,
        label="Pick many",
        cited=["r0:label", "r1:opt_0", "r2:opt_1", "r3:opt_2"],
        options=_STACKED3,
        model_selected={"Option B"},
    )
    assert [o.selected for o in field.options] == [False, True, False]
    assert field.value_normalized == "Option B"
    assert field.provenance.review_flag is False


# The exact ``to_json(Field)`` for one right-only stacked mark (captured on HEAD
# 5aeedb0; 0.6.1-A moves the one member it deliberately changes, ``answers``: the
# resolver's selection now mirrors onto it). The union must leave every other
# member byte-identical.
SINGLE_MARK_STACKED_JSON = """{
  "ambiguity": null,
  "annotations": [],
  "answers": [
    "Option A"
  ],
  "bbox": {
    "page": 0,
    "x0": 0.0,
    "x1": 0.0,
    "y0": 0.0,
    "y1": 0.0
  },
  "canonical_name": null,
  "control_type": "multi_select",
  "edited_by_human": false,
  "field_id": "f-28cdf03f6485497e",
  "human_value": null,
  "label_text": "Pick many",
  "normalizer_id": "none",
  "options": [
    {
      "bbox": null,
      "raw_span": null,
      "selected": true,
      "text": "Option A"
    },
    {
      "bbox": null,
      "raw_span": null,
      "selected": false,
      "text": "Option B"
    },
    {
      "bbox": null,
      "raw_span": null,
      "selected": false,
      "text": "Option C"
    }
  ],
  "provenance": {
    "binding_id": "d1",
    "binding_version": null,
    "derived_confidence": null,
    "heuristic_agreement": [
      "marker_auto_select"
    ],
    "llm_confidence": 0.95,
    "match_score": null,
    "review_flag": false,
    "review_reason": null,
    "source": "llm",
    "verified": null
  },
  "region_ref": "p0:c0:field-row:pick_many",
  "section_path": [],
  "select_all_ref": null,
  "source_elements": [
    "r0:label",
    "r1:mark",
    "r1:opt_0",
    "r2:opt_1",
    "r3:opt_2"
  ],
  "tab": "page 0",
  "unresolved_source_refs": [],
  "value_normalized": "Option A",
  "value_raw": "Option A"
}"""


def test_one_mark_byte_identical_to_pre_fix():
    """(e) Exactly one right-only marker: the whole Field is byte-identical."""
    elements = _stacked_elements("Pick many", _STACKED3, marks={0})
    field = _run_field(
        elements,
        label="Pick many",
        cited=["r0:label", "r1:mark", "r1:opt_0", "r2:opt_1", "r3:opt_2"],
        options=_STACKED3,
    )
    assert to_json(field) == SINGLE_MARK_STACKED_JSON


# ---------------------------------------------------------------------------
# The short-circuits stay first
# ---------------------------------------------------------------------------


def _short_circuit_fixture(kind: str):
    if kind == "between":
        return [
            _element("L", "Q", 0, 0),
            _element("yes", "Yes", 100, 0),
            _element("mb", "X", 120, 0),
            _element("no", "No", 140, 0),
            _element("mr", "X", 400, 0),
            _element("od", "Option D", 420, 0),
        ], ["L", "yes", "mb", "no", "mr", "od"], ["Yes", "No", "Option D"]
    return [
        _element("L", "Q", 0, 0),
        _element("mc", "X", 100, 0),
        _element("oa", "Option A", 120, 0),
        _element("ob", "Option B", 140, 0),
        _element("mr", "X", 400, 0),
        _element("od", "Option D", 420, 0),
    ], ["L", "mc", "oa", "ob", "mr", "od"], ["Option A", "Option B", "Option D"]


def test_between_marker_short_circuits_the_union():
    """(f) A BETWEEN marker anywhere in the field still wins over the union."""
    elements, cited, options = _short_circuit_fixture("between")
    field = _run_field(elements, label="Q", cited=cited, options=options)
    assert all(o.selected is None for o in field.options)
    assert field.ambiguity is not None
    assert field.ambiguity.reason == "between_options"
    assert field.value_normalized is None
    assert field.provenance.review_reason is ReviewReason.AMBIGUOUS_MARK


def test_competing_marker_short_circuits_the_union():
    """(g) A COMPETING marker likewise short-circuits the union."""
    elements, cited, options = _short_circuit_fixture("competing")
    field = _run_field(elements, label="Q", cited=cited, options=options)
    assert all(o.selected is None for o in field.options)
    assert field.ambiguity is not None
    assert field.ambiguity.reason == "competing_options"
    assert field.value_normalized is None


# ---------------------------------------------------------------------------
# The model's selected-flag cross-check compares the whole union
# ---------------------------------------------------------------------------


def test_model_agreeing_with_the_union_is_not_flagged():
    """(h) The model agreeing with the whole union gives no flag."""
    elements = _stacked_elements("Pick many", _STACKED3, marks={0, 2})
    field = _run_field(
        elements,
        label="Pick many",
        cited=_STACKED3_CITED,
        options=_STACKED3,
        model_selected={"Option A", "Option C"},
    )
    assert [o.selected for o in field.options] == [True, False, True]
    assert field.provenance.review_flag is False


def test_model_selecting_a_subset_flags_review():
    """(h) A model subset of the union is flagged."""
    elements = _stacked_elements("Pick many", _STACKED3, marks={0, 2})
    field = _run_field(
        elements,
        label="Pick many",
        cited=_STACKED3_CITED,
        options=_STACKED3,
        model_selected={"Option A"},
    )
    assert [o.selected for o in field.options] == [True, False, True]
    assert field.provenance.review_flag is True
    assert field.provenance.review_reason is ReviewReason.AMBIGUOUS_MARK


def test_model_selecting_an_extra_option_flags_review():
    """(h) A model superset of the union is flagged."""
    elements = _stacked_elements("Pick many", _STACKED3, marks={0, 2})
    field = _run_field(
        elements,
        label="Pick many",
        cited=_STACKED3_CITED,
        options=_STACKED3,
        model_selected={"Option A", "Option B", "Option C"},
    )
    assert [o.selected for o in field.options] == [True, False, True]
    assert field.provenance.review_flag is True
    assert field.provenance.review_reason is ReviewReason.AMBIGUOUS_MARK


# ---------------------------------------------------------------------------
# Non-answer markers and declared conventions
# ---------------------------------------------------------------------------


def test_non_answer_marker_and_candidate_are_ignored():
    """(i) A marker in a non-answer column, and an unmatched non-answer
    candidate, are ignored as before; the other marker still selects."""
    elements = _stacked_elements("Pick many", _STACKED3, marks={0, 2})
    # the marker of the third option is a non-answer element: it is dropped
    field = _run_field(
        elements,
        label="Pick many",
        cited=_STACKED3_CITED,
        options=_STACKED3,
        non_answer_ids={"r3:mark"},
    )
    assert [o.selected for o in field.options] == [True, False, False]
    assert field.provenance.review_flag is False

    # a marker whose single candidate is a non-answer element that is not an
    # option contributes nothing; the first marker still selects.
    field = _run_field(
        elements,
        label="Pick many",
        cited=_STACKED3_CITED,
        options=["Option A", "Option B"],
        model_options=["Option A", "Option B"],
        non_answer_ids={"r3:opt_2"},
    )
    assert [o.selected for o in field.options] == [True, False]
    assert field.value_normalized == "Option A"
    assert field.provenance.review_flag is False


# The exact Field for a BETWEEN marker resolved by a declared
# ``mark_precedes_option`` convention (captured on HEAD 5aeedb0; 0.6.1-A moves
# the one member it deliberately changes, ``answers``: a resolver selection now
# mirrors onto ``answers``).
BETWEEN_CONVENTION_JSON = """{
  "ambiguity": null,
  "annotations": [],
  "answers": [
    "No"
  ],
  "bbox": {
    "page": 0,
    "x0": 0.0,
    "x1": 0.0,
    "y0": 0.0,
    "y1": 0.0
  },
  "canonical_name": null,
  "control_type": "single_select",
  "edited_by_human": false,
  "field_id": "f-ec2d878fed3a7a77",
  "human_value": null,
  "label_text": "Q",
  "normalizer_id": "none",
  "options": [
    {
      "bbox": null,
      "raw_span": null,
      "selected": null,
      "text": "Yes"
    },
    {
      "bbox": null,
      "raw_span": null,
      "selected": true,
      "text": "No"
    }
  ],
  "provenance": {
    "binding_id": "d1",
    "binding_version": null,
    "derived_confidence": null,
    "heuristic_agreement": [
      "declared_checkbox_convention"
    ],
    "llm_confidence": 0.95,
    "match_score": null,
    "review_flag": false,
    "review_reason": null,
    "source": "llm",
    "verified": null
  },
  "region_ref": "p0:c0:field-row:q",
  "section_path": [],
  "select_all_ref": null,
  "source_elements": [
    "r0:label",
    "r1:yes",
    "r1:mark",
    "r1:no"
  ],
  "tab": "page 0",
  "unresolved_source_refs": [],
  "value_normalized": "No",
  "value_raw": "No"
}"""


def test_declared_convention_on_between_is_untouched():
    """(j) A declared convention still resolves a BETWEEN marker, byte for byte;
    the same convention never touches an auto_select union."""
    convention = [
        CheckboxConvention(tab="*", convention=CHECKBOX_MARK_PRECEDES_OPTION)
    ]
    elements = [
        _element("r0:label", "Q", 0, 0),
        _element("r1:yes", "Yes", 100, 30),
        _element("r1:mark", "X", 120, 30),
        _element("r1:no", "No", 140, 30),
    ]
    field = _run_field(
        elements,
        label="Q",
        cited=["r0:label", "r1:yes", "r1:mark", "r1:no"],
        options=["Yes", "No"],
        control_type=ControlType.SINGLE_SELECT,
        conventions=convention,
    )
    assert to_json(field) == BETWEEN_CONVENTION_JSON

    stacked = _stacked_elements("Pick many", _STACKED3, marks={0, 2})
    union = _run_field(
        stacked,
        label="Pick many",
        cited=_STACKED3_CITED,
        options=_STACKED3,
        conventions=convention,
    )
    assert [o.selected for o in union.options] == [True, False, True]
    assert union.provenance.heuristic_agreement == ["marker_auto_select"]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_shuffled_source_elements_same_selection_and_field_id():
    """(k) Reversing the cited order does not move the selection or the id."""
    elements = _stacked_elements("Pick many", _STACKED3, marks={0, 2})
    forward = _run_field(
        elements,
        label="Pick many",
        cited=_STACKED3_CITED,
        options=_STACKED3,
        region_element="r0:label",
    )
    backward = _run_field(
        elements,
        label="Pick many",
        cited=list(reversed(_STACKED3_CITED)),
        options=_STACKED3,
        region_element="r0:label",
    )
    assert [o.selected for o in forward.options] == [
        o.selected for o in backward.options
    ]
    assert forward.field_id == backward.field_id


# ---------------------------------------------------------------------------
# (l) The gold stacked_multi tab scores selected_ok through tools/score_gold.py
# ---------------------------------------------------------------------------


def _row_col(cell_id: str) -> tuple[int, int]:
    _tab, rc = cell_id.split("!", 1)
    row, col = rc.split(":", 1)
    return int(row), int(col)


def test_gold_stacked_multi_selected_ok(tmp_path):
    from formextract.ingest import ingest

    gold, _manifest, _sheets = make_gold.build_set(
        "dev", make_gold.DEFAULT_SEEDS["dev"], tmp_path / "dev"
    )
    cells = {c["id"]: c for c in gold["cells"]}
    markers = {c["id"] for c in gold["cells"] if c["role"] == "marker"}

    layouts: dict[str, tuple] = {}
    for tab in gold["tabs"]:
        ing = ingest(tmp_path / "dev" / f"{tab}.xlsx")
        layouts[tab] = (analyze(ing.elements), {e.element_id: e for e in ing.elements})

    fields = []
    for gf in gold["fields"]:
        if "stacked_multi" not in gf["tags"]:
            continue
        tab = gf["tab"]
        layout, by_id = layouts[tab]
        option_ids = list(gf["option_cells"])
        marker_ids = []
        for oid in option_ids:
            row, col = _row_col(oid)
            mid = f"{tab}!{row}:{col - 1}"
            if mid in markers:
                marker_ids.append(mid)
        cited = list(gf["label_cells"]) + option_ids + marker_ids
        refs = [_ref_for(layout, cid) for cid in cited]
        draft = BindingDraft(
            draft_id=gf["field_id_gold"],
            label=" ".join(cells[c]["text"] for c in gf["label_cells"]),
            control_type=ControlType.MULTI_SELECT,
            bbox=BBox(),
            options=[
                Option(
                    text=cells[cid]["text"],
                    selected=(cid in gf["selected_option_cells"]),
                )
                for cid in option_ids
            ],
            answers=[],
            region_id=refs[0].region_id,
            source_refs=refs,
            confidence=0.9,
            tab=tab,
        )
        fields.extend(
            drafts_to_fields([draft], layout=layout, elements_by_id=by_id, tabs=[tab])
        )

    record = {"fields": [to_dict(f) for f in fields]}
    gold_index = score_gold.Gold(gold)
    predicted, stray, unresolved = score_gold._predicted_fields(
        gold_index, score_gold._extract_fields(record)
    )
    result = score_gold.score_document(gold_index, predicted, stray, unresolved, None)
    tag = result["per_tag"]["stacked_multi"]
    assert tag["gold_fields"] > 0
    assert tag["selected_total"] == tag["gold_fields"]
    assert tag["selected_ok"] == tag["gold_fields"]


# ---------------------------------------------------------------------------
# (m) mutation cases, each with the anti-vacuity triple
# ---------------------------------------------------------------------------


def _patch(monkeypatch, name, replacement):
    """Replace ``resolve.<name>``, assert the seam exists, and log every call."""
    assert hasattr(R, name), f"seam {name!r} is missing"
    calls: list = []

    def patched(*args, **kwargs):
        calls.append(True)
        return replacement(*args, **kwargs)

    monkeypatch.setattr(R, name, patched)
    assert getattr(R, name) is patched
    return calls


def test_mutation_union_reverted_to_first_marker(monkeypatch):
    elements = _stacked_elements("Pick many", _STACKED3, marks={0, 2})
    correct = _run_field(
        elements, label="Pick many", cited=_STACKED3_CITED, options=_STACKED3
    )
    assert [o.selected for o in correct.options] == [True, False, True]

    real = R._right_only_option_texts

    def first_only(right_only, elements_by_id):
        texts, declined = real(right_only, elements_by_id)
        return texts[:1], declined

    calls = _patch(monkeypatch, "_right_only_option_texts", first_only)
    observed = _run_field(
        elements, label="Pick many", cited=_STACKED3_CITED, options=_STACKED3
    )
    assert calls, "the mutation was never reached"
    assert [o.selected for o in observed.options] == [True, False, False]
    assert [o.selected for o in observed.options] != [
        o.selected for o in correct.options
    ]


def test_mutation_dedup_removed(monkeypatch):
    elements = [
        _element("r0:label", "Pick many", 0, 0),
        _element("r1:mark0", "X", 80, 30),
        _element("r1:mark1", "X", 90, 30),
        _element("r1:opt", "Option A", 100, 30),
    ]
    cited = ["r0:label", "r1:mark0", "r1:mark1", "r1:opt"]
    correct = _decision(elements, label="Pick many", cited=cited, options=["Option A"])
    assert correct.option_texts == ["Option A"]

    def no_dedup(right_only, elements_by_id):
        texts: list[str] = []
        declined: list[str] = []
        for c in right_only:
            candidate = elements_by_id.get(c.right_candidate_element_ids[0])
            text = strip_marks(candidate.text) if candidate is not None else None
            if not text:
                declined.append(c.marker_element_id)
                continue
            texts.append(text)
        return texts, declined

    calls = _patch(monkeypatch, "_right_only_option_texts", no_dedup)
    observed = _decision(elements, label="Pick many", cited=cited, options=["Option A"])
    assert calls, "the mutation was never reached"
    assert observed.option_texts == ["Option A", "Option A"]
    assert observed.option_texts != correct.option_texts


def test_mutation_between_short_circuit_moved_after_the_union(monkeypatch):
    elements, cited, options = _short_circuit_fixture("between")
    correct = _run_field(elements, label="Q", cited=cited, options=options)
    assert correct.ambiguity is not None
    assert correct.ambiguity.reason == "between_options"

    calls = _patch(
        monkeypatch, "_between_competing_short_circuit", lambda between, competing: None
    )
    observed = _run_field(elements, label="Q", cited=cited, options=options)
    assert calls, "the mutation was never reached"
    assert observed.ambiguity is None
    assert observed.ambiguity != correct.ambiguity
    # the union now wins: the right-only union selects the stacked candidate
    assert observed.options[-1].selected is True


def test_mutation_cross_check_compares_only_the_first_option(monkeypatch):
    elements = _stacked_elements("Pick many", _STACKED3, marks={0, 2})
    correct = _run_field(
        elements,
        label="Pick many",
        cited=_STACKED3_CITED,
        options=_STACKED3,
        model_selected={"Option A"},
    )
    assert correct.provenance.review_flag is True

    real = R._selections_disagree

    def first_only(model_selected, geometric_selected):
        return real(model_selected, geometric_selected[:1])

    calls = _patch(monkeypatch, "_selections_disagree", first_only)
    observed = _run_field(
        elements,
        label="Pick many",
        cited=_STACKED3_CITED,
        options=_STACKED3,
        model_selected={"Option A"},
    )
    assert calls, "the mutation was never reached"
    assert observed.provenance.review_flag is False
    assert observed.provenance.review_flag != correct.provenance.review_flag
