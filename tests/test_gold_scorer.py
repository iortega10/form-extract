from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl
import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "tools") not in sys.path:
    sys.path.insert(0, str(REPO / "tools"))

import make_gold  # noqa: E402
import score_gold  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures: build the dev and held-out sets once per module.
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    base = tmp_path_factory.mktemp("gold")
    dev_dir = base / "dev"
    held_dir = base / "heldout"
    dev_gold, dev_manifest, _ = make_gold.build_set(
        "dev", make_gold.DEFAULT_SEEDS["dev"], dev_dir
    )
    held_gold, held_manifest, _ = make_gold.build_set(
        "heldout", make_gold.DEFAULT_SEEDS["heldout"], held_dir
    )
    make_gold.write_canned(dev_gold, dev_dir / "canned")
    make_gold.write_canned(held_gold, held_dir / "canned")
    return {
        "base": base,
        "dev_dir": dev_dir,
        "held_dir": held_dir,
        "dev_gold": dev_gold,
        "dev_manifest": dev_manifest,
        "held_gold": held_gold,
        "held_manifest": held_manifest,
    }


def _workbook_cells(path: Path) -> dict[str, str]:
    wb = openpyxl.load_workbook(filename=str(path))
    out: dict[str, str] = {}
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is not None:
                    out[f"{ws.title}!{cell.row}:{cell.column}"] = str(cell.value)
    wb.close()
    return out


def _label_option_texts(doc: dict) -> set[str]:
    cells = {c["id"]: c for c in doc["cells"]}
    out: set[str] = set()
    for field in doc["fields"]:
        for cid in field["label_cells"] + field["option_cells"]:
            out.add(cells[cid]["text"])
    return out


def _load_record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _overall(gold_doc: dict, record_path: Path) -> dict:
    g = score_gold.Gold(gold_doc)
    record = _load_record(record_path)
    predicted, stray, unresolved = score_gold._predicted_fields(
        g, score_gold._extract_fields(record)
    )
    disposition = score_gold._disposition_accuracy(g, record)
    return score_gold.score_document(g, predicted, stray, unresolved, disposition)["overall"]


# --------------------------------------------------------------------------
# Committed expected outputs (fixed seed). Regenerate with:
#   python tools/make_gold.py --set dev --out DIR --canned DIR/canned
#   (then read the scorer's overall counters for the perfect and mutated dumps)
# --------------------------------------------------------------------------

PERFECT_COUNTERS = {
    "gold_fields": 125,
    "predicted_fields": 125,
    "matched": 125,
    "merge_count": 0,
    "split_count": 0,
    "missed": 0,
    "spurious": 0,
    "stray_ref_count": 0,
    "unresolved_ref_count": 0,
    "label_ok": 125,
    "label_total": 125,
    "options_ok": 125,
    "options_total": 125,
    "selected_ok": 90,
    "selected_total": 90,
    "answer_ok": 35,
    "answer_total": 35,
    "addressed_num": 628,
    "addressed_den": 628,
    "precision_num": 125,
    "precision_den": 125,
    "recall_num": 125,
    "recall_den": 125,
    "precision": 1.0,
    "recall": 1.0,
    "addressed": 1.0,
}

