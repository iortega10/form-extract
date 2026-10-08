"""0.6.0-K1: the resolver's one-option ``single`` -> ``text``/``bool`` rule.

Hermetic. A tiny workbook gives a real layout and lattice; the drafts are built
through the two real parse paths (``parse_drafts`` for JSON, ``parse_lines_response``
for lines) and run through the shared ``apply_kind_rule``. Expected values are
hand-derived from ``docs/design/0.6.0-kind-rule-spec.md`` (Amendment A).

The last section covers ``tools/replay_kind_rule.py`` on a synthetic dump built
from the canned perfect response; those two tests run the real dev gold set, so
they are not tiny.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook

REPO = Path(__file__).resolve().parents[1]
for _root in (REPO, REPO / "tools"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

import gold_lines  # noqa: E402
import make_gold  # noqa: E402
import replay_kind_rule  # noqa: E402
import score_gold  # noqa: E402

from formextract.ingest import ingest
from formextract.layout import analyze
from formextract.model import (
    BBox,
    BindingDraft,
    ControlType,
    ElementRef,
    Option,
)
from formextract.resolve import (
    KIND_RULE_ID,
    LLMResponse,
    apply_kind_rule,
    parse_drafts,
    parse_lines_response,
)
from formextract.rows import build_all_rows

SHEET = "S"


def _workbook(path, grid):
    """Write ``grid`` (row -> [(col_index, value), ...]) into one sheet."""
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    for r, cells in enumerate(grid, start=1):
        for col, value in cells:
            ws.cell(row=r, column=col, value=value)
    wb.save(path)
    wb.close()
    return path


def _fixture(path):
    """Ingest + analyze, and return the pieces ``apply_kind_rule`` needs."""
    ing = ingest(path)
    layout = analyze(ing.elements)
    by_id = {e.element_id: e for e in ing.elements}
    lattice = build_all_rows(layout, by_id)[0]
    return ing, layout, by_id, lattice


def _ref(lattice, cell_id) -> ElementRef:
    region_id, band_id, segment_index = lattice.refs_by_element[cell_id]
    return ElementRef(
        region_id=region_id, band_id=band_id, segment_index=segment_index
    )


def _seg(lattice, cell_id) -> str:
    row = lattice.row_of_element(cell_id)
    return f"{row}.{lattice.rows[row].elements.index(cell_id)}"


def _apply(drafts, layout, by_id):
    apply_kind_rule(
        drafts,
        layout=layout,
        elements_by_id=by_id,
        non_answer_element_ids=set(),
    )
    return drafts


# --------------------------------------------------------------------------
# The three conditions, each with its negative.
# --------------------------------------------------------------------------

def test_a_lone_one_option_single_is_rederived_as_text(tmp_path):
    """Condition 1+2 pass and no other draft shares the row -> ``text``."""
    path = _workbook(tmp_path / "lone.xlsx", [[(1, "Lone choice"), (3, "Only")]])
    _ing, layout, by_id, lattice = _fixture(path)
    draft = BindingDraft(
        draft_id="d1",
        label="Lone choice",
        control_type=ControlType.SINGLE_SELECT,
        bbox=BBox(),
        options=[Option(text="Only", selected=None)],
        source_refs=[_ref(lattice, "S!1:1"), _ref(lattice, "S!1:3")],
        option_refs=(_ref(lattice, "S!1:3"),),
        tab=SHEET,
    )
    _apply([draft], layout, by_id)

    assert draft.control_type is ControlType.TEXT
    assert draft.answers == ["Only"]
    assert draft.options == []
    assert draft.answer_refs == (_ref(lattice, "S!1:3"),)
    assert draft.stated_control_type == "single_select"
    assert draft.kind_rule == KIND_RULE_ID


def test_two_options_are_unchanged(tmp_path):
    """Condition 2 fails with two option elements -> the kind is kept."""
    path = _workbook(
        tmp_path / "two.xlsx", [[(1, "Pick one"), (3, "Red"), (4, "Blue")]]
    )
    _ing, layout, by_id, lattice = _fixture(path)
    draft = BindingDraft(
        draft_id="d1",
        label="Pick one",
        control_type=ControlType.SINGLE_SELECT,
        bbox=BBox(),
        options=[Option(text="Red", selected=None), Option(text="Blue", selected=None)],
        source_refs=[_ref(lattice, "S!1:1"), _ref(lattice, "S!1:3")],
        tab=SHEET,
    )
    _apply([draft], layout, by_id)
    assert draft.control_type is ControlType.SINGLE_SELECT
    assert [o.text for o in draft.options] == ["Red", "Blue"]
    assert draft.kind_rule is None


def test_a_shared_row_is_unchanged(tmp_path):
    """Condition 3 fails when another draft occupies the row -> unchanged.

    This is the ``side_by_side`` shape; it is also the falsifiability guard for
    condition 3 (drop the condition and this pair would be re-derived).
    """
    path = _workbook(
        tmp_path / "shared.xlsx",
        [[(1, "Left"), (3, "One"), (6, "Right"), (8, "Two")]],
    )
    _ing, layout, by_id, lattice = _fixture(path)
    left = BindingDraft(
        draft_id="d-left",
        label="Left",
        control_type=ControlType.SINGLE_SELECT,
        bbox=BBox(),
        options=[Option(text="One", selected=None)],
        source_refs=[_ref(lattice, "S!1:1"), _ref(lattice, "S!1:3")],
        tab=SHEET,
    )
    right = BindingDraft(
        draft_id="d-right",
        label="Right",
        control_type=ControlType.SINGLE_SELECT,
        bbox=BBox(),
        options=[Option(text="Two", selected=None)],
        source_refs=[_ref(lattice, "S!1:6"), _ref(lattice, "S!1:8")],
        tab=SHEET,
    )
    _apply([left, right], layout, by_id)
    assert left.control_type is ControlType.SINGLE_SELECT
    assert right.control_type is ControlType.SINGLE_SELECT
    assert left.kind_rule is None and right.kind_rule is None


def test_stated_multi_text_and_bool_are_unchanged(tmp_path):
    """Condition 1 fails for a stated ``multi_select``/``text``/``bool``.

    The ``multi_select`` case is the falsifiability guard for condition 1 (widen
    the rule to ``multi_select`` and this fails).
    """
    path = _workbook(tmp_path / "kinds.xlsx", [[(1, "Lone choice"), (3, "Only")]])
    _ing, layout, by_id, lattice = _fixture(path)
    refs = [_ref(lattice, "S!1:1"), _ref(lattice, "S!1:3")]
    for control in (ControlType.MULTI_SELECT, ControlType.TEXT, ControlType.BOOL):
        draft = BindingDraft(
            draft_id=f"d-{control.value}",
            label="Lone choice",
            control_type=control,
            bbox=BBox(),
            options=[Option(text="Only", selected=None)],
            source_refs=refs,
            tab=SHEET,
        )
        _apply([draft], layout, by_id)
        assert draft.control_type is control, control
        assert draft.kind_rule is None, control


def test_a_draft_that_already_states_an_answer_is_unchanged(tmp_path):
    """A1: a one-option ``single`` that carries answer evidence is not a typed value."""
    path = _workbook(tmp_path / "answered.xlsx", [[(1, "Lone choice"), (3, "Only")]])
    _ing, layout, by_id, lattice = _fixture(path)
    refs = [_ref(lattice, "S!1:1"), _ref(lattice, "S!1:3")]
    with_answers = BindingDraft(
        draft_id="d-answers",
        label="Lone choice",
        control_type=ControlType.SINGLE_SELECT,
        bbox=BBox(),
        options=[Option(text="Only", selected=None)],
        answers=["Waived"],
        source_refs=refs,
        tab=SHEET,
    )
    with_selection = BindingDraft(
        draft_id="d-selected",
        label="Lone choice",
        control_type=ControlType.SINGLE_SELECT,
        bbox=BBox(),
        options=[Option(text="Only", selected=True)],
        source_refs=refs,
        tab=SHEET,
    )
    _apply([with_answers, with_selection], layout, by_id)
    assert with_answers.control_type is ControlType.SINGLE_SELECT
    assert with_selection.control_type is ControlType.SINGLE_SELECT
    assert with_answers.kind_rule is None and with_selection.kind_rule is None


def test_the_marker_clause_rederives_bool(tmp_path):
    """A one-option ``single`` whose row holds a classified marker -> ``bool``."""
    path = _workbook(
        tmp_path / "marked.xlsx", [[(1, "Agree"), (3, "X"), (4, "Only")]]
    )
    _ing, layout, by_id, lattice = _fixture(path)
    draft = BindingDraft(
        draft_id="d1",
        label="Agree",
        control_type=ControlType.SINGLE_SELECT,
        bbox=BBox(),
        options=[Option(text="Only", selected=None)],
        source_refs=[_ref(lattice, "S!1:1"), _ref(lattice, "S!1:4")],
        tab=SHEET,
    )
    _apply([draft], layout, by_id)
    assert draft.control_type is ControlType.BOOL
    assert draft.kind_rule == KIND_RULE_ID
    # the bool path keeps the option list; only the kind flips
    assert [o.text for o in draft.options] == ["Only"]


def test_an_ascii_bracket_box_is_not_a_marker(tmp_path):
    """A5 limit: literal ``[ ]`` is not in the layout's mark vocabulary."""
    path = _workbook(
        tmp_path / "ascii.xlsx", [[(1, "Agree"), (3, "[ ] I consent")]]
    )
    _ing, layout, by_id, lattice = _fixture(path)
    draft = BindingDraft(
        draft_id="d1",
        label="Agree",
        control_type=ControlType.SINGLE_SELECT,
        bbox=BBox(),
        options=[Option(text="[ ] I consent", selected=None)],
        source_refs=[_ref(lattice, "S!1:1"), _ref(lattice, "S!1:3")],
        option_refs=(_ref(lattice, "S!1:3"),),
        tab=SHEET,
    )
    _apply([draft], layout, by_id)
    assert draft.control_type is ControlType.TEXT
    assert draft.answers == ["[ ] I consent"]


