#!/usr/bin/env python3
"""Synthetic gold-set generator for the 0.6.0 line-contract work (turn G).

Openpyxl only: this module must never import ``formextract`` (a test asserts
that), so ground truth is a property of the workbook cells and of nothing else.
Gold is keyed by the xlsx element ids the ingest emits, ``<sheet>!<row>:<col>``
(verified against ``formextract/ingest/xlsx.py``), so it is independent of
bands, regions, layout and any model.

Deterministic: the same ``--seed`` and ``--set`` produce byte-identical .xlsx
files and gold JSON. openpyxl stamps ``docProps/core.xml`` with the wall clock
and the zip entries with mtime, so the workbook is written to a buffer, the
clock fields are pinned and the archive is repacked with fixed metadata.

Writes one ``.xlsx`` per tab, one gold JSON and one manifest per set, and (with
``--canned``) perfect + mutated record dumps with their expected counter moves.
``--canned-lines`` writes the same thing for the 0.6.0 line contract, in cell-id
form: the response grammar with ``{Sheet!r:c}`` in place of ``row.seg``.

Usage::

    python tools/make_gold.py --set dev --seed 20260101 --out DIR
    python tools/make_gold.py --set dev --seed 20260101 --out DIR --canned DIR/canned
    python tools/make_gold.py --set dev --seed 20260101 --out DIR --canned-lines DIR/canned
    python tools/make_gold.py --set dev --gold-version 2 --out DIR   # the frozen v2 schema
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import re
import zipfile
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill

GOLD_VERSION = 3
#: The gold-set versions the generator can reproduce. ``2`` is frozen: its tabs
#: and its gold JSON are byte-identical to what the previous turns shipped (a
#: committed sha256 table asserts the tab bytes). ``3`` is the current default
#: and adds the kind cue below. ``4`` is opt-in and rebuilds the ``matrix`` tag
#: as an ordinary mark grid: a header row of ``MATRIX_OPTIONS`` option cells and,
#: per row, one label plus one ``marker`` cell under exactly one of those
#: headers. The gold rows carry ``expected_ambiguous`` (a mark under a header has
#: no ``mark_precedes_option`` neighbour, so the resolver cannot decide it), and
#: only the tabs that hold a matrix differ from their v3 bytes.
GOLD_VERSIONS = (2, 3, 4)

#: The clock is pinned so regeneration is byte-identical; the zip is repacked
#: with a fixed date_time so openpyxl's mtime never leaks into the bytes.
_FIXED_TS = (1980, 1, 1, 0, 0, 0)
_CREATED = b'<dcterms:created xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:created>'
_MODIFIED = b'<dcterms:modified xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:modified>'

#: The closed structure-tag vocabulary. Field tags carry at least one per gold
#: field; the three disposition tags belong to gold dispositions instead.
FIELD_TAGS = (
    "yes_no_row",
    "dense_multi",
    "stacked_multi",
    "side_by_side",
    "label_two_rows",
    "label_two_cells",
    "text_field",
    "typed_value",
    "no_glyph_answer",
    "grid_50",
    "matrix",
    "merged_tall",
    "gutter_column",
    "non_answer_column",
    # Not a structure: a cross-cutting tag on every field of a mixed tab, so the
    # mixed set reports as a whole and each structure tag includes its mixed
    # instances.
    "mixed_tab",
)
DISPOSITION_TAGS = ("header_row", "note_row", "skip_row")
#: Gold dispositions are stored by their line kind; the tags above name them.
DISPOSITION_TAG_VALUE = {"header_row": "hdr", "note_row": "note", "skip_row": "skip"}
TAGS = FIELD_TAGS + DISPOSITION_TAGS

#: The closed cue vocabulary (gold v3). The label wording makes a field's kind
#: readable from the sheet alone, because a bare question under a typed answer or
#: a multi-option grid leaves ``single``/``multi``/``bool``/``text`` a coin flip.
KIND_CUES = (
    "plain_question",
    "select_all_that_apply",
    "choose_one",
    "imperative",
    "noun_phrase",
)

#: One cue per structure tag, by the field's gold kind. The affirm/dissent rows
#: (``yes_no_row``, ``gutter_column``) keep the plain question -- their two
#: options already read as a yes/no, so no cue is needed and any suffix would
#: only blur them. Every other multi field takes the select-all cue, every other
#: single field the choose-one cue, and the text fields are rewritten away from a
#: question (imperative). The typed-value rows are a ``bool`` under a status word
#: and keep the plain question too: a noun phrase there reads as a text field, so
#: the sheet's own wording would provoke the very kind confusion the vocabulary
#: exists to avoid. ``noun_phrase`` stays in `KIND_CUES` but is not assigned to a
#: tag for that reason.
TAG_KIND_CUE = {
    "yes_no_row": "plain_question",
    "gutter_column": "plain_question",
    "side_by_side": "choose_one",
    "matrix": "choose_one",
    "no_glyph_answer": "choose_one",
    "dense_multi": "select_all_that_apply",
    "stacked_multi": "select_all_that_apply",
    "grid_50": "select_all_that_apply",
    "non_answer_column": "select_all_that_apply",
    "text_field": "imperative",
    "label_two_rows": "imperative",
    "label_two_cells": "imperative",
    "merged_tall": "imperative",
    "typed_value": "plain_question",
}

#: The fixed words the cue templates add to a label (never a pool word). They are
#: kept out of the prompt-variant examples on purpose: a cue must be readable
#: from the sheet, never copyable out of a prompt (a test asserts the two word
#: sets are disjoint and that this list is exactly what the templates introduce).
CUE_TEMPLATE_WORDS = (
    "select",
    "all",
    "that",
    "apply",
    "choose",
    "one",
    "the",
    "of",
)

#: Two disjoint word pools. Every label and option string is built only from
#: its own pool, so the dev and held-out vocabularies share no label or option
#: string (a test asserts the intersection is empty). No pool word is a tag.
VOCAB = {
    "dev": {
        "verb": [
            "inspect", "calibrate", "lubricate", "torque", "stencil",
            "anodize", "deburr", "etch", "sweep", "purge",
        ],
        "noun": [
            "grommet", "spindle", "flange", "gasket", "bracket", "ratchet",
            "cogwheel", "pulley", "toggle", "latch", "bushing", "shim",
            "ferrule", "clevis", "gudgeon", "trunnion",
        ],
        "adj": [
            "upper", "lower", "inner", "outer", "forward", "aft", "port",
            "starboard",
        ],
        "yesno": ("Affirm", "Dissent"),
        "typed": ("Recorded", "Waived"),
    },
    "heldout": {
        "verb": [
            "survey", "fettle", "wrangle", "chamfer", "sinter", "quench",
            "braze", "ream", "swage", "hob",
        ],
        "noun": [
            "zephyr", "quokka", "nimbus", "vellum", "cinder", "tessera",
            "gossamer", "plinth", "kershaw", "lumen", "marlin", "onyx",
            "pumice", "quill", "sable", "tallow",
        ],
        "adj": [
            "dorsal", "ventral", "keystone", "primeval", "vermilion",
            "cerulean", "saffron", "gilded",
        ],
        "yesno": ("Conform", "Rebuff"),
        "typed": ("Noted", "Forborne"),
    },
}

MARKER = "X"
_FILL = "FFEEDD"

#: Per-set default seeds. Held-out is a different seed AND a different
#: vocabulary so prompts tuned on dev cannot memorise the held-out fixtures.
DEFAULT_SEEDS = {"dev": 20260101, "heldout": 20260202}

_KIND_TO_CONTROL = {
    "single": "single_select",
    "multi": "multi_select",
    "bool": "bool",
    "text": "text",
}
_CONTROL_TO_KIND = {v: k for k, v in _KIND_TO_CONTROL.items()}


def _distinct(rng: random.Random, k: int, make) -> list[str]:
    """`k` distinct strings from `make(rng)`, in draw order (deterministic)."""
    out: list[str] = []
    seen: set[str] = set()
    while len(out) < k:
        value = make(rng)
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


class Words:
    """Deterministic text builder for one tab."""

    def __init__(self, pool: dict, rng: random.Random, version: int = GOLD_VERSION):
        self.pool = pool
        self.rng = rng
        self.version = version

    def label(self, cue: str | None = None) -> str:
        """The label for one structure, with its kind cue from gold v3 on.

        Every cue draws the same four words in the same order as the plain
        question, so a v3 tab differs from its v2 twin only in the label strings:
        the cells, their positions, their order and the generated selections are
        all unchanged (the cue is a wording change, not a data change). Gold v2
        ignores the cue, so its output stays byte-identical.
        """
        p, rng = self.pool, self.rng
        verb = rng.choice(p["verb"])
        noun = rng.choice(p["noun"])
        adj = rng.choice(p["adj"])
        noun2 = rng.choice(p["noun"])
        if self.version < 3 or cue in (None, "plain_question"):
            return f"{verb.capitalize()} {noun} {adj} {noun2}?"
        if cue == "select_all_that_apply":
            return f"{verb.capitalize()} {noun} {adj} {noun2}? (select all that apply)"
        if cue == "choose_one":
            return f"{verb.capitalize()} {noun} {adj} {noun2}? (choose one)"
        if cue == "imperative":
            return f"{verb.capitalize()} the {adj} {noun}"
        if cue == "noun_phrase":
            return f"{noun.capitalize()} of the {adj} {noun2}"
        raise ValueError(f"unknown kind cue: {cue!r}")

    def options(self, k: int) -> list[str]:
        p, rng = self.pool, self.rng
        return _distinct(
            rng, k, lambda r: f"{r.choice(p['adj']).capitalize()} {r.choice(p['noun'])}"
        )

    def yesno(self) -> tuple[str, str]:
        return self.pool["yesno"]

    def text_value(self) -> str:
        p, rng = self.pool, self.rng
        return f"{rng.choice(p['noun']).capitalize()} {rng.choice(p['adj'])} {rng.randint(100, 999)}"

    def typed_value(self) -> str:
        return self.rng.choice(self.pool["typed"])

    def annotation(self) -> str:
        p, rng = self.pool, self.rng
        return f"see {rng.choice(p['noun'])} {rng.choice(p['adj'])} appendix"

    def header(self) -> str:
        p, rng = self.pool, self.rng
        return f"{rng.choice(p['noun']).capitalize()} {rng.choice(p['noun']).capitalize()} Intake"

    def note(self) -> str:
        p, rng = self.pool, self.rng
        return f"{rng.choice(p['adj']).capitalize()} {rng.choice(p['noun'])} value unclear"

    def skip(self) -> str:
        p, rng = self.pool, self.rng
        return f"{rng.choice(p['noun']).capitalize()} legend"


class Sheet:
    """One tab: writes cells and accumulates gold cells, fields, dispositions."""

    def __init__(self, ws, words: Words, version: int = GOLD_VERSION):
        self.ws = ws
        self.name = ws.title
        self.words = words
        self.version = version
        self.cells: list[dict] = []
        self.fields: list[dict] = []
        self.dispositions: list[dict] = []
        self._field_n = 0

    def put(self, row: int, col: int, text: str, role: str, *, bold=False, fill=False) -> str:
        cell = self.ws.cell(row=row, column=col, value=text)
        if bold:
            cell.font = Font(bold=True)
        if fill:
            cell.fill = PatternFill(fill_type="solid", start_color=_FILL, end_color=_FILL)
        cid = f"{self.name}!{row}:{col}"
        self.cells.append({"id": cid, "tab": self.name, "text": str(text), "role": role})
        return cid

    def label(self, row: int, col: int, tag: str) -> str:
        """Write a label cell with the kind cue its structure tag calls for."""
        return self.put(row, col, self.words.label(TAG_KIND_CUE[tag]), "label")

    def dispose(self, row: int, col: int, text: str, kind: str) -> str:
        cid = self.put(row, col, text, kind)
        self.dispositions.append({"tab": self.name, "cell": cid, "disposition": kind})
        return cid

    @staticmethod
    def _cue_for(tags) -> str | None:
        for tag in tags:
            if tag in TAG_KIND_CUE:
                return TAG_KIND_CUE[tag]
        return None

    def field(
        self,
        *,
        kind: str,
        label_cells,
        option_cells=(),
        answer_cells=(),
        annotation_cells=(),
        selected_option_cells=(),
        answer_text=None,
        tags=(),
        expected_ambiguous=False,
    ) -> str:
        self._field_n += 1
        fid = f"{self.name}!f{self._field_n}"
        entry = {
            "field_id_gold": fid,
            "tab": self.name,
            "kind": kind,
            "label_cells": list(label_cells),
            "option_cells": list(option_cells),
            "answer_cells": list(answer_cells),
            "annotation_cells": list(annotation_cells),
            "selected_option_cells": list(selected_option_cells),
            "answer_text": answer_text,
            "tags": list(tags),
            "expected_ambiguous": bool(expected_ambiguous),
        }
        if self.version >= 3:
            # The cue the label was written with, so the scorer and a reader can
            # tell a field whose kind is determinable from one that is not.
            entry["kind_cue"] = self._cue_for(tags)
        self.fields.append(entry)
        return fid


def _put_options(sheet: Sheet, row: int, c0: int, options) -> tuple[list[str], list[str]]:
    """Write (marker?, option) column pairs. Returns (option_ids, selected_ids)."""
    option_ids: list[str] = []
    selected: list[str] = []
    col = c0
    for text, is_sel in options:
        if is_sel:
            sheet.put(row, col, MARKER, "marker")
        col += 1
        oid = sheet.put(row, col, text, "option")
        option_ids.append(oid)
        if is_sel:
            selected.append(oid)
        col += 1
    return option_ids, selected


# --------------------------------------------------------------------------
# Tab builders. Each returns nothing; it fills the Sheet with gold + cells.
# --------------------------------------------------------------------------

YES_NO_N = 14
DENSE_N = 10
DENSE_OPTIONS = 5
STACKED_N = 6
STACKED_OPTIONS = 3
SIDE_ROWS = 7
LTR_N = 8
LTC_N = 11
TEXT_N = 10
TYPED_N = 10
NO_GLYPH_N = 8
GRID_N = 3
GRID_ROWS = 5
GRID_COLS = 10
MATRIX_N = 6
#: gold v4: the number of column options a matrix header row carries (K). A
#: matrix row is a label plus one ``marker`` under exactly one of these.
MATRIX_OPTIONS = 3
MERGED_N = 6
GUTTER_N = 6
NON_ANSWER_N = 8


def _tab_yes_no(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(YES_NO_N):
        lid = s.label(row, 1, "yes_no_row")
        yn = s.words.yesno()
        sel = s.words.rng.randint(0, 1)
        opts = [(yn[0], sel == 0), (yn[1], sel == 1)]
        option_ids, selected = _put_options(s, row, 3, opts)
        s.field(
            kind="single",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["yes_no_row"],
        )
        row += 1
    s.dispose(row, 1, s.words.note(), "note")
    row += 1
    s.dispose(row, 1, s.words.skip(), "skip")


def _tab_dense(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(DENSE_N):
        lid = s.label(row, 1, "dense_multi")
        texts = s.words.options(DENSE_OPTIONS)
        opts = [(t, s.words.rng.random() < 0.4) for t in texts]
        option_ids, selected = _put_options(s, row, 3, opts)
        s.field(
            kind="multi",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["dense_multi"],
        )
        row += 1
    s.dispose(row, 1, s.words.note(), "note")
    row += 1
    s.dispose(row, 1, s.words.skip(), "skip")


def _tab_stacked(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(STACKED_N):
        lid = s.label(row, 1, "stacked_multi")
        row += 1
        texts = s.words.options(STACKED_OPTIONS)
        option_ids: list[str] = []
        selected: list[str] = []
        for text in texts:
            is_sel = s.words.rng.random() < 0.4
            if is_sel:
                s.put(row, 3, MARKER, "marker")
            oid = s.put(row, 4, text, "option")
            option_ids.append(oid)
            if is_sel:
                selected.append(oid)
            row += 1
        s.field(
            kind="multi",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["stacked_multi"],
        )


def _tab_side_by_side(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(SIDE_ROWS):
        for label_col, opt_col in ((1, 3), (8, 10)):
            lid = s.label(row, label_col, "side_by_side")
            texts = s.words.options(2)
            sel = s.words.rng.randint(0, 1)
            opts = [(texts[0], sel == 0), (texts[1], sel == 1)]
            option_ids, selected = _put_options(s, row, opt_col, opts)
            s.field(
                kind="single",
                label_cells=[lid],
                option_cells=option_ids,
                selected_option_cells=selected,
                tags=["side_by_side"],
            )
        row += 1
    # One row where two fields share a single competing marker: the marker at
    # column 6 is a right candidate of the left field's option and a left
    # candidate of the right field, so the outcome is ambiguous BY DESIGN and
    # selected_ok is not penalised for these two fields.
    left_label = s.label(row, 1, "side_by_side")
    left_marker = s.put(row, 3, MARKER, "marker")
    left_option = s.put(row, 4, s.words.options(1)[0], "option")
    s.put(row, 6, MARKER, "marker")
    right_label = s.label(row, 8, "side_by_side")
    right_option = s.put(row, 10, s.words.options(1)[0], "option")
    s.field(
        kind="single",
        label_cells=[left_label],
        option_cells=[left_option],
        selected_option_cells=[left_option],
        tags=["side_by_side"],
        expected_ambiguous=True,
    )
    s.field(
        kind="single",
        label_cells=[right_label],
        option_cells=[right_option],
        tags=["side_by_side"],
        expected_ambiguous=True,
    )
    del left_marker  # marker cell is accounted for by Sheet.put; name only for clarity
    s.dispose(row + 1, 1, s.words.note(), "note")
    s.dispose(row + 2, 1, s.words.skip(), "skip")


def _tab_label_two_rows(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(LTR_N):
        l1 = s.label(row, 1, "label_two_rows")
        l2 = s.label(row + 1, 1, "label_two_rows")
        text = s.words.text_value()
        ans = s.put(row, 3, text, "answer")
        s.field(
            kind="text",
            label_cells=[l1, l2],
            answer_cells=[ans],
            answer_text=text,
            tags=["label_two_rows"],
        )
        row += 2
    s.dispose(row, 1, s.words.note(), "note")
    row += 1
    s.dispose(row, 1, s.words.skip(), "skip")


def _tab_label_two_cells(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(LTC_N):
        first = s.words.label(TAG_KIND_CUE["label_two_cells"])
        second = s.words.label(TAG_KIND_CUE["label_two_cells"])
        l1 = s.put(row, 1, first, "label")
        l2 = s.put(row, 2, second, "label")
        text = s.words.text_value()
        ans = s.put(row, 4, text, "answer")
        s.field(
            kind="text",
            label_cells=[l1, l2],
            answer_cells=[ans],
            answer_text=text,
            tags=["label_two_cells"],
        )
        row += 1


def _tab_text_and_typed(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(TEXT_N):
        lid = s.label(row, 1, "text_field")
        text = s.words.text_value()
        ans = s.put(row, 3, text, "answer")
        s.field(
            kind="text",
            label_cells=[lid],
            answer_cells=[ans],
            answer_text=text,
            tags=["text_field"],
        )
        row += 1
    for _ in range(TYPED_N):
        lid = s.label(row, 1, "typed_value")
        ans = s.put(row, 3, s.words.typed_value(), "answer")
        s.field(kind="bool", label_cells=[lid], answer_cells=[ans], tags=["typed_value"])
        row += 1
    s.dispose(row, 1, s.words.note(), "note")


def _tab_no_glyph(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(NO_GLYPH_N):
        lid = s.label(row, 1, "no_glyph_answer")
        texts = s.words.options(3)
        sel = s.words.rng.randint(0, 2)
        option_ids: list[str] = []
        col = 3
        styled: str | None = None
        for i, text in enumerate(texts):
            if i == sel:
                oid = s.put(row, col, text, "option", bold=True, fill=True)
                styled = oid
            else:
                oid = s.put(row, col, text, "option")
            option_ids.append(oid)
            col += 1
        s.field(
            kind="single",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=[styled] if styled else [],
            tags=["no_glyph_answer"],
        )
        row += 1
    s.dispose(row, 1, s.words.note(), "note")
    row += 1
    s.dispose(row, 1, s.words.skip(), "skip")


def _tab_grid(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(GRID_N):
        lid = s.label(row, 1, "grid_50")
        row += 1
        texts = s.words.options(GRID_ROWS * GRID_COLS)
        option_ids: list[str] = []
        for i in range(GRID_ROWS):
            for j in range(GRID_COLS):
                oid = s.put(row + i, 2 + j, texts[i * GRID_COLS + j], "option")
                option_ids.append(oid)
        selected = [oid for k, oid in enumerate(option_ids) if k % 7 == 0]
        s.field(
            kind="multi",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["grid_50"],
        )
        row += GRID_ROWS


def _tab_matrix_and_merged(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    if s.version >= 4:
        header_ids = [
            s.put(row, 2 + j, text, "option")
            for j, text in enumerate(s.words.options(MATRIX_OPTIONS))
        ]
        row += 1
        for _ in range(MATRIX_N):
            lid = s.label(row, 1, "matrix")
            sel = s.words.rng.randint(0, MATRIX_OPTIONS - 1)
            s.put(row, 2 + sel, MARKER, "marker")
            s.field(
                kind="single",
                label_cells=[lid],
                option_cells=header_ids,
                selected_option_cells=[header_ids[sel]],
                tags=["matrix"],
                expected_ambiguous=True,
            )
            row += 1
    else:
        header = s.put(row, 2, s.words.header(), "option")
        row += 1
        for _ in range(MATRIX_N):
            lid = s.label(row, 1, "matrix")
            ans = s.put(row, 2, s.words.typed_value(), "answer")
            s.field(
                kind="single",
                label_cells=[lid],
                option_cells=[header],
                answer_cells=[ans],
                tags=["matrix"],
            )
            row += 1
    for _ in range(MERGED_N):
        s.ws.merge_cells(start_row=row, start_column=1, end_row=row + 1, end_column=2)
        lid = s.label(row, 1, "merged_tall")
        text = s.words.text_value()
        ans = s.put(row, 4, text, "answer")
        s.field(
            kind="text",
            label_cells=[lid],
            answer_cells=[ans],
            answer_text=text,
            tags=["merged_tall"],
        )
        row += 2


def _tab_gutter_and_non_answer(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(GUTTER_N):
        lid = s.label(row, 2, "gutter_column")
        yn = s.words.yesno()
        sel = s.words.rng.randint(0, 1)
        opts = [(yn[0], sel == 0), (yn[1], sel == 1)]
        option_ids, selected = _put_options(s, row, 4, opts)
        s.field(
            kind="single",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["gutter_column"],
        )
        row += 1
    for _ in range(NON_ANSWER_N):
        lid = s.label(row, 1, "non_answer_column")
        opts = [(t, s.words.rng.random() < 0.5) for t in s.words.options(2)]
        option_ids, selected = _put_options(s, row, 3, opts)
        ann = s.put(row, 12, s.words.annotation(), "annotation")
        s.field(
            kind="multi",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            annotation_cells=[ann],
            tags=["non_answer_column"],
        )
        row += 1
    s.dispose(row, 1, s.words.skip(), "skip")


# --------------------------------------------------------------------------
# Mixed tabs: blocks of the existing structure kinds, shuffled per tab, with
# hdr/note/skip separators. Every field also carries the `mixed_tab` tag, so a
# structure tag's score includes its mixed instances and `mixed_tab` reports the
# mixed set as a whole.
# --------------------------------------------------------------------------

#: Appended after the homogeneous tabs, fed by a separate RNG stream
#: (``_MIXED_SEED_BASE``) so adding them cannot perturb the homogeneous tabs.
MIXED_DEV_TABS = ("Dev13", "Dev14", "Dev15", "Dev16")
MIXED_HELD_TABS = ("Hold07", "Hold08")
MIXED_TABS = MIXED_DEV_TABS + MIXED_HELD_TABS
_MIXED_SEED_BASE = 500
_MIXED_TAG = "mixed_tab"

#: The palette: kind -> instances written. Every mixed tab always gets a 12-row
#: yes/no run and a 50-option grid (the two long-form shapes) plus the three
#: multi-row kinds (so a tab lands in the 60-100 row band), and a seeded four of
#: the rest, so every mixed tab has >= 9 distinct structure kinds.
_MIXED_COUNTS = {
    "yes_no_row": 12,
    "grid_50": 1,
    "stacked_multi": 4,
    "label_two_rows": 3,
    "merged_tall": 3,
    "dense_multi": 1,
    "label_two_cells": 1,
    "text_field": 1,
    "typed_value": 1,
    "no_glyph_answer": 1,
    "matrix": 1,
    "gutter_column": 1,
    "non_answer_column": 1,
    "side_by_side": 1,
    "ambiguous": 1,
}
_MIXED_ALWAYS = (
    "yes_no_row",
    "grid_50",
    "stacked_multi",
    "label_two_rows",
    "merged_tall",
    "ambiguous",
)
_MIXED_OPTIONAL = (
    "dense_multi",
    "label_two_cells",
    "text_field",
    "typed_value",
    "no_glyph_answer",
    "matrix",
    "gutter_column",
    "non_answer_column",
    "side_by_side",
)
_MIXED_OPTIONAL_PICK = 4


def _blk_header(s: Sheet, row: int) -> int:
    s.dispose(row, 1, s.words.header(), "hdr")
    return row + 1


def _blk_note(s: Sheet, row: int) -> int:
    s.dispose(row, 1, s.words.note(), "note")
    return row + 1


def _blk_skip(s: Sheet, row: int) -> int:
    s.dispose(row, 1, s.words.skip(), "skip")
    return row + 1


def _blk_yes_no(s: Sheet, row: int, n: int) -> int:
    for _ in range(n):
        lid = s.label(row, 1, "yes_no_row")
        yn = s.words.yesno()
        sel = s.words.rng.randint(0, 1)
        option_ids, selected = _put_options(
            s, row, 3, [(yn[0], sel == 0), (yn[1], sel == 1)]
        )
        s.field(
            kind="single",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["yes_no_row", _MIXED_TAG],
        )
        row += 1
    return row


def _blk_dense(s: Sheet, row: int, n: int) -> int:
    for _ in range(n):
        lid = s.label(row, 1, "dense_multi")
        opts = [(t, s.words.rng.random() < 0.4) for t in s.words.options(DENSE_OPTIONS)]
        option_ids, selected = _put_options(s, row, 3, opts)
        s.field(
            kind="multi",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["dense_multi", _MIXED_TAG],
        )
        row += 1
    return row


def _blk_stacked(s: Sheet, row: int, n: int) -> int:
    for _ in range(n):
        lid = s.label(row, 1, "stacked_multi")
        row += 1
        option_ids: list[str] = []
        selected: list[str] = []
        for text in s.words.options(STACKED_OPTIONS):
            is_sel = s.words.rng.random() < 0.4
            if is_sel:
                s.put(row, 3, MARKER, "marker")
            oid = s.put(row, 4, text, "option")
            option_ids.append(oid)
            if is_sel:
                selected.append(oid)
            row += 1
        s.field(
            kind="multi",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["stacked_multi", _MIXED_TAG],
        )
    return row


def _blk_side_by_side(s: Sheet, row: int, n: int) -> int:
    for _ in range(n):
        for label_col, opt_col in ((1, 3), (8, 10)):
            lid = s.label(row, label_col, "side_by_side")
            texts = s.words.options(2)
            sel = s.words.rng.randint(0, 1)
            option_ids, selected = _put_options(
                s, row, opt_col, [(texts[0], sel == 0), (texts[1], sel == 1)]
            )
            s.field(
                kind="single",
                label_cells=[lid],
                option_cells=option_ids,
                selected_option_cells=selected,
                tags=["side_by_side", _MIXED_TAG],
            )
        row += 1
    return row


def _blk_label_two_rows(s: Sheet, row: int, n: int) -> int:
    for _ in range(n):
        l1 = s.label(row, 1, "label_two_rows")
        l2 = s.label(row + 1, 1, "label_two_rows")
        text = s.words.text_value()
        ans = s.put(row, 3, text, "answer")
        s.field(
            kind="text",
            label_cells=[l1, l2],
            answer_cells=[ans],
            answer_text=text,
            tags=["label_two_rows", _MIXED_TAG],
        )
        row += 2
    return row


def _blk_label_two_cells(s: Sheet, row: int, n: int) -> int:
    for _ in range(n):
        l1 = s.label(row, 1, "label_two_cells")
        l2 = s.label(row, 2, "label_two_cells")
        text = s.words.text_value()
        ans = s.put(row, 4, text, "answer")
        s.field(
            kind="text",
            label_cells=[l1, l2],
            answer_cells=[ans],
            answer_text=text,
            tags=["label_two_cells", _MIXED_TAG],
        )
        row += 1
    return row


def _blk_text(s: Sheet, row: int, n: int) -> int:
    for _ in range(n):
        lid = s.label(row, 1, "text_field")
        text = s.words.text_value()
        ans = s.put(row, 3, text, "answer")
        s.field(
            kind="text",
            label_cells=[lid],
            answer_cells=[ans],
            answer_text=text,
            tags=["text_field", _MIXED_TAG],
        )
        row += 1
    return row


def _blk_typed(s: Sheet, row: int, n: int) -> int:
    for _ in range(n):
        lid = s.label(row, 1, "typed_value")
        ans = s.put(row, 3, s.words.typed_value(), "answer")
        s.field(
            kind="bool",
            label_cells=[lid],
            answer_cells=[ans],
            tags=["typed_value", _MIXED_TAG],
        )
        row += 1
    return row


def _blk_no_glyph(s: Sheet, row: int, n: int) -> int:
    for _ in range(n):
        lid = s.label(row, 1, "no_glyph_answer")
        texts = s.words.options(3)
        sel = s.words.rng.randint(0, 2)
        option_ids: list[str] = []
        styled: str | None = None
        col = 3
        for i, text in enumerate(texts):
            if i == sel:
                oid = s.put(row, col, text, "option", bold=True, fill=True)
                styled = oid
            else:
                oid = s.put(row, col, text, "option")
            option_ids.append(oid)
            col += 1
        s.field(
            kind="single",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=[styled] if styled else [],
            tags=["no_glyph_answer", _MIXED_TAG],
        )
        row += 1
    return row


def _blk_grid(s: Sheet, row: int, n: int) -> int:
    for _ in range(n):
        lid = s.label(row, 1, "grid_50")
        row += 1
        texts = s.words.options(GRID_ROWS * GRID_COLS)
        option_ids: list[str] = []
        for i in range(GRID_ROWS):
            for j in range(GRID_COLS):
                option_ids.append(
                    s.put(row + i, 2 + j, texts[i * GRID_COLS + j], "option")
                )
        selected = [oid for k, oid in enumerate(option_ids) if k % 7 == 0]
        s.field(
            kind="multi",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["grid_50", _MIXED_TAG],
        )
        row += GRID_ROWS
    return row


def _blk_matrix(s: Sheet, row: int, n: int) -> int:
    if s.version >= 4:
        header_ids = [
            s.put(row, 2 + j, text, "option")
            for j, text in enumerate(s.words.options(MATRIX_OPTIONS))
        ]
        row += 1
        for _ in range(n):
            lid = s.label(row, 1, "matrix")
            sel = s.words.rng.randint(0, MATRIX_OPTIONS - 1)
            s.put(row, 2 + sel, MARKER, "marker")
            s.field(
                kind="single",
                label_cells=[lid],
                option_cells=header_ids,
                selected_option_cells=[header_ids[sel]],
                tags=["matrix", _MIXED_TAG],
                expected_ambiguous=True,
            )
            row += 1
        return row
    header = s.put(row, 2, s.words.header(), "option")
    row += 1
    for _ in range(n):
        lid = s.label(row, 1, "matrix")
        ans = s.put(row, 2, s.words.typed_value(), "answer")
        s.field(
            kind="single",
            label_cells=[lid],
            option_cells=[header],
            answer_cells=[ans],
            tags=["matrix", _MIXED_TAG],
        )
        row += 1
    return row


def _blk_merged(s: Sheet, row: int, n: int) -> int:
    for _ in range(n):
        s.ws.merge_cells(start_row=row, start_column=1, end_row=row + 1, end_column=2)
        lid = s.label(row, 1, "merged_tall")
        text = s.words.text_value()
        ans = s.put(row, 4, text, "answer")
        s.field(
            kind="text",
            label_cells=[lid],
            answer_cells=[ans],
            answer_text=text,
            tags=["merged_tall", _MIXED_TAG],
        )
        row += 2
    return row


def _blk_gutter(s: Sheet, row: int, n: int) -> int:
    for _ in range(n):
        lid = s.label(row, 2, "gutter_column")
        yn = s.words.yesno()
        sel = s.words.rng.randint(0, 1)
        option_ids, selected = _put_options(
            s, row, 4, [(yn[0], sel == 0), (yn[1], sel == 1)]
        )
        s.field(
            kind="single",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["gutter_column", _MIXED_TAG],
        )
        row += 1
    return row


def _blk_non_answer(s: Sheet, row: int, n: int) -> int:
    for _ in range(n):
        lid = s.label(row, 1, "non_answer_column")
        opts = [(t, s.words.rng.random() < 0.5) for t in s.words.options(2)]
        option_ids, selected = _put_options(s, row, 3, opts)
        ann = s.put(row, 12, s.words.annotation(), "annotation")
        s.field(
            kind="multi",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            annotation_cells=[ann],
            tags=["non_answer_column", _MIXED_TAG],
        )
        row += 1
    return row


def _blk_ambiguous(s: Sheet, row: int) -> int:
    """Two yes/no-shaped fields sharing one competing between-marker.

    The single marker at column 6 is between the left field's option and the
    right field's option, so the resolution is genuinely ambiguous *by design*:
    the declared `mark_precedes_option` convention cannot decide it. The gold
    marks both fields `expected_ambiguous` so the by-design outcome does not read
    as a regression (the same shape as the side-by-side tab's shared-marker row).
    """
    yn = s.words.yesno()
    left_label = s.label(row, 1, "yes_no_row")
    s.put(row, 3, MARKER, "marker")
    left_option = s.put(row, 4, yn[0], "option")
    s.put(row, 6, MARKER, "marker")
    right_label = s.label(row, 8, "yes_no_row")
    right_option = s.put(row, 10, yn[1], "option")
    s.field(
        kind="single",
        label_cells=[left_label],
        option_cells=[left_option],
        selected_option_cells=[left_option],
        tags=["yes_no_row", _MIXED_TAG],
        expected_ambiguous=True,
    )
    s.field(
        kind="single",
        label_cells=[right_label],
        option_cells=[right_option],
        tags=["yes_no_row", _MIXED_TAG],
        expected_ambiguous=True,
    )
    return row + 1


def _mixed_block_builders(s: Sheet) -> dict:
    c = _MIXED_COUNTS
    return {
        "yes_no_row": lambda r: _blk_yes_no(s, r, c["yes_no_row"]),
        "grid_50": lambda r: _blk_grid(s, r, c["grid_50"]),
        "stacked_multi": lambda r: _blk_stacked(s, r, c["stacked_multi"]),
        "label_two_rows": lambda r: _blk_label_two_rows(s, r, c["label_two_rows"]),
        "merged_tall": lambda r: _blk_merged(s, r, c["merged_tall"]),
        "dense_multi": lambda r: _blk_dense(s, r, c["dense_multi"]),
        "label_two_cells": lambda r: _blk_label_two_cells(s, r, c["label_two_cells"]),
        "text_field": lambda r: _blk_text(s, r, c["text_field"]),
        "typed_value": lambda r: _blk_typed(s, r, c["typed_value"]),
        "no_glyph_answer": lambda r: _blk_no_glyph(s, r, c["no_glyph_answer"]),
        "matrix": lambda r: _blk_matrix(s, r, c["matrix"]),
        "gutter_column": lambda r: _blk_gutter(s, r, c["gutter_column"]),
        "non_answer_column": lambda r: _blk_non_answer(s, r, c["non_answer_column"]),
        "side_by_side": lambda r: _blk_side_by_side(s, r, c["side_by_side"]),
        "ambiguous": lambda r: _blk_ambiguous(s, r),
    }


def _build_mixed(s: Sheet) -> None:
    builders = _mixed_block_builders(s)
    kinds = list(_MIXED_ALWAYS) + s.words.rng.sample(
        _MIXED_OPTIONAL, _MIXED_OPTIONAL_PICK
    )
    s.words.rng.shuffle(kinds)
    separators = (_blk_header, _blk_note, _blk_skip)
    row = _blk_header(s, 1)
    for i, kind in enumerate(kinds):
        row = builders[kind](row)
        row = separators[i % len(separators)](s, row)
    row = _blk_note(s, row)
    _blk_skip(s, row)


def _tab_mixed(s: Sheet) -> None:
    _build_mixed(s)


DEV_TABS = (
    ("Dev01", _tab_yes_no),
    ("Dev02", _tab_dense),
    ("Dev03", _tab_stacked),
    ("Dev04", _tab_side_by_side),
    ("Dev05", _tab_label_two_rows),
    ("Dev06", _tab_label_two_cells),
    ("Dev07", _tab_text_and_typed),
    ("Dev08", _tab_no_glyph),
    ("Dev09", _tab_grid),
    ("Dev10", _tab_grid),
    ("Dev11", _tab_matrix_and_merged),
    ("Dev12", _tab_gutter_and_non_answer),
    ("Dev13", _tab_mixed),
    ("Dev14", _tab_mixed),
    ("Dev15", _tab_mixed),
    ("Dev16", _tab_mixed),
)


def _tab_hold_plain(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(6):
        lid = s.label(row, 1, "yes_no_row")
        yn = s.words.yesno()
        sel = s.words.rng.randint(0, 1)
        opts = [(yn[0], sel == 0), (yn[1], sel == 1)]
        option_ids, selected = _put_options(s, row, 3, opts)
        s.field(
            kind="single",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["yes_no_row"],
        )
        row += 1
    for _ in range(6):
        lid = s.label(row, 1, "dense_multi")
        opts = [(t, s.words.rng.random() < 0.4) for t in s.words.options(4)]
        option_ids, selected = _put_options(s, row, 3, opts)
        s.field(
            kind="multi",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["dense_multi"],
        )
        row += 1


def _tab_hold_stacked_side(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(6):
        lid = s.label(row, 1, "stacked_multi")
        row += 1
        option_ids: list[str] = []
        selected: list[str] = []
        for text in s.words.options(3):
            is_sel = s.words.rng.random() < 0.4
            if is_sel:
                s.put(row, 3, MARKER, "marker")
            oid = s.put(row, 4, text, "option")
            option_ids.append(oid)
            if is_sel:
                selected.append(oid)
            row += 1
        s.field(
            kind="multi",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["stacked_multi"],
        )
    for _ in range(3):
        for label_col, opt_col in ((1, 3), (8, 10)):
            lid = s.label(row, label_col, "side_by_side")
            texts = s.words.options(2)
            sel = s.words.rng.randint(0, 1)
            option_ids, selected = _put_options(
                s, row, opt_col, [(texts[0], sel == 0), (texts[1], sel == 1)]
            )
            s.field(
                kind="single",
                label_cells=[lid],
                option_cells=option_ids,
                selected_option_cells=selected,
                tags=["side_by_side"],
            )
        row += 1
    left_label = s.label(row, 1, "side_by_side")
    left_option = s.put(row, 4, s.words.options(1)[0], "option")
    s.put(row, 6, MARKER, "marker")
    right_label = s.label(row, 8, "side_by_side")
    right_option = s.put(row, 10, s.words.options(1)[0], "option")
    s.field(
        kind="single",
        label_cells=[left_label],
        option_cells=[left_option],
        selected_option_cells=[left_option],
        tags=["side_by_side"],
        expected_ambiguous=True,
    )
    s.field(
        kind="single",
        label_cells=[right_label],
        option_cells=[right_option],
        tags=["side_by_side"],
        expected_ambiguous=True,
    )


def _tab_hold_labels_text(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(3):
        l1 = s.label(row, 1, "label_two_rows")
        l2 = s.label(row + 1, 1, "label_two_rows")
        text = s.words.text_value()
        ans = s.put(row, 3, text, "answer")
        s.field(
            kind="text",
            label_cells=[l1, l2],
            answer_cells=[ans],
            answer_text=text,
            tags=["label_two_rows"],
        )
        row += 2
    for _ in range(3):
        l1 = s.label(row, 1, "label_two_cells")
        l2 = s.label(row, 2, "label_two_cells")
        text = s.words.text_value()
        ans = s.put(row, 4, text, "answer")
        s.field(
            kind="text",
            label_cells=[l1, l2],
            answer_cells=[ans],
            answer_text=text,
            tags=["label_two_cells"],
        )
        row += 1
    for _ in range(4):
        lid = s.label(row, 1, "text_field")
        text = s.words.text_value()
        ans = s.put(row, 3, text, "answer")
        s.field(
            kind="text",
            label_cells=[lid],
            answer_cells=[ans],
            answer_text=text,
            tags=["text_field"],
        )
        row += 1
    for _ in range(4):
        lid = s.label(row, 1, "typed_value")
        ans = s.put(row, 3, s.words.typed_value(), "answer")
        s.field(kind="bool", label_cells=[lid], answer_cells=[ans], tags=["typed_value"])
        row += 1


def _tab_hold_no_glyph_non_answer(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(3):
        lid = s.label(row, 1, "no_glyph_answer")
        texts = s.words.options(3)
        sel = s.words.rng.randint(0, 2)
        option_ids: list[str] = []
        styled = None
        col = 3
        for i, text in enumerate(texts):
            if i == sel:
                oid = s.put(row, col, text, "option", bold=True, fill=True)
                styled = oid
            else:
                oid = s.put(row, col, text, "option")
            option_ids.append(oid)
            col += 1
        s.field(
            kind="single",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=[styled] if styled else [],
            tags=["no_glyph_answer"],
        )
        row += 1
    for _ in range(3):
        lid = s.label(row, 1, "non_answer_column")
        opts = [(t, s.words.rng.random() < 0.5) for t in s.words.options(2)]
        option_ids, selected = _put_options(s, row, 3, opts)
        ann = s.put(row, 12, s.words.annotation(), "annotation")
        s.field(
            kind="multi",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            annotation_cells=[ann],
            tags=["non_answer_column"],
        )
        row += 1


def _tab_hold_grid_matrix(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(2):
        lid = s.label(row, 1, "grid_50")
        row += 1
        texts = s.words.options(GRID_ROWS * GRID_COLS)
        option_ids: list[str] = []
        for i in range(GRID_ROWS):
            for j in range(GRID_COLS):
                oid = s.put(row + i, 2 + j, texts[i * GRID_COLS + j], "option")
                option_ids.append(oid)
        selected = [oid for k, oid in enumerate(option_ids) if k % 9 == 0]
        s.field(
            kind="multi",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["grid_50"],
        )
        row += GRID_ROWS
    if s.version >= 4:
        header_ids = [
            s.put(row, 2 + j, text, "option")
            for j, text in enumerate(s.words.options(MATRIX_OPTIONS))
        ]
        row += 1
        for _ in range(3):
            lid = s.label(row, 1, "matrix")
            sel = s.words.rng.randint(0, MATRIX_OPTIONS - 1)
            s.put(row, 2 + sel, MARKER, "marker")
            s.field(
                kind="single",
                label_cells=[lid],
                option_cells=header_ids,
                selected_option_cells=[header_ids[sel]],
                tags=["matrix"],
                expected_ambiguous=True,
            )
            row += 1
        return
    header = s.put(row, 2, s.words.header(), "option")
    row += 1
    for _ in range(3):
        lid = s.label(row, 1, "matrix")
        ans = s.put(row, 2, s.words.typed_value(), "answer")
        s.field(
            kind="single",
            label_cells=[lid],
            option_cells=[header],
            answer_cells=[ans],
            tags=["matrix"],
        )
        row += 1


def _tab_hold_merged_gutter(s: Sheet) -> None:
    row = 1
    s.dispose(row, 1, s.words.header(), "hdr")
    row += 1
    for _ in range(3):
        s.ws.merge_cells(start_row=row, start_column=1, end_row=row + 1, end_column=2)
        lid = s.label(row, 1, "merged_tall")
        text = s.words.text_value()
        ans = s.put(row, 4, text, "answer")
        s.field(
            kind="text",
            label_cells=[lid],
            answer_cells=[ans],
            answer_text=text,
            tags=["merged_tall"],
        )
        row += 2
    for _ in range(3):
        lid = s.label(row, 2, "gutter_column")
        yn = s.words.yesno()
        sel = s.words.rng.randint(0, 1)
        option_ids, selected = _put_options(
            s, row, 4, [(yn[0], sel == 0), (yn[1], sel == 1)]
        )
        s.field(
            kind="single",
            label_cells=[lid],
            option_cells=option_ids,
            selected_option_cells=selected,
            tags=["gutter_column"],
        )
        row += 1


HELDOUT_TABS = (
    ("Hold01", _tab_hold_plain),
    ("Hold02", _tab_hold_stacked_side),
    ("Hold03", _tab_hold_labels_text),
    ("Hold04", _tab_hold_no_glyph_non_answer),
    ("Hold05", _tab_hold_grid_matrix),
    ("Hold06", _tab_hold_merged_gutter),
    ("Hold07", _tab_mixed),
    ("Hold08", _tab_mixed),
)

#: A different base for each tab so text does not repeat, still seed-driven.
_SEED_STEP = 101


def _sheet_rng(seed: int, index: int) -> random.Random:
    return random.Random(seed + index * _SEED_STEP)


def _write_workbook(wb, path: Path) -> bytes:
    """Serialise `wb` deterministically to `path` and return the bytes."""
    wb.properties.creator = "make_gold"
    wb.properties.lastModifiedBy = "make_gold"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    src = zipfile.ZipFile(buf)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "docProps/core.xml":
                data = re.sub(
                    rb"<dcterms:created[^>]*>[^<]*</dcterms:created>", _CREATED, data
                )
                data = re.sub(
                    rb"<dcterms:modified[^>]*>[^<]*</dcterms:modified>", _MODIFIED, data
                )
            new_info = zipfile.ZipInfo(info.filename, date_time=_FIXED_TS)
            new_info.compress_type = zipfile.ZIP_DEFLATED
            new_info.external_attr = info.external_attr
            dst.writestr(new_info, data)
    payload = out.getvalue()
    Path(path).write_bytes(payload)
    return payload


def build_set(set_name: str, seed: int, out_dir: Path, gold_version: int = GOLD_VERSION):
    """Write the .xlsx tabs, the gold JSON and the manifest for one set.

    ``gold_version`` selects the gold schema: ``3`` (the default) writes the kind
    cue into every label and records ``kind_cue`` per field; ``2`` is frozen and
    reproduces the previous sets byte for byte. Returns ``(gold, manifest,
    sheets)`` where ``sheets`` is a list of ``(tab_name, Sheet)`` for canned-dump
    generation.
    """
    if gold_version not in GOLD_VERSIONS:
        raise ValueError(f"gold_version must be one of {GOLD_VERSIONS!r}")
    tabs = DEV_TABS if set_name == "dev" else HELDOUT_TABS
    pool = VOCAB[set_name]
    out_dir.mkdir(parents=True, exist_ok=True)

    cells: list[dict] = []
    fields: list[dict] = []
    dispositions: list[dict] = []
    sheets: list[tuple[str, Sheet]] = []
    per_tab_sha: dict[str, str] = {}
    mixed_seen = 0

    for index, (tab_name, builder) in enumerate(tabs):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = tab_name
        # The mixed tabs draw from their own stream, appended after the
        # homogeneous tabs' index-keyed streams, so adding mixed tabs cannot
        # perturb a homogeneous tab's bytes.
        if tab_name in MIXED_TABS:
            rng = _sheet_rng(seed, _MIXED_SEED_BASE + mixed_seen)
            mixed_seen += 1
        else:
            rng = _sheet_rng(seed, index)
        sheet = Sheet(ws, Words(pool, rng, gold_version), gold_version)
        builder(sheet)
        payload = _write_workbook(wb, out_dir / f"{tab_name}.xlsx")
        wb.close()
        per_tab_sha[tab_name] = hashlib.sha256(payload).hexdigest()
        cells.extend(sheet.cells)
        fields.extend(sheet.fields)
        dispositions.extend(sheet.dispositions)
        sheets.append((tab_name, sheet))

    # Every tab is generated with the same documented convention: the mark
    # precedes its option label. Written exactly in the shape
    # PipelineConfig.checkbox_conventions takes, one selector per tab, so a
    # harness can declare it without translation.
    checkbox_conventions = [
        {
            "tab": name,
            "anchor_pattern": None,
            "convention": "mark_precedes_option",
        }
        for name, _ in tabs
    ]
    gold = {
        "gold_version": gold_version,
        "set": set_name,
        "seed": seed,
        "tabs": [name for name, _ in tabs],
        "checkbox_conventions": checkbox_conventions,
        "cells": cells,
        "fields": fields,
        "dispositions": dispositions,
    }
    tab_counts = {name: sum(1 for f in fields if f["tab"] == name) for name in gold["tabs"]}
    tag_counts = {
        tag: sum(1 for f in fields if tag in f["tags"]) for tag in FIELD_TAGS
    }
    disposition_counts = {
        tag: sum(
            1
            for d in dispositions
            if d["disposition"] == DISPOSITION_TAG_VALUE[tag]
        )
        for tag in DISPOSITION_TAGS
    }
    manifest = {
        "gold_version": gold_version,
        "set": set_name,
        "seed": seed,
        "tabs": gold["tabs"],
        "homogeneous_tabs": [n for n in gold["tabs"] if n not in MIXED_TABS],
        "mixed_tabs": [n for n in gold["tabs"] if n in MIXED_TABS],
        "fields": len(fields),
        "cells": len(cells),
        "tag_counts": tag_counts,
        "disposition_counts": disposition_counts,
        "tab_counts": tab_counts,
        "tab_sha256": per_tab_sha,
    }
    (out_dir / f"gold_{set_name}.json").write_text(
        json.dumps(gold, indent=2, sort_keys=True), encoding="utf-8"
    )
    (out_dir / f"gold_manifest_{set_name}.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return gold, manifest, sheets


# --------------------------------------------------------------------------
# Canned record dumps: a perfect one plus one per mutation.
# --------------------------------------------------------------------------

def _gold_field_to_predicted(field: dict, cells_by_id: dict) -> dict:
    kind = field["kind"]
    label_text = " ".join(cells_by_id[c]["text"] for c in field["label_cells"])
    options = [
        {
            "text": cells_by_id[c]["text"],
            "selected": (
                c in field["selected_option_cells"] if kind in ("single", "multi", "bool") else None
            ),
        }
        for c in field["option_cells"]
    ]
    answers = [field["answer_text"]] if kind == "text" and field["answer_text"] else []
    annotations = [cells_by_id[c]["text"] for c in field["annotation_cells"]]
    source = (
        list(field["label_cells"])
        + list(field["option_cells"])
        + list(field["answer_cells"])
        + list(field["annotation_cells"])
    )
    return {
        "label_text": label_text,
        "control_type": _KIND_TO_CONTROL[kind],
        "options": options,
        "answers": answers,
        "annotations": annotations,
        "source_elements": source,
        "tab": field["tab"],
        "unresolved_source_refs": [],
    }


def perfect_record(gold: dict) -> dict:
    cells_by_id = {c["id"]: c for c in gold["cells"]}
    return {
        "fields": [_gold_field_to_predicted(f, cells_by_id) for f in gold["fields"]],
    }


def _first_tab_with(gold: dict, predicate) -> str:
    for f in gold["fields"]:
        if predicate(f):
            return f["tab"]
    raise ValueError("no matching gold field")


def _row_col(cell_id: str) -> tuple[int, int]:
    _, rc = cell_id.split("!", 1)
    row, col = rc.split(":", 1)
    return int(row), int(col)


def _between_marker_fields(gold: dict) -> list[tuple[int, str]]:
    """``(index, marker_id)`` for every gold field a convention is required for.

    A between-marker field's selected option sits after an option on its left, so
    its marker has option candidates on both sides: only a declared convention
    can decide it. ``expected_ambiguous`` fields are excluded - their ambiguity
    is designed, not a missing declaration.
    """
    cells_by_id = {c["id"]: c for c in gold["cells"]}
    out: list[tuple[int, str]] = []
    for i, f in enumerate(gold["fields"]):
        if f["kind"] not in ("single", "multi", "bool"):
            continue
        if f.get("expected_ambiguous"):
            continue
        option_cols = [_row_col(c)[1] for c in f["option_cells"]]
        if not option_cols:
            continue
        for sel in f["selected_option_cells"]:
            row, col = _row_col(sel)
            marker_id = f"{f['tab']}!{row}:{col - 1}"
            marker = cells_by_id.get(marker_id)
            if marker is not None and marker["role"] == "marker":
                if any(o < col - 1 for o in option_cols):
                    out.append((i, marker_id))
                    break
    return out


def mutations(gold: dict) -> dict[str, dict]:
    """Return ``{mutation_name: record_dump}`` derived from the perfect dump."""
    cells_by_id = {c["id"]: c for c in gold["cells"]}
    perfect = perfect_record(gold)

    def clone() -> dict:
        return json.loads(json.dumps(perfect))

    out: dict[str, dict] = {}

    # drop one field
    rec = clone()
    rec["fields"].pop()
    out["drop_field"] = rec

    # merge two gold fields (from one tab) into a single predicted field.
    tab = gold["fields"][0]["tab"]
    pair = [f for f in gold["fields"] if f["tab"] == tab][:2]
    rec = clone()
    ids = [i for f in pair for i in f["label_cells"] + f["option_cells"]]
    originals = [
        i
        for i, p in enumerate(rec["fields"])
        if p["tab"] == tab and set(p["source_elements"]) & set(ids)
    ]
    for i in sorted(originals, reverse=True):
        rec["fields"].pop(i)
    rec["fields"].append(
        {
            "label_text": " ".join(cells_by_id[i]["text"] for i in pair[0]["label_cells"]),
            "control_type": _KIND_TO_CONTROL[pair[0]["kind"]],
            "options": [],
            "answers": [],
            "annotations": [],
            "source_elements": ids,
            "tab": tab,
            "unresolved_source_refs": [],
        }
    )
    out["merge_two"] = rec

    # split one predicted field into two strict subsets.
    target = next(
        f for f in perfect["fields"] if len(f["options"]) >= 2
    )
    rec = clone()
    idx = rec["fields"].index(target)
    label_ids = [i for i in target["source_elements"] if cells_by_id[i]["role"] == "label"]
    option_ids = [i for i in target["source_elements"] if cells_by_id[i]["role"] == "option"]
    half = len(option_ids) // 2
    part_a = list(label_ids) + option_ids[:half]
    part_b = list(label_ids) + option_ids[half:]
    rec["fields"].pop(idx)
    for part in (part_a, part_b):
        rec["fields"].append(
            {
                "label_text": target["label_text"],
                "control_type": target["control_type"],
                "options": [],
                "answers": [],
                "annotations": [],
                "source_elements": part,
                "tab": target["tab"],
                "unresolved_source_refs": [],
            }
        )
    out["split_one"] = rec

    # wrong kind on one field (ids kept)
    rec = clone()
    victim = rec["fields"][0]
    victim["control_type"] = "bool" if victim["control_type"] != "bool" else "text"
    out["wrong_kind"] = rec

    # every single-select field read as a multi-select: the grouping is
    # untouched, so only the kind counters move.
    rec = clone()
    for field in rec["fields"]:
        if field["control_type"] == "single_select":
            field["control_type"] = "multi_select"
    out["kind_swap"] = rec

    # stray hallucinated id added to one field (identity unchanged)
    rec = clone()
    rec["fields"][0]["source_elements"] = list(rec["fields"][0]["source_elements"]) + [
        f"{rec['fields'][0]['tab']}!999:999"
    ]
    out["stray_id"] = rec

    # an unresolved ref added to one field
    rec = clone()
    rec["fields"][0]["unresolved_source_refs"] = [
        f"{rec['fields'][0]['tab']}!888:888"
    ]
    out["unresolved_ref"] = rec

    # wrong label text on a matched field
    rec = clone()
    rec["fields"][0]["label_text"] = rec["fields"][0]["label_text"] + " epsilon"
    out["wrong_label"] = rec

    # wrong selected option on a field that has options
    rec = clone()
    victim = next(f for f in rec["fields"] if f["options"])
    flags = [o["selected"] for o in victim["options"]]
    victim["options"][0]["selected"] = not flags[0]
    if len(victim["options"]) > 1:
        victim["options"][1]["selected"] = not flags[1]
    out["wrong_selected"] = rec

    # wrong typed answer on a text field
    rec = clone()
    victim = next(f for f in rec["fields"] if f["control_type"] == "text")
    victim["answers"] = [victim["answers"][0] + " zeta" if victim["answers"] else "zeta"]
    out["wrong_answer"] = rec

    # one whole tab's fields missing
    tab = gold["tabs"][0]
    rec = clone()
    rec["fields"] = [f for f in rec["fields"] if f["tab"] != tab]
    out["missing_tab"] = rec

    # empty fields list
    out["empty_fields"] = {"fields": []}

    # duplicate one predicted field
    rec = clone()
    rec["fields"].append(json.loads(json.dumps(rec["fields"][0])))
    out["duplicate_field"] = rec

    # no checkbox convention declared: every between-marker field is ambiguous.
    # Each such field moves out of selected_ok (and
    # selected_ok_under_convention) into unexpected_ambiguous.
    rec = clone()
    for index, marker_id in _between_marker_fields(gold):
        rec["fields"][index]["ambiguity"] = {
            "marker_element_id": marker_id,
            "candidate_element_ids": [],
            "reason": "between_options",
        }
    out["declare_nothing"] = rec

    return out


# --------------------------------------------------------------------------
# Canned LINES responses (the 0.6.0 line contract), in cell-id form.
# --------------------------------------------------------------------------

#: How a canned line names a cell: ``{Sheet!r:c}``, the gold's own element id.
#: The generator may not import ``formextract`` and so cannot know the
#: ``row.seg`` a lattice assigns a cell; ``tests/gold_lines.py`` substitutes each
#: placeholder with that coordinate. Everything else in the line is the response
#: grammar verbatim (section 3.2), so the artifact is the perfect response with
#: the projection coordinates left for the bridge to fill in.
_LINE_KEYS = ("O", "A", "N")


def _line_record(kind: str, label=(), options=(), answer=(), notes=()) -> dict:
    record = {"kind": kind, "L": list(label)}
    for key, cells in (("O", options), ("A", answer), ("N", notes)):
        if cells:
            record[key] = list(cells)
    return record


def _render_line(record: dict) -> str:
    parts = [record["kind"], "L=" + ",".join("{%s}" % c for c in record["L"])]
    for key in _LINE_KEYS:
        cells = record.get(key)
        if cells:
            parts.append(f"{key}=" + ",".join("{%s}" % c for c in cells))
    return " ".join(parts)


def _lines_doc(records_by_tab: dict, *, end: bool = True) -> dict:
    return {
        tab: {"lines": [_render_line(r) for r in records], "end": bool(end)}
        for tab, records in records_by_tab.items()
    }


def _lines_records(gold: dict, *, drop=(), extra=()) -> dict:
    """``{tab: [record, ...]}`` for the gold, minus ``drop`` indices, plus ``extra``.

    Disposition lines come first and field lines last, matching a form read top
    to bottom for the common header case. The order is not significant to the
    resolver, and it makes the section-3.5 cut rule observable: the physical tail
    of a response is always a field line, so a cut costs a field.
    """
    out: dict[str, list[dict]] = {tab: [] for tab in gold["tabs"]}
    for disp in gold["dispositions"]:
        out[disp["tab"]].append(_line_record(disp["disposition"], label=[disp["cell"]]))
    for index, field in enumerate(gold["fields"]):
        if index in drop:
            continue
        out[field["tab"]].append(
            _line_record(
                field["kind"],
                label=field["label_cells"],
                options=field["option_cells"],
                answer=field["answer_cells"],
                notes=field["annotation_cells"],
            )
        )
    for tab, record in extra:
        out[tab].append(record)
    return out


def perfect_lines(gold: dict) -> dict:
    """``{tab: {"lines": [...], "end": True}}``: the perfect response, cell-id form.

    One line per gold field (its kind and its cells in gold order) and one per
    gold disposition; ``L``/``O``/``A``/``N`` carry ``{Sheet!r:c}`` placeholders
    the bridge replaces with the lattice's ``row.seg``. The ``end`` terminator is
    present, so the two sections-3.5 cut rules are both satisfied.
    """
    return _lines_doc(_lines_records(gold))


def line_mutations(gold: dict) -> dict:
    """``{name: lines_doc}``: the section-1 mutations at the line level.

    ``drop_line`` loses one field; ``wrong_kind`` keeps its cells and changes its
    kind; ``merge_two`` fuses two fields' cells into one line; ``split_one``
    splits one line's option cells in two; ``cut_before_end`` drops the ``end``
    terminator with a non-empty tail, so the section-3.5 rule cuts the last line.
    """
    out: dict[str, dict] = {}

    out["drop_line"] = _lines_doc(_lines_records(gold, drop={0}))

    rec = _lines_records(gold)
    lines = rec[gold["tabs"][0]]
    first_field = next(
        i for i, r in enumerate(lines) if r["kind"] in ("single", "multi", "bool", "text")
    )
    victim = lines[first_field]
    victim["kind"] = "bool" if victim["kind"] != "bool" else "text"
    out["wrong_kind"] = _lines_doc(rec)

    tab = gold["fields"][0]["tab"]
    pair_index = [
        i for i, f in enumerate(gold["fields"]) if f["tab"] == tab
    ][:2]
    pair = [gold["fields"][i] for i in pair_index]
    merged = _line_record(
        pair[0]["kind"],
        label=pair[0]["label_cells"],
        options=list(pair[0]["option_cells"]) + list(pair[1]["option_cells"]),
    )
    out["merge_two"] = _lines_doc(
        _lines_records(gold, drop=set(pair_index), extra=[(tab, merged)])
    )

    split = next(f for f in gold["fields"] if len(f["option_cells"]) >= 2)
    half = len(split["option_cells"]) // 2
    parts = (
        _line_record(split["kind"], label=split["label_cells"],
                     options=split["option_cells"][:half]),
        _line_record(split["kind"], label=split["label_cells"],
                     options=split["option_cells"][half:]),
    )
    out["split_one"] = _lines_doc(
        _lines_records(gold, drop={gold["fields"].index(split)},
                       extra=[(split["tab"], p) for p in parts])
    )

    out["cut_before_end"] = _lines_doc(_lines_records(gold), end=False)

    return out


def write_canned_lines(gold: dict, canned_dir: Path) -> None:
    canned_dir.mkdir(parents=True, exist_ok=True)
    set_name = gold["set"]
    doc = {"set": set_name, "perfect": perfect_lines(gold),
           "mutations": line_mutations(gold)}
    (canned_dir / f"lines_{set_name}.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8"
    )


def write_canned(gold: dict, canned_dir: Path) -> None:
    canned_dir.mkdir(parents=True, exist_ok=True)
    set_name = gold["set"]
    perfect = perfect_record(gold)
    (canned_dir / f"perfect_{set_name}.json").write_text(
        json.dumps(perfect, indent=2, sort_keys=True), encoding="utf-8"
    )
    for name, rec in mutations(gold).items():
        (canned_dir / f"{name}_{set_name}.json").write_text(
            json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8"
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", choices=sorted(VOCAB), default="dev")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--canned", type=Path, default=None)
    parser.add_argument(
        "--gold-version",
        type=int,
        choices=GOLD_VERSIONS,
        default=GOLD_VERSION,
        dest="gold_version",
        metavar="{2,3,4}",
        help="gold schema: 3 (default) writes the kind cue into every label and "
        "records kind_cue; 2 is frozen and reproduces the previous sets byte for "
        "byte; 4 rebuilds the matrix tag as a K-option header row with one mark "
        "per row and is opt-in",
    )
    parser.add_argument(
        "--canned-lines",
        type=Path,
        default=None,
        metavar="DIR",
        help="write the 0.6.0 line-contract canned responses (cell-id form) here",
    )
    parser.add_argument(
        "--print-conventions",
        type=Path,
        default=None,
        metavar="GOLD.json",
        help="print the gold's checkbox_conventions as a JSON list to paste into "
        "PipelineConfig(checkbox_conventions=...), then exit",
    )
    args = parser.parse_args(argv)

    if args.print_conventions is not None:
        doc = json.loads(args.print_conventions.read_text(encoding="utf-8"))
        print(json.dumps(doc.get("checkbox_conventions") or [], indent=2))
        return 0
    if args.out is None:
        parser.error("--out is required unless --print-conventions is given")

    seed = args.seed if args.seed is not None else DEFAULT_SEEDS[args.set]
    gold, manifest, _ = build_set(args.set, seed, args.out, args.gold_version)
    if args.canned is not None:
        write_canned(gold, args.canned)
    if args.canned_lines is not None:
        write_canned_lines(gold, args.canned_lines)
    print(
        f"{args.set}: {len(manifest['tabs'])} tabs, "
        f"{manifest['fields']} fields, {manifest['cells']} cells, seed {seed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