MUTATION_DELTAS = {
    "drop_field": {
        "predicted_fields": -1, "matched": -1, "missed": 1, "label_ok": -1,
        "label_total": -1, "options_ok": -1, "options_total": -1,
        "selected_ok": -1, "selected_total": -1, "addressed_num": -3,
        "precision_num": -1, "precision_den": -1, "recall_num": -1,
    },
    "duplicate_field": {
        "predicted_fields": 1, "spurious": 1, "precision_den": 1,
    },
    "empty_fields": {
        "predicted_fields": -125, "matched": -125, "missed": 125,
        "label_ok": -125, "label_total": -125, "options_ok": -125,
        "options_total": -125, "selected_ok": -90, "selected_total": -90,
        "answer_ok": -35, "answer_total": -35, "addressed_num": -628,
        "precision_num": -125, "precision_den": -125, "recall_num": -125,
    },
    "merge_two": {
        "predicted_fields": -1, "matched": -2, "merge_count": 1,
        "label_ok": -2, "label_total": -2, "options_ok": -2,
        "options_total": -2, "selected_ok": -2, "selected_total": -2,
        "precision_num": -2, "precision_den": -2, "recall_num": -2,
        "recall_den": -2,
    },
    "missing_tab": {
        "predicted_fields": -14, "matched": -14, "missed": 14,
        "label_ok": -14, "label_total": -14, "options_ok": -14,
        "options_total": -14, "selected_ok": -14, "selected_total": -14,
        "addressed_num": -42, "precision_num": -14, "precision_den": -14,
        "recall_num": -14,
    },
    "split_one": {
        "predicted_fields": 1, "matched": -1, "split_count": 1,
        "label_ok": -1, "label_total": -1, "options_ok": -1,
        "options_total": -1, "selected_ok": -1, "selected_total": -1,
        "precision_num": -1, "precision_den": -1, "recall_num": -1,
        "recall_den": -1,
    },
    "stray_id": {"stray_ref_count": 1},
    "unresolved_ref": {"unresolved_ref_count": 1},
    "wrong_answer": {"answer_ok": -1},
    "wrong_kind": {
        "matched": -1, "missed": 1, "spurious": 1, "label_ok": -1,
        "label_total": -1, "options_ok": -1, "options_total": -1,
        "selected_ok": -1, "selected_total": -1, "precision_num": -1,
        "recall_num": -1,
    },
    "wrong_label": {"label_ok": -1},
    "wrong_selected": {"selected_ok": -1},
}


# --------------------------------------------------------------------------
# Generator determinism
# --------------------------------------------------------------------------

def test_generator_is_deterministic(tmp_path):
    one, _, _ = make_gold.build_set("dev", 20260101, tmp_path / "a")
    two, _, _ = make_gold.build_set("dev", 20260101, tmp_path / "b")
    three, _, _ = make_gold.build_set("dev", 999, tmp_path / "c")

    assert one == two
    for tab in one["tabs"]:
        assert (tmp_path / "a" / f"{tab}.xlsx").read_bytes() == (
            tmp_path / "b" / f"{tab}.xlsx"
        ).read_bytes()
        assert (tmp_path / "a" / f"{tab}.xlsx").read_bytes() != (
            tmp_path / "c" / f"{tab}.xlsx"
        ).read_bytes()
    assert one != three


def test_dev_and_heldout_vocabularies_are_disjoint(gold):
    dev = _label_option_texts(gold["dev_gold"])
    held = _label_option_texts(gold["held_gold"])
    assert dev & held == set()


def test_every_field_tag_has_at_least_six_dev_instances(gold):
    counts = gold["dev_manifest"]["tag_counts"]
    for tag in make_gold.FIELD_TAGS:
        assert counts[tag] >= 6, (tag, counts[tag])
    for tag in make_gold.DISPOSITION_TAGS:
        assert gold["dev_manifest"]["disposition_counts"][tag] >= 1


def test_manifest_counts_match_a_gold_recount(gold):
    doc = gold["dev_gold"]
    manifest = gold["dev_manifest"]
    assert manifest["fields"] == len(doc["fields"])
    assert manifest["cells"] == len(doc["cells"])
    for tag in make_gold.FIELD_TAGS:
        recount = sum(1 for f in doc["fields"] if tag in f["tags"])
        assert manifest["tag_counts"][tag] == recount
    tab_counts = {tab: 0 for tab in doc["tabs"]}
    for field in doc["fields"]:
        tab_counts[field["tab"]] += 1
    assert manifest["tab_counts"] == tab_counts


# --------------------------------------------------------------------------
# Gold is consistent with the workbooks
# --------------------------------------------------------------------------