# --------------------------------------------------------------------------
# Both contracts agree.
# --------------------------------------------------------------------------

def test_both_contracts_re_derive_the_same_way(tmp_path):
    """The JSON parse path and the lines path give the same re-derived draft."""
    path = _workbook(tmp_path / "both.xlsx", [[(1, "Lone choice"), (3, "Only")]])
    _ing, layout, by_id, lattice = _fixture(path)

    import json

    json_refs = [
        {
            "region_id": _ref(lattice, "S!1:1").region_id,
            "band_id": _ref(lattice, "S!1:1").band_id,
            "segment_index": _ref(lattice, "S!1:1").segment_index,
        },
        {
            "region_id": _ref(lattice, "S!1:3").region_id,
            "band_id": _ref(lattice, "S!1:3").band_id,
            "segment_index": _ref(lattice, "S!1:3").segment_index,
        },
    ]
    json_drafts = parse_drafts(
        json.dumps(
            {
                "fields": [
                    {
                        "label": "Lone choice",
                        "control_type": "single_select",
                        "options": [{"text": "Only", "selected": None}],
                        "answer": [],
                        "source_elements": json_refs,
                    }
                ]
            }
        )
    )
    for draft in json_drafts:
        draft.tab = SHEET

    from formextract.resolve import ProjectionChunk

    chunk = ProjectionChunk(key=SHEET, text="")
    response = LLMResponse(
        text=f"single L={_seg(lattice, 'S!1:1')} O={_seg(lattice, 'S!1:3')}\nend",
        model="canned",
        params={},
        finish_reason="stop",
    )
    line_drafts, _dispositions, errors, _stats = parse_lines_response(
        chunk,
        lattice,
        response,
        layout=layout,
        elements_by_id=by_id,
        non_answer_element_ids=set(),
    )
    assert errors == []
    for draft in line_drafts:
        draft.tab = SHEET

    _apply(json_drafts, layout, by_id)
    _apply(line_drafts, layout, by_id)
    assert len(json_drafts) == len(line_drafts) == 1
    for draft in (*json_drafts, *line_drafts):
        assert draft.control_type is ControlType.TEXT, draft.label
        assert draft.answers == ["Only"], draft.label
        assert draft.options == [], draft.label
        assert draft.kind_rule == KIND_RULE_ID


