#!/usr/bin/env python3
"""Replay the 0.6.0-K1 one-option ``single`` kind rule on a saved record dump.

Offline, no provider. Reads a gold directory (its ``gold_*.json`` for the gold
document and its ``<tab>.xlsx`` for the row lattice and the layout's classified
markers) and a ``--record-out`` dump written by ``live_probe.py``; scores the
dump as recorded ("rule off") and again after re-deriving every field the rule
fires on ("rule on"); prints the scorer's overall and per-tag counters for both.

The rule is reimplemented at the serialised-field level (the shape the reviewer's
scratch replay used); the resolver is not imported. It fires when a field
(1) states ``single_select``, (2) has exactly one option and no answer evidence,
and (3) shares no lattice row with another field on its tab; a row that holds a
classified marker makes it ``bool``, otherwise ``text`` with the cited option
cell as the answer. This models the shipped rule including its marker clause and
the Amendment-A answer guard, so on a dump where those change nothing it
reproduces the conditions-only table.

Usage:
    python tools/replay_kind_rule.py GOLD_DIR --record-out DUMP.json
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

import score_gold

from formextract.ingest import ingest
from formextract.layout import analyze
from formextract.rows import build_all_rows

#: The literal ASCII checkbox spellings the shipped rule treats as a marker. This is a copy, on
#: purpose (the tool never imports the resolver); a test asserts it equals ``resolve._ASCII_BOX``.
ASCII_BOX = re.compile(r"^[\[(]\s*[xX]?\s*[\])](?:\s|$)")

#: Per-tag counters the reviewer's table reports; the rest are still summed into
#: the overall line but not printed, to keep the two runs side by side.
FOCUS_TAGS = (
    "text_field",
    "side_by_side",
    "yes_no_row",
    "no_glyph_answer",
    "matrix",
    "typed_value",
    "gutter_column",
    "label_two_cells",
)


class Replay:
    """The rule reimplemented against a serialised dump.

    Reads the gold ``.xlsx`` for the per-tab row lattice and the layout's
    ``marker_classes``; never imports the resolver, so this is the offline model
    of the rule, not the production code path.
    """

    def __init__(self, gold_dir: Path, gold_doc: dict, *, non_answer_ids=()):
        self.gold_dir = Path(gold_dir)
        self.non_answer_ids = frozenset(non_answer_ids)
        self.by_id: dict[str, dict] = {}
        self.lattices: dict[str, dict] = {}
        self.marker_rows: dict[str, set] = {}
        for tab in gold_doc["tabs"]:
            elements = list(ingest(self.gold_dir / f"{tab}.xlsx").elements)
            by_id = {e.element_id: e for e in elements}
            layout = analyze(elements)
            lattices = build_all_rows(layout, by_id)
            self.by_id[tab] = by_id
            self.lattices[tab] = lattices
            rows: set[tuple[int, int]] = set()
            for mc in layout.marker_classes:
                if mc.marker_element_id in self.non_answer_ids:
                    continue
                element = by_id.get(mc.marker_element_id)
                if element is None:
                    continue
                lattice = lattices.get(element.bbox.page)
                row = lattice.row_of_element(mc.marker_element_id) if lattice else None
                if row is not None:
                    rows.add((element.bbox.page, row))
            for element_id, element in by_id.items():
                if element_id in self.non_answer_ids or not ASCII_BOX.match(element.text.strip()):
                    continue
                lattice = lattices.get(element.bbox.page)
                row = lattice.row_of_element(element_id) if lattice else None
                if row is not None:
                    rows.add((element.bbox.page, row))
            self.marker_rows[tab] = rows

    def _rows_of(self, tab: str, field: dict) -> set[tuple[int, int]]:
        rows: set[tuple[int, int]] = set()
        for element_id in field.get("source_elements") or ():
            if not isinstance(element_id, str) or element_id in self.non_answer_ids:
                continue
            element = self.by_id.get(tab, {}).get(element_id)
            if element is None:
                continue
            lattice = self.lattices[tab].get(element.bbox.page)
            row = lattice.row_of_element(element_id) if lattice else None
            if row is not None:
                rows.add((element.bbox.page, row))
        return rows

    @staticmethod
    def _candidate(field: dict) -> bool:
        """The card, the option count and the Amendment-A answer guard."""
        if field.get("control_type") != "single_select":
            return False
        options = field.get("options") or []
        if len(options) != 1:
            return False
        if field.get("answers"):
            return False
        return not any(
            isinstance(option, dict) and option.get("selected") is True
            for option in options
        )

    def apply(self, dump: dict) -> tuple[list[dict], int]:
        """Return ``(fields, fired)`` with the rule applied to a copy of the dump."""
        fields = copy.deepcopy(score_gold._extract_fields(dump))
        by_tab: dict[str | None, list[tuple[int, set]]] = {}
        for index, field in enumerate(fields):
            tab = score_gold._tab_of(field)
            by_tab.setdefault(tab, []).append((index, self._rows_of(tab, field)))

        fired = 0
        for index, field in enumerate(fields):
            tab = score_gold._tab_of(field)
            if not self._candidate(field):
                continue
            mine = self._rows_of(tab, field)
            if any(
                other_index != index and (other & mine)
                for other_index, other in by_tab.get(tab, ())
            ):
                continue
            fired += 1
            if mine & self.marker_rows.get(tab, set()):
                field["control_type"] = "bool"
            else:
                field["control_type"] = "text"
                field["answers"] = [field["options"][0].get("text")]
                field["options"] = []
        return fields, fired


def _score(gold: "score_gold.Gold", fields: list[dict]) -> dict:
    predicted, stray, unresolved = score_gold._predicted_fields(gold, fields)
    return score_gold.score_document(gold, predicted, stray, unresolved, None)


def replay(gold_dir, dump: dict, gold_doc: dict) -> dict:
    """Score ``dump`` off and on and return both results plus the fired count."""
    gold = score_gold.Gold(gold_doc)
    replayer = Replay(Path(gold_dir), gold_doc)
    off_fields = copy.deepcopy(score_gold._extract_fields(dump))
    on_fields, fired = replayer.apply(dump)
    return {
        "fired": fired,
        "off": {"fields": off_fields, "score": _score(gold, off_fields)},
        "on": {"fields": on_fields, "score": _score(gold, on_fields)},
    }


def _overall_line(name: str, fired: int, overall: dict) -> str:
    return (
        f"{name:9} fired={fired:2} matched={overall['matched']} "
        f"ik={overall['matched_ignoring_kind']} conf={overall['kind_confusions']} "
        f"prec={overall['precision']} rec={overall['recall']} "
        f"gold={overall['gold_fields']} pred={overall['predicted_fields']} "
        f"missed={overall['missed']} spurious={overall['spurious']}"
    )


def format_report(result: dict) -> str:
    """One text block per run: the overall line, then the focused per-tag lines."""
    lines: list[str] = []
    for name, key, fired in (("rule off", "off", 0), ("rule on", "on", result["fired"])):
        score = result[key]["score"]
        lines.append(_overall_line(name, fired, score["overall"]))
        per_tag = score["per_tag"]
        for tag in FOCUS_TAGS:
            if tag not in per_tag:
                continue
            entry = per_tag[tag]
            lines.append(
                f"    {tag:18} matched={entry['matched']}/{entry['gold_fields']} "
                f"ik={entry['matched_ignoring_kind']} conf={entry['kind_confusions']}"
            )
    return "\n".join(lines)


def _load_gold_doc(gold_dir: Path, explicit: str | None) -> dict:
    if explicit is not None:
        return json.loads(Path(explicit).read_text(encoding="utf-8"))
    if not gold_dir.is_dir():
        raise SystemExit(f"--gold is not a directory: {gold_dir}")
    golds = sorted(
        p for p in gold_dir.glob("gold_*.json") if not p.name.startswith("gold_manifest_")
    )
    if len(golds) != 1:
        raise SystemExit("a gold directory must hold exactly one gold_*.json")
    return json.loads(golds[0].read_text(encoding="utf-8"))


def _load_dump(path: Path) -> dict:
    dump = json.loads(path.read_text(encoding="utf-8"))
    score_gold._extract_fields(dump)  # refuse a dump with no fields list
    return dump


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("gold_dir", metavar="GOLD_DIR")
    parser.add_argument(
        "--record-out", required=True, metavar="PATH",
        help="a record dump written by live_probe.py --record-out",
    )
    parser.add_argument(
        "--gold", default=None, metavar="GOLD.json",
        help="the gold document, when the directory does not name one",
    )
    args = parser.parse_args(argv)

    gold_dir = Path(args.gold_dir)
    gold_doc = _load_gold_doc(gold_dir, args.gold)
    dump = _load_dump(Path(args.record_out))
    print(format_report(replay(gold_dir, dump, gold_doc)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