@pytest.mark.parametrize("set_name", ["dev", "heldout"])
def test_gold_cells_match_workbook_cells(gold, set_name):
    doc = gold[f"{'dev' if set_name == 'dev' else 'held'}_gold"]
    out_dir = gold["dev_dir"] if set_name == "dev" else gold["held_dir"]

    roles: dict[str, str] = {}
    for cell in doc["cells"]:
        assert cell["id"] not in roles, cell["id"]
        roles[cell["id"]] = cell["role"]

    for tab in doc["tabs"]:
        workbook = _workbook_cells(out_dir / f"{tab}.xlsx")
        gold_cells = {c["id"]: c["text"] for c in doc["cells"] if c["tab"] == tab}
        # every gold cell exists at that id and holds exactly the stated text
        for cid, text in gold_cells.items():
            assert workbook.get(cid) == text, (cid, workbook.get(cid), text)
        # and no non-empty cell of the tab is unaccounted for
        assert set(workbook) == set(gold_cells), (
            set(workbook) - set(gold_cells),
            set(gold_cells) - set(workbook),
        )

    role_of_field_cells = (
        ("label_cells", "label"),
        ("option_cells", "option"),
        ("answer_cells", "answer"),
        ("annotation_cells", "annotation"),
    )
    for field in doc["fields"]:
        for key, role in role_of_field_cells:
            for cid in field[key]:
                assert roles[cid] == role, (cid, roles[cid], role)
        for cid in field["selected_option_cells"]:
            assert cid in field["option_cells"]

    # every disposition cell carries the disposition role
    by_id = {c["id"]: c for c in doc["cells"]}
    for disp in doc["dispositions"]:
        assert by_id[disp["cell"]]["role"] == disp["disposition"]


# --------------------------------------------------------------------------
# The scorer on the perfect dump
# --------------------------------------------------------------------------

def test_committed_perfect_dump_table(gold):
    overall = _overall(gold["dev_gold"], gold["dev_dir"] / "canned" / "perfect_dev.json")
    for key, expected in PERFECT_COUNTERS.items():
        assert overall[key] == expected, (key, overall[key], expected)


def test_perfect_dump_scores_one_per_tag(gold):
    g = score_gold.Gold(gold["dev_gold"])
    record = _load_record(gold["dev_dir"] / "canned" / "perfect_dev.json")
    predicted, stray, unresolved = score_gold._predicted_fields(
        g, score_gold._extract_fields(record)
    )
    result = score_gold.score_document(g, predicted, stray, unresolved, None)
    assert result["per_tag"]
    for tag, t in result["per_tag"].items():
        assert t["precision"] == 1.0, tag
        assert t["recall"] == 1.0, tag
        assert t["matched"] == t["gold_fields"], tag
        assert t["merge_count"] == 0 and t["split_count"] == 0, tag
        assert t["missed"] == 0 and t["spurious"] == 0, tag
        assert t["label_ok"] == t["matched"], tag
        assert t["options_ok"] == t["matched"], tag
        assert t["selected_ok"] == t["selected_total"], tag


# --------------------------------------------------------------------------
# Mutated dumps move exactly the named counters
# --------------------------------------------------------------------------

def test_mutations_move_exactly_the_expected_counters(gold):
    base = _overall(gold["dev_gold"], gold["dev_dir"] / "canned" / "perfect_dev.json")
    canned = gold["dev_dir"] / "canned"
    names = sorted(p.name for p in canned.glob("*_dev.json"))
    assert names, "no canned dumps written"
    for name in names:
        mutation = name[: -len("_dev.json")]
        if mutation == "perfect":
            continue
        overall = _overall(gold["dev_gold"], canned / name)
        delta = {
            key: overall[key] - base[key]
            for key in score_gold.COUNTER_KEYS
            if overall[key] != base[key]
        }
        assert delta == MUTATION_DELTAS[mutation], (mutation, delta)


# --------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------

def test_empty_record_reports_null_precision(gold, tmp_path):
    record = tmp_path / "empty.json"
    record.write_text(json.dumps({"fields": []}), encoding="utf-8")
    overall = _overall(gold["dev_gold"], record)
    assert overall["predicted_fields"] == 0
    assert overall["matched"] == 0
    assert overall["missed"] == overall["gold_fields"]
    assert overall["precision"] is None
    assert overall["recall"] == 0.0


