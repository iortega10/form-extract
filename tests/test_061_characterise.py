"""0.6.1-D: a stray mark in a TEXT field's row is inert (a fix, not a
characterisation). The mark must neither null the typed answer nor set an
ambiguity; ``value_raw``/``value_normalized`` follow ``answers``, as they do for
a text field whose row carries no mark. SELECT/MULTI/BOOL decisions are
unchanged: a mark there is the control's own state.

Every case runs through the real path: a ``Pipeline`` run with a scripted
``lines`` client on a synthetic workbook (no model, no network). The rows are::

    mark | label | typed answer          the mark is its own cell
    Rounding | Yes | X | No              a select field beside it

Hand-derived outcomes (glyphs ``X``, ``☒`` (U+2612), ``✓`` (U+2713), with and
without ``mark_follows_option``; the text field is ``Fee Description`` and its
answer ``Administrative charge``):

| row | reply | control | answers | value_raw | ambiguity | review |
|---|---|---|---|---|---|---|
| mark-first text row | ``text L=0.1 A=0.2`` | text | ``[answer]`` | ``answer`` | ``None`` | no flag |
| select ``Rounding \\| Yes \\| X \\| No``, no convention | ``single L=0.0 O=0.1,0.3`` | single_select | ``[]`` | ``None`` | ``between_options`` | AMBIGUOUS_MARK |
| the same select with ``mark_follows_option`` | ``single L=0.0 O=0.1,0.3`` | single_select | ``["Yes"]`` | ``"Yes"`` | ``None`` | no flag |
| answer cell holds the mark only | ``text L=0.0 A=0.1`` | text | ``[]`` | ``None`` | ``None`` | no flag |

Before the fix a mark-first text row came out with ``value_raw is None`` and a
``competing_options``/``convention_unsupported_by_geometry`` ambiguity: the
marker was injected into the options-less TEXT draft, its auto-select was
downgraded to declining, and the value was nulled before the TEXT branch.
"""
from __future__ import annotations

from openpyxl import Workbook

from formextract.model import (
    CHECKBOX_MARK_FOLLOWS_OPTION,
    CheckboxConvention,
    ControlType,
)
from formextract.pipeline import Pipeline, PipelineConfig
from formextract.resolve import LLMResponse
from formextract.store import Store

GLYPHS = {"X": "X", "U+2612": "\u2612", "U+2713": "\u2713"}
FOLLOWS = [CheckboxConvention(tab="Tiny", convention=CHECKBOX_MARK_FOLLOWS_OPTION)]

TEXT_LABEL = "Fee Description"
TEXT_ANSWER = "Administrative charge"
TEXT_LINE = "text L=0.1 A=0.2"
SELECT_LINE = "single L=0.0 O=0.1,0.3"
#: The same select field one lattice row down (a text row above it).
SELECT_LINE_ROW1 = "single L=1.0 O=1.1,1.3"


def _reply(*lines):
    return "\n".join([*lines, "end"])


class _Client:
    """A scripted client: one fixed reply for every prompt (no model)."""

    def __init__(self, text: str):
        self.text = text
        self.prompts: list[str] = []

    def complete(self, prompt, *, model, params):
        self.prompts.append(prompt)
        return LLMResponse(
            text=self.text,
            model=model,
            params=params,
            tokens=1,
            latency_ms=0,
            finish_reason="stop",
        )


def _workbook(tmp_path, rows, name="tiny.xlsx"):
    path = tmp_path / name
    wb = Workbook()
    ws = wb.active
    ws.title = "Tiny"
    for r, cells in enumerate(rows, start=1):
        for col, value in enumerate(cells, start=1):
            if value is not None:
                ws.cell(row=r, column=col, value=value)
    wb.save(path)
    wb.close()
    return path


def _run(tmp_path, path, reply, *, conventions=(), name="store"):
    config = PipelineConfig(
        model="canned",
        output_contract="lines",
        checkbox_conventions=list(conventions),
    )
    return Pipeline(Store(tmp_path / name), _Client(reply), config).run(path)


def _text_field(record):
    fields = [f for f in record.fields if f.control_type is ControlType.TEXT]
    assert len(fields) == 1, [(f.label_text, f.control_type) for f in record.fields]
    return fields[0]