# --------------------------------------------------------------------------
# Provenance: the resolver writes the two new keys only when the rule fired.
# --------------------------------------------------------------------------

def test_provenance_is_written_only_when_the_rule_fires():
    """A re-derived draft's ``Field.provenance`` carries the stated kind + rule."""
    from formextract.resolve import drafts_to_fields

    def _field(draft):
        return drafts_to_fields([draft])[0]

    re_derived = BindingDraft(
        draft_id="d1",
        label="Lone choice",
        control_type=ControlType.TEXT,
        bbox=BBox(),
        answers=["Only"],
        kind_rule=KIND_RULE_ID,
        stated_control_type="single_select",
    )
    untouched = BindingDraft(
        draft_id="d2",
        label="Another",
        control_type=ControlType.TEXT,
        bbox=BBox(),
        answers=["x"],
    )
    fired = _field(re_derived).provenance
    assert fired.stated_control_type == "single_select"
    assert fired.kind_rule == KIND_RULE_ID
    assert _field(untouched).provenance.kind_rule is None
    assert _field(untouched).provenance.stated_control_type is None


def test_kind_rule_id_is_exported():
    from formextract import KIND_RULE_VERSION
    from formextract.model import KIND_RULE_VERSION as MODEL_KIND_RULE_VERSION

    assert KIND_RULE_ID == "one_option_single"
    assert KIND_RULE_VERSION == MODEL_KIND_RULE_VERSION == "1"