def test_empty_gold_tab_reports_null_recall(tmp_path):
    gold_doc = {
        "gold_version": 1,
        "set": "tiny",
        "seed": 1,
        "tabs": ["T"],
        "cells": [],
        "fields": [],
        "dispositions": [],
    }
    record = tmp_path / "r.json"
    record.write_text(json.dumps({"fields": []}), encoding="utf-8")
    overall = _overall(gold_doc, record)
    assert overall["gold_fields"] == 0
    assert overall["precision"] is None
    assert overall["recall"] is None


def test_zero_predicted_with_gold_reports_defined_recall(gold, tmp_path):
    record = tmp_path / "r.json"
    record.write_text(json.dumps({"fields": []}), encoding="utf-8")
    overall = _overall(gold["dev_gold"], record)
    assert overall["recall"] == 0.0
    assert overall["precision"] is None


def test_bad_input_exits_two(gold, tmp_path, capsys):
    good_gold = gold["dev_dir"] / "gold_dev.json"

    not_json = tmp_path / "bad.json"
    not_json.write_text("{not json", encoding="utf-8")
    assert score_gold.main(["--gold", str(good_gold), "--record", str(not_json)]) == 2

    no_fields = tmp_path / "nofields.json"
    no_fields.write_text(json.dumps({"tabs": []}), encoding="utf-8")
    assert score_gold.main(["--gold", str(good_gold), "--record", str(no_fields)]) == 2

    mismatch = tmp_path / "mismatch.json"
    mismatch.write_text(
        json.dumps(
            {"fields": [{"tab": "Other", "control_type": "text", "source_elements": []}]}
        ),
        encoding="utf-8",
    )
    assert score_gold.main(["--gold", str(good_gold), "--record", str(mismatch)]) == 2

    # malformed gold
    bad_gold = tmp_path / "badgold.json"
    bad_gold.write_text(json.dumps([]), encoding="utf-8")
    assert score_gold.main(["--gold", str(bad_gold), "--record", str(no_fields)]) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err


# --------------------------------------------------------------------------
# The output contains no gold or record text
# --------------------------------------------------------------------------

def _secret_strings(gold_doc: dict, record: dict) -> set[str]:
    secrets = {c["text"] for c in gold_doc["cells"]}
    secrets |= set(gold_doc["tabs"])
    for field in record.get("fields", []):
        if isinstance(field.get("label_text"), str):
            secrets.add(field["label_text"])
    secrets.discard("")
    return secrets


def test_scorer_output_contains_no_gold_or_record_text(gold, capsys):
    g = score_gold.Gold(gold["dev_gold"])
    record_path = gold["dev_dir"] / "canned" / "perfect_dev.json"
    record = _load_record(record_path)
    secrets = _secret_strings(gold["dev_gold"], record)
    assert secrets  # the anti-vacuity guard: there is something to leak

    score_gold.main(["--gold", str(gold["dev_dir"] / "gold_dev.json"), "--record", str(record_path)])
    text_out = capsys.readouterr().out
    score_gold.main(
        ["--gold", str(gold["dev_dir"] / "gold_dev.json"), "--record", str(record_path), "--json"]
    )
    json_out = capsys.readouterr().out

    for secret in secrets:
        assert secret not in text_out, secret
        assert secret not in json_out, secret

    # anti-vacuity triple: the patched symbol exists, the patch is reached, and
    # the observation then differs (the leak detector can fail).
    reached = {"hit": False}
    real_format = score_gold.format_text

    def leaking_format(result):
        reached["hit"] = True
        return real_format(result) + "\n" + sorted(secrets)[0]

    assert hasattr(score_gold, "format_text")
    score_gold.format_text = leaking_format
    try:
        score_gold.main(
            ["--gold", str(gold["dev_dir"] / "gold_dev.json"), "--record", str(record_path)]
        )
        leaked_out = capsys.readouterr().out
    finally:
        score_gold.format_text = real_format
    assert reached["hit"] is True
    assert sorted(secrets)[0] in leaked_out


# --------------------------------------------------------------------------
# The scorer accepts a real InstanceRecord dump
# --------------------------------------------------------------------------

def _ref_for(layout, element_id: str):
    from formextract.model import ElementRef

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