def test_a_stray_mark_is_inert_for_text_at_every_glyph_and_convention(tmp_path):
    """The six combinations: the mark does not touch a TEXT field's answer."""
    for name, glyph in GLYPHS.items():
        for convention_on in (False, True):
            conventions = FOLLOWS if convention_on else ()
            path = _workbook(
                tmp_path,
                [[glyph, TEXT_LABEL, TEXT_ANSWER]],
                name=f"text-{name}-{convention_on}.xlsx",
            )
            record = _run(
                tmp_path,
                path,
                _reply(TEXT_LINE),
                conventions=conventions,
                name=f"store-{name}-{convention_on}",
            )
            field = _text_field(record)
            key = (name, convention_on)
            assert field.label_text == TEXT_LABEL, key
            assert field.answers == [TEXT_ANSWER], key
            assert field.value_raw == TEXT_ANSWER, key
            assert field.value_normalized == TEXT_ANSWER, key
            assert field.options == [], key
            assert field.ambiguity is None, key
            assert field.provenance.review_flag is False, key


def test_the_select_row_beside_it_is_unchanged(tmp_path):
    """A mark on a select row is the control's state; the guard must not reach it.

    No convention: the between marker is ambiguous (``between_options``) and the
    value is null. With ``mark_follows_option`` the resolver selects the option
    the mark follows.
    """
    path = _workbook(
        tmp_path,
        [
            ["X", TEXT_LABEL, TEXT_ANSWER],
            ["Rounding", "Yes", "X", "No"],
        ],
    )

    plain = _run(tmp_path, path, _reply(TEXT_LINE, SELECT_LINE_ROW1), name="plain")
    text = _text_field(plain)
    select = next(f for f in plain.fields if f.control_type is ControlType.SINGLE_SELECT)
    # the text field beside it is fixed...
    assert text.value_raw == TEXT_ANSWER
    assert text.ambiguity is None
    # ...and the select row is exactly what it was: undecided between two options.
    assert select.label_text == "Rounding"
    assert [o.text for o in select.options] == ["Yes", "No"]
    assert all(o.selected is None for o in select.options)
    assert select.answers == []
    assert select.value_raw is None
    assert select.ambiguity is not None
    assert select.ambiguity.reason == "between_options"
    assert select.provenance.review_flag is True

    followed = _run(
        tmp_path,
        path,
        _reply(TEXT_LINE, SELECT_LINE_ROW1),
        conventions=FOLLOWS,
        name="follows",
    )
    select2 = next(
        f for f in followed.fields if f.control_type is ControlType.SINGLE_SELECT
    )
    assert [o.selected for o in select2.options] == [True, None]
    assert select2.value_raw == "Yes"
    assert select2.ambiguity is None


def test_the_guard_widened_to_selects_would_erase_the_ambiguity(tmp_path):
    """Mutation guard: this fails if the TEXT guard is applied to selects.

    The select row's mark must keep its between-options ambiguity. A build that
    scoped the guard to ``control_type in (TEXT, SINGLE_SELECT)`` (or dropped the
    control-type scoping) would leave ``ambiguity is None`` and ``value_raw``
    set here.
    """
    path = _workbook(tmp_path, [["Rounding", "Yes", "X", "No"]])
    record = _run(tmp_path, path, _reply(SELECT_LINE))
    (select,) = record.fields
    assert select.control_type is ControlType.SINGLE_SELECT
    assert select.ambiguity is not None
    assert select.ambiguity.reason == "between_options"
    assert select.value_raw is None


def test_a_mark_inside_the_answer_cell_is_unchanged(tmp_path):
    """The answer cell holds only the mark: no text, so nothing binds.

    The mark is not a separate row cell here, but the same guard applies: the
    field keeps ``value_raw is None`` and no ambiguity.
    """
    path = _workbook(tmp_path, [[TEXT_LABEL, "X"]])
    record = _run(tmp_path, path, "text L=0.0 A=0.1\nend")
    field = _text_field(record)
    assert field.label_text == TEXT_LABEL
    assert field.answers == []
    assert field.value_raw is None
    assert field.value_normalized is None
    assert field.ambiguity is None
    assert field.provenance.review_flag is False


def test_a_mark_sharing_the_answer_cell_text_is_not_a_control(tmp_path):
    """A mark token inside the answer cell's text is stripped, the rest binds."""
    path = _workbook(tmp_path, [[TEXT_LABEL, f"X {TEXT_ANSWER}"]])
    record = _run(tmp_path, path, "text L=0.0 A=0.1\nend")
    field = _text_field(record)
    assert field.answers == [TEXT_ANSWER]
    assert field.value_raw == TEXT_ANSWER
    assert field.ambiguity is None
    assert field.provenance.review_flag is False

