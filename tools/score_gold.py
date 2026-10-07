#!/usr/bin/env python3
"""Exact gold scorer for the 0.6.0 line-contract work (turn G).

Stdlib only: imports nothing from ``formextract`` and nothing from openpyxl, so
it can be run on the owner's private workbook as integers only. It compares a
record's ``fields`` (an ``InstanceRecord`` JSON dump, or a bare ``{"fields":
[...]}``) against a gold JSON made by ``tools/make_gold.py``.

Scoring is exact and gives no half credit. A predicted field matches a gold
field iff the set of its label/option element ids equals the gold field's
``label_cells`` U ``option_cells`` AND its kind equals the gold field's kind.
Predicted ids outside the tab's gold cell universe are ``stray_ref_count`` and
never enter the identity set; ``unresolved_source_refs`` are counted separately.
Every number is reported overall and per structure tag, as integers with their
denominators and (where meaningful) a percentage. The output contains only
integers, percentages and tag/counter names: never a label, a cell text or a tab
name, so it is safe on private gold.

Exit codes: 0 scored, 2 bad input (not JSON, wrong schema, tab mismatch).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

GOLD_VERSION = 1

_KIND_BY_CONTROL = {
    "single_select": "single",
    "multi_select": "multi",
    "bool": "bool",
    "text": "text",
    # accept the gold kind directly, for a bare fields list
    "single": "single",
    "multi": "multi",
}

#: The closed set a field kind can take. ``kind_confusion_pairs`` keys are built
#: only from these (never from any sheet text).
KIND_VOCAB = ("single", "multi", "bool", "text")

#: Integer counters reported overall and per tag. The mutation table in the
#: test diffs exactly these. ``kind_confusion_pairs`` is a small count table,
#: not an integer, so it travels beside these keys rather than inside them.
COUNTER_KEYS = (
    "gold_fields",
    "predicted_fields",
    "matched",
    "matched_ignoring_kind",
    "kind_confusions",
    "merge_count",
    "split_count",
    "missed",
    "spurious",
    "stray_ref_count",
    "unresolved_ref_count",
    "label_ok",
    "label_total",
    "options_ok",
    "options_total",
    "selected_ok",
    "selected_total",
    "selected_ok_under_convention",
    "selected_ambiguous_expected",
    "unexpected_ambiguous",
    "answer_ok",
    "answer_total",
    "addressed_num",
    "addressed_den",
    "precision_num",
    "precision_den",
    "recall_num",
    "recall_den",
)


class BadInput(Exception):
    """Raised for anything that makes the inputs unusable (exit code 2)."""


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 6) if den else None


def _load_json(path: str) -> object:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise BadInput(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BadInput(f"{path} is not JSON: {exc}") from exc


def _extract_fields(doc: object) -> list[dict]:
    if isinstance(doc, list):
        fields = doc
    elif isinstance(doc, dict) and isinstance(doc.get("fields"), list):
        fields = doc["fields"]
    else:
        raise BadInput("record has no fields list")
    for field in fields:
        if not isinstance(field, dict):
            raise BadInput("record fields must be objects")
    return fields


def _kind_of(control_type: object) -> str | None:
    if not isinstance(control_type, str):
        return None
    return _KIND_BY_CONTROL.get(control_type)


def _tab_of(field: dict) -> str | None:
    tab = field.get("tab")
    if isinstance(tab, str) and tab:
        return tab
    for element_id in field.get("source_elements") or ():
        if isinstance(element_id, str) and "!" in element_id:
            return element_id.split("!", 1)[0]
    return None


class Gold:
    """The gold JSON, indexed for scoring."""

    def __init__(self, doc: dict):
        if not isinstance(doc, dict) or not isinstance(doc.get("fields"), list):
            raise BadInput("gold has no fields list")
        self.tabs = list(doc.get("tabs") or [])
        if not self.tabs:
            # infer the tab universe from the ids if the file omits it
            for cell in doc.get("cells") or ():
                tab = str(cell.get("id", "")).split("!", 1)[0]
                if tab and tab not in self.tabs:
                    self.tabs.append(tab)
        self.cells_by_id = {c["id"]: c for c in doc.get("cells") or ()}
        self.tab_cells: dict[str, set[str]] = {tab: set() for tab in self.tabs}
        self.tab_label_option: dict[str, set[str]] = {tab: set() for tab in self.tabs}
        for cid, cell in self.cells_by_id.items():
            tab = cell["tab"]
            self.tab_cells.setdefault(tab, set()).add(cid)
            if cell["role"] in ("label", "option"):
                self.tab_label_option.setdefault(tab, set()).add(cid)
        self.dispositions = list(doc.get("dispositions") or [])
        self.fields: list[dict] = []
        for gf in doc["fields"]:
            label_cells = list(gf["label_cells"])
            option_cells = list(gf["option_cells"])
            identity = frozenset(label_cells + option_cells)
            self.fields.append(
                {
                    "gf": gf,
                    "tab": gf["tab"],
                    "identity": identity,
                    "kind": gf["kind"],
                    "tags": set(gf.get("tags") or ()),
                    "expected_ambiguous": bool(gf.get("expected_ambiguous")),
                    "label_text": " ".join(
                        self.cells_by_id[c]["text"] for c in label_cells
                    ),
                    "option_texts": {
                        self.cells_by_id[c]["text"] for c in option_cells
                    },
                    "selected_texts": {
                        self.cells_by_id[c]["text"]
                        for c in gf.get("selected_option_cells") or ()
                    },
                    "answer_text": gf.get("answer_text"),
                    "label_option_cells": set(label_cells + option_cells),
                    "kind_": gf["kind"],
                }
            )

    def check_tab(self, tab: str | None) -> str:
        if tab is None or tab not in self.tabs:
            raise BadInput("record/gold tab mismatch")
        return tab


def _predicted_fields(gold: Gold, fields: list[dict]) -> tuple[list[dict], int, int]:
    """Normalise predicted fields; return (predicted, stray_ref_count, unresolved)."""
    predicted: list[dict] = []
    stray = 0
    unresolved = 0
    for field in fields:
        tab = gold.check_tab(_tab_of(field))
        source = [e for e in (field.get("source_elements") or ()) if isinstance(e, str)]
        universe = gold.tab_cells.get(tab, set())
        stray += sum(1 for e in source if e not in universe)
        unresolved += len(field.get("unresolved_source_refs") or ())
        label_option = gold.tab_label_option.get(tab, set())
        identity = frozenset(e for e in source if e in label_option)
        predicted.append(
            {
                "raw": field,
                "tab": tab,
                "identity": identity,
                "kind": _kind_of(field.get("control_type")),
                "label_text": field.get("label_text")
                if isinstance(field.get("label_text"), str)
                else None,
                "option_texts": {
                    o.get("text")
                    for o in (field.get("options") or ())
                    if isinstance(o, dict)
                },
                "selected_texts": {
                    o.get("text")
                    for o in (field.get("options") or ())
                    if isinstance(o, dict) and o.get("selected") is True
                },
                "answers": [
                    a for a in (field.get("answers") or ()) if isinstance(a, str)
                ],
                # A predicted field that carries an ambiguity could not be
                # decided by the resolver (a between-marker without a declared,
                # applicable convention). On a gold `expected_ambiguous` field
                # that is the by-design outcome; anywhere else it is the signal
                # that a convention was not declared or could not apply.
                "ambiguous": field.get("ambiguity") is not None,
            }
        )
    return predicted, stray, unresolved


def _subscore(gold_fields: list[dict], predicted: list[dict], cited: set[str]) -> dict:
    """Score one (gold subset, predicted subset) universe."""
    gold_by_key: dict[tuple, dict] = {}
    for g in gold_fields:
        gold_by_key.setdefault((g["identity"], g["kind"]), g)

    matched: list[tuple[dict, dict]] = []
    used_gold: set[int] = set()
    for p in predicted:
        g = gold_by_key.get((p["identity"], p["kind"]))
        if g is not None and id(g) not in used_gold:
            used_gold.add(id(g))
            matched.append((p, g))

    # The same greedy walk keyed on the identity alone: the grouping question,
    # with the kind question set aside. A gold identity is unique, so every
    # strictly matched field is an ignoring-kind match too and the two counts
    # differ by exactly the fields whose kind was guessed wrong.
    gold_by_identity: dict[frozenset, dict] = {}
    for g in gold_fields:
        gold_by_identity.setdefault(g["identity"], g)
    matched_ignoring_kind: list[tuple[dict, dict]] = []
    used_gold_ik: set[int] = set()
    for p in predicted:
        g = gold_by_identity.get(p["identity"])
        if g is not None and id(g) not in used_gold_ik:
            used_gold_ik.add(id(g))
            matched_ignoring_kind.append((p, g))

    matched_n = len(matched)
    matched_ik_n = len(matched_ignoring_kind)
    kind_confusions = matched_ik_n - matched_n
    strictly_matched_gold = {id(g) for _, g in matched}
    kind_confusion_pairs: dict[str, int] = {}
    for p, g in matched_ignoring_kind:
        if id(g) in strictly_matched_gold:
            continue
        predicted_kind = p["kind"] if p["kind"] in KIND_VOCAB else "none"
        key = f"{g['kind']}>{predicted_kind}"
        kind_confusion_pairs[key] = kind_confusion_pairs.get(key, 0) + 1

    merge_count = sum(
        1
        for p in predicted
        if any(g["identity"] and g["identity"] < p["identity"] for g in gold_fields)
    )
    split_count = sum(
        1
        for g in gold_fields
        if any(p["identity"] and p["identity"] < g["identity"] for p in predicted)
    )
    missed = 0
    for g in gold_fields:
        if id(g) in used_gold:
            continue
        if any(g["identity"] and g["identity"] < p["identity"] for p in predicted):
            continue
        if any(p["identity"] and p["identity"] < g["identity"] for p in predicted):
            continue
        missed += 1
    spurious = 0
    matched_pred = {id(p) for p, _ in matched}
    for p in predicted:
        if id(p) in matched_pred:
            continue
        if any(p["identity"] and p["identity"] > g["identity"] for g in gold_fields):
            continue
        if any(p["identity"] and p["identity"] < g["identity"] for g in gold_fields):
            continue
        spurious += 1

    label_ok = sum(1 for p, g in matched if p["label_text"] == g["label_text"])
    options_ok = sum(1 for p, g in matched if p["option_texts"] == g["option_texts"])
    selected_total = 0
    selected_ok = 0
    selected_ok_under_convention = 0
    selected_ambiguous_expected = 0
    unexpected_ambiguous = 0
    answer_total = 0
    answer_ok = 0
    for p, g in matched:
        if g["kind"] in ("single", "multi", "bool"):
            selected_total += 1
            if g["expected_ambiguous"]:
                # Ambiguity is the designed outcome here; do not penalise it.
                selected_ok += 1
                selected_ambiguous_expected += 1
            elif p["ambiguous"]:
                # The resolver could not decide a field that should be
                # decidable: the convention was not declared or did not apply.
                unexpected_ambiguous += 1
            elif p["selected_texts"] == g["selected_texts"]:
                selected_ok += 1
                selected_ok_under_convention += 1
        if g["kind"] == "text":
            answer_total += 1
            joined = " ".join(p["answers"])
            if g["answer_text"] is not None and joined == g["answer_text"]:
                answer_ok += 1

    addressed_den = sum(len(g["label_option_cells"]) for g in gold_fields)
    addressed_num = sum(
        sum(1 for c in g["label_option_cells"] if c in cited) for g in gold_fields
    )

    matched_n = len(matched)
    counters = {
        "gold_fields": len(gold_fields),
        "predicted_fields": len(predicted),
        "matched": matched_n,
        "matched_ignoring_kind": matched_ik_n,
        "kind_confusions": kind_confusions,
        "kind_confusion_pairs": kind_confusion_pairs,
        "merge_count": merge_count,
        "split_count": split_count,
        "missed": missed,
        "spurious": spurious,
        "label_ok": label_ok,
        "label_total": matched_n,
        "options_ok": options_ok,
        "options_total": matched_n,
        "selected_ok": selected_ok,
        "selected_total": selected_total,
        "selected_ok_under_convention": selected_ok_under_convention,
        "selected_ambiguous_expected": selected_ambiguous_expected,
        "unexpected_ambiguous": unexpected_ambiguous,
        "answer_ok": answer_ok,
        "answer_total": answer_total,
        "addressed_num": addressed_num,
        "addressed_den": addressed_den,
        "precision_num": matched_n,
        "precision_den": matched_n + spurious,
        "recall_num": matched_n,
        "recall_den": matched_n + missed,
    }
    return counters


def _with_stars(counters: dict) -> dict:
    out = dict(counters)
    out["precision"] = _ratio(counters["precision_num"], counters["precision_den"])
    out["recall"] = _ratio(counters["recall_num"], counters["recall_den"])
    out["addressed"] = _ratio(counters["addressed_num"], counters["addressed_den"])
    return out


def score_document(gold: Gold, predicted: list[dict], stray: int, unresolved: int,
                   disposition_acc: dict | None) -> dict:
    cited = {e for p in predicted for e in (p["raw"].get("source_elements") or ())}
    overall = _subscore(gold.fields, predicted, cited)
    overall["stray_ref_count"] = stray
    overall["unresolved_ref_count"] = unresolved
    overall = _with_stars(overall)

    per_tag: dict[str, dict] = {}
    tags = sorted({tag for g in gold.fields for tag in g["tags"]})
    for tag in tags:
        gold_sub = [g for g in gold.fields if tag in g["tags"]]
        pred_sub = [
            p
            for p in predicted
            if any(
                p["identity"] == g["identity"]
                or (p["identity"] and p["identity"] < g["identity"])
                or (p["identity"] and p["identity"] > g["identity"])
                for g in gold_sub
            )
        ]
        sub = _subscore(gold_sub, pred_sub, cited)
        sub["stray_ref_count"] = overall["stray_ref_count"]
        sub["unresolved_ref_count"] = overall["unresolved_ref_count"]
        sub = _with_stars(sub)
        sub["small_n"] = len(gold_sub) < 5
        per_tag[tag] = sub

    result = {"gold_version": GOLD_VERSION, "overall": overall, "per_tag": per_tag}
    if disposition_acc is not None:
        result["overall"]["disposition_num"] = disposition_acc["num"]
        result["overall"]["disposition_den"] = disposition_acc["den"]
        result["overall"]["disposition_acc"] = _ratio(
            disposition_acc["num"], disposition_acc["den"]
        )
    return result


def _disposition_accuracy(gold: Gold, doc: object) -> dict | None:
    if not isinstance(doc, dict) or "dispositions" not in doc:
        return None
    predicted = doc.get("dispositions")
    if not isinstance(predicted, list):
        raise BadInput("record dispositions must be a list")
    hit = {d.get("cell") for d in predicted if isinstance(d, dict)}
    num = sum(1 for d in gold.dispositions if d["cell"] in hit)
    return {"num": num, "den": len(gold.dispositions)}


def _pct(ratio: float | None) -> str:
    return "null" if ratio is None else f"{ratio * 100:.1f}%"


def _frac(num: int, den: int, ratio: float | None) -> str:
    return f"{num}/{den}={_pct(ratio)}"


def _pairs_text(pairs: dict) -> str:
    """``kind_confusion_pairs`` as ``<gold>><predicted>=n``, kinds only."""
    if not pairs:
        return "none"
    return ",".join(f"{key}={pairs[key]}" for key in sorted(pairs))


def format_text(result: dict) -> str:
    o = result["overall"]
    lines = [
        f"gold_fields {o['gold_fields']} predicted_fields {o['predicted_fields']} "
        f"matched {o['matched']}",
        f"matched_ignoring_kind {o['matched_ignoring_kind']} "
        f"kind_confusions {o['kind_confusions']} "
        f"kind_confusion_pairs {_pairs_text(o['kind_confusion_pairs'])}",
        f"precision {_frac(o['precision_num'], o['precision_den'], o['precision'])} "
        f"recall {_frac(o['recall_num'], o['recall_den'], o['recall'])}",
        f"merge_count {o['merge_count']} split_count {o['split_count']} "
        f"missed {o['missed']} spurious {o['spurious']} "
        f"stray_ref_count {o['stray_ref_count']} "
        f"unresolved_ref_count {o['unresolved_ref_count']}",
        f"label_ok {o['label_ok']}/{o['label_total']} "
        f"options_ok {o['options_ok']}/{o['options_total']} "
        f"selected_ok {o['selected_ok']}/{o['selected_total']} "
        f"answer_ok {o['answer_ok']}/{o['answer_total']}",
        f"selected_ok_under_convention {o['selected_ok_under_convention']} "
        f"selected_ambiguous_expected {o['selected_ambiguous_expected']} "
        f"unexpected_ambiguous {o['unexpected_ambiguous']}",
        f"addressed {_frac(o['addressed_num'], o['addressed_den'], o['addressed'])} "
        "(perception)",
    ]
    if "disposition_acc" in o:
        lines.append(
            f"disposition_acc "
            f"{_frac(o['disposition_num'], o['disposition_den'], o['disposition_acc'])}"
        )
    lines.append("per tag (small_n = fewer than 5 gold instances):")
    for tag in sorted(result["per_tag"]):
        t = result["per_tag"][tag]
        mark = " [small_n]" if t["small_n"] else ""
        lines.append(
            f"  {tag}: gold {t['gold_fields']} matched {t['matched']} "
            f"matched_ignoring_kind {t['matched_ignoring_kind']} "
            f"kind_confusions {t['kind_confusions']} "
            f"precision {_frac(t['precision_num'], t['precision_den'], t['precision'])} "
            f"recall {_frac(t['recall_num'], t['recall_den'], t['recall'])} "
            f"missed {t['missed']} spurious {t['spurious']} "
            f"merge {t['merge_count']} split {t['split_count']} "
            f"selected_ok {t['selected_ok']}/{t['selected_total']} "
            f"unexpected_ambiguous {t['unexpected_ambiguous']}{mark}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True)
    parser.add_argument("--record", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        gold_doc = _load_json(args.gold)
        record_doc = _load_json(args.record)
        if not isinstance(gold_doc, dict):
            raise BadInput("gold must be a JSON object")
        gold = Gold(gold_doc)
        fields = _extract_fields(record_doc)
        predicted, stray, unresolved = _predicted_fields(gold, fields)
        disposition = _disposition_accuracy(gold, record_doc)
        result = score_document(gold, predicted, stray, unresolved, disposition)
    except BadInput as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        # never surface a traceback that could echo text from the input files
        print("error: could not score the inputs", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(format_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