def test_scorer_accepts_a_real_instance_record_dump(tmp_path):
    from openpyxl import Workbook

    from formextract.ingest import ingest
    from formextract.layout import analyze
    from formextract.model import to_json
    from formextract.pipeline import Pipeline, PipelineConfig
    from formextract.resolve import LLMResponse
    from formextract.store import Store

    path = tmp_path / "tiny.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Tiny"
    ws["A1"] = "Alpha widget variant?"
    ws["C1"] = "X"
    ws["D1"] = "Ruby"
    ws["F1"] = "X"
    ws["G1"] = "Teal"
    wb.save(path)
    wb.close()

    ing = ingest(path)
    layout = analyze(ing.elements)
    refs = [_ref_for(layout, eid) for eid in ("Tiny!1:1", "Tiny!1:3", "Tiny!1:4", "Tiny!1:6", "Tiny!1:7")]

    class _Client:
        def complete(self, prompt, *, model, params):
            return LLMResponse(
                text=json.dumps(
                    {
                        "fields": [
                            {
                                "label": "Alpha widget variant?",
                                "control_type": "single_select",
                                "options": [
                                    {"text": "Ruby", "selected": True},
                                    {"text": "Teal", "selected": False},
                                ],
                                "answer": ["Ruby"],
                                "annotations": [],
                                "region_id": refs[0].region_id,
                                "source_elements": [
                                    {
                                        "region_id": r.region_id,
                                        "band_id": r.band_id,
                                        "segment_index": r.segment_index,
                                    }
                                    for r in refs
                                ],
                            }
                        ]
                    }
                ),
                model=model,
                params=params,
                tokens=1,
                latency_ms=0,
            )

    record = Pipeline(Store(tmp_path / "store"), _Client(), PipelineConfig()).run(path)
    dump_path = tmp_path / "record.json"
    dump_path.write_text(to_json(record), encoding="utf-8")

    gold_doc = {
        "gold_version": 1,
        "set": "tiny",
        "seed": 1,
        "tabs": ["Tiny"],
        "cells": [
            {"id": "Tiny!1:1", "tab": "Tiny", "text": "Alpha widget variant?", "role": "label"},
            {"id": "Tiny!1:3", "tab": "Tiny", "text": "X", "role": "marker"},
            {"id": "Tiny!1:4", "tab": "Tiny", "text": "Ruby", "role": "option"},
            {"id": "Tiny!1:6", "tab": "Tiny", "text": "X", "role": "marker"},
            {"id": "Tiny!1:7", "tab": "Tiny", "text": "Teal", "role": "option"},
        ],
        "fields": [
            {
                "field_id_gold": "Tiny!f1",
                "tab": "Tiny",
                "kind": "single",
                "label_cells": ["Tiny!1:1"],
                "option_cells": ["Tiny!1:4", "Tiny!1:7"],
                "answer_cells": [],
                "annotation_cells": [],
                "selected_option_cells": ["Tiny!1:4"],
                "answer_text": None,
                "tags": ["yes_no_row"],
                "expected_ambiguous": False,
            }
        ],
        "dispositions": [],
    }
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(json.dumps(gold_doc), encoding="utf-8")

    assert score_gold.main(["--gold", str(gold_path), "--record", str(dump_path), "--json"]) == 0
    overall = _overall(gold_doc, dump_path)
    assert overall["gold_fields"] == 1
    assert overall["matched"] == 1
    assert overall["precision"] == 1.0
    assert overall["recall"] == 1.0


# --------------------------------------------------------------------------
# Static import scan
# --------------------------------------------------------------------------

def test_tools_import_neither_package_nor_each_other():
    import ast

    for name in ("make_gold.py", "score_gold.py"):
        source = (REPO / "tools" / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
        assert "formextract" not in imported, name
        assert "make_gold" not in imported, name
        assert "score_gold" not in imported, name

    scorer = (REPO / "tools" / "score_gold.py").read_text(encoding="utf-8")
    assert "import openpyxl" not in scorer
    generator = (REPO / "tools" / "make_gold.py").read_text(encoding="utf-8")
    assert "import openpyxl" in generator