# --------------------------------------------------------------------------
# tools/replay_kind_rule.py: the reviewer's offline replay, on a synthetic dump
# built from the canned perfect response. Not hermetic-tiny: it runs the real
# dev gold set through the lines contract once and re-scores the dump.
# --------------------------------------------------------------------------

def _perfect_dump(tmp_path_factory, version: int = 3):
    base = tmp_path_factory.mktemp(f"replay-dev-v{version}")
    gold, _, _ = make_gold.build_set(
        "dev", make_gold.DEFAULT_SEEDS["dev"], base, gold_version=version
    )
    gl = gold_lines.GoldLines(base, gold)
    fields = gl.run(gl.perfect(), tmp_path_factory.mktemp(f"replay-work-v{version}"))
    return base, gold, fields


def _lone_text_field(replayer, fields) -> int:
    """Index of a ``text`` field alone on a marker-free lattice row."""
    for index, field in enumerate(fields):
        if field.get("control_type") != "text":
            continue
        tab = score_gold._tab_of(field)
        mine = replayer._rows_of(tab, field)
        if not mine or (mine & replayer.marker_rows.get(tab, set())):
            continue
        if any(
            j != index
            and score_gold._tab_of(other) == tab
            and (replayer._rows_of(tab, other) & mine)
            for j, other in enumerate(fields)
        ):
            continue
        return index
    raise AssertionError("no lone marker-free text field in the perfect dump")


def test_the_replay_tool_changes_nothing_on_the_perfect_dump(tmp_path_factory):
    """Every gold field already matches, so the rule fires on none of them."""
    base, gold, fields = _perfect_dump(tmp_path_factory)
    result = replay_kind_rule.replay(base, {"fields": fields}, gold)

    assert result["fired"] == 0
    off = result["off"]["score"]["overall"]
    on = result["on"]["score"]["overall"]
    assert off["matched"] == off["gold_fields"] == 244
    assert off["precision"] == off["recall"] == 1.0
    assert on == off  # a rule that fired on nothing cannot move a counter


def test_the_replay_tool_restores_a_swapped_text_field(tmp_path_factory):
    """Swap one lone text field to a lone one-option ``single``; the rule undoes it.

    The dump is the perfect response, so the swap costs exactly one kind
    confusion with the rule off; the rule re-derives the field back to ``text``
    with the cited cell as its answer, restoring strict 1.0.
    """
    base, gold, fields = _perfect_dump(tmp_path_factory)
    index = _lone_text_field(replay_kind_rule.Replay(base, gold), fields)
    answer = (fields[index].get("answers") or [None])[0]

    swapped = copy.deepcopy({"fields": fields})
    field = swapped["fields"][index]
    field["control_type"] = "single_select"
    field["options"] = [{"text": answer, "selected": None}]
    field["answers"] = []

    result = replay_kind_rule.replay(base, swapped, gold)

    assert result["fired"] == 1
    off = result["off"]["score"]["overall"]
    on = result["on"]["score"]["overall"]
    assert off["matched"] == 243 and off["kind_confusions"] == 1
    assert on["matched"] == on["gold_fields"] == 244
    assert on["precision"] == on["recall"] == 1.0
    restored = result["on"]["fields"][index]
    assert restored["control_type"] == "text"
    assert restored["options"] == []
    assert restored["answers"] == [answer]
