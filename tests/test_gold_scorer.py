from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import openpyxl
import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "tools") not in sys.path:
    sys.path.insert(0, str(REPO / "tools"))

import make_gold  # noqa: E402
import score_gold  # noqa: E402

#: The words a prompt variant can hand the model to copy: the words of every
#: example line and of every quoted literal (the same rule the prompt-variant
#: test uses). The cue words must not appear here.
_VARIANT_EXAMPLE_LINE = re.compile(
    r"^  (?:single|multi|bool|text|hdr|note|skip)\b.*$", re.M
)
_VARIANT_QUOTED = re.compile(r'"([^"]*)"')
WORD = re.compile(r"[A-Za-z]+")


def _variant_copyable_words() -> set[str]:
    from formextract.resolve import LINES_PROMPT_VARIANTS, ProjectionChunk

    chunk = ProjectionChunk(key="Probe", text="0: 0=Alpha widget variant? | 1=X")
    words: set[str] = set()
    for make_prompt in LINES_PROMPT_VARIANTS.values():
        text = make_prompt(chunk)
        for snippet in (
            _VARIANT_EXAMPLE_LINE.findall(text) + _VARIANT_QUOTED.findall(text)
        ):
            words.update(word.lower() for word in WORD.findall(snippet))
    return words


# --------------------------------------------------------------------------
# Fixtures: build the dev and held-out sets once per module.
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    """The gold v2 set: the frozen schema the committed tables below pin."""
    base = tmp_path_factory.mktemp("gold")
    dev_dir = base / "dev"
    held_dir = base / "heldout"
    dev_gold, dev_manifest, _ = make_gold.build_set(
        "dev", make_gold.DEFAULT_SEEDS["dev"], dev_dir, gold_version=2
    )
    held_gold, held_manifest, _ = make_gold.build_set(
        "heldout", make_gold.DEFAULT_SEEDS["heldout"], held_dir, gold_version=2
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


@pytest.fixture(scope="module")
def gold_v3(tmp_path_factory):
    """The gold v3 set (the generator's default): the kind cue is in the gold."""
    base = tmp_path_factory.mktemp("gold-v3")
    dev_dir = base / "dev"
    held_dir = base / "heldout"
    dev_gold, dev_manifest, _ = make_gold.build_set(
        "dev", make_gold.DEFAULT_SEEDS["dev"], dev_dir, gold_version=3
    )
    held_gold, held_manifest, _ = make_gold.build_set(
        "heldout", make_gold.DEFAULT_SEEDS["heldout"], held_dir, gold_version=3
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


@pytest.fixture(scope="module")
def gold_v4(tmp_path_factory):
    """The gold v4 set (opt-in): the matrix tag is a K-option header row with
    one ``marker`` per label row, and every matrix field is ``expected_ambiguous``."""
    base = tmp_path_factory.mktemp("gold-v4")
    dev_dir = base / "dev"
    held_dir = base / "heldout"
    dev_gold, dev_manifest, _ = make_gold.build_set(
        "dev", make_gold.DEFAULT_SEEDS["dev"], dev_dir, gold_version=4
    )
    held_gold, held_manifest, _ = make_gold.build_set(
        "heldout", make_gold.DEFAULT_SEEDS["heldout"], held_dir, gold_version=4
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
    return _score(gold_doc, _load_record(record_path))["overall"]


def _score(gold_doc: dict, record: dict) -> dict:
    g = score_gold.Gold(gold_doc)
    predicted, stray, unresolved = score_gold._predicted_fields(
        g, score_gold._extract_fields(record)
    )
    disposition = score_gold._disposition_accuracy(g, record)
    return score_gold.score_document(g, predicted, stray, unresolved, disposition)


# --------------------------------------------------------------------------
# Committed expected outputs (fixed seed). Regenerate with:
#   python tools/make_gold.py --set dev --out DIR --canned DIR/canned
#   (then read the scorer's overall counters for the perfect and mutated dumps)
# --------------------------------------------------------------------------

#: sha256 of every homogeneous tab, captured before the mixed tabs were added.
#: The mixed tabs are appended and draw from their own RNG stream, so these
#: bytes must never move. Extend this table when a new homogeneous tab is added;
#: never edit a row.
OLD_TAB_SHA256 = {
    "dev": {
        "Dev01": "2dc20f1c91027f95bcf839b75bbcb9b95392894c9a0e530d9dcb81771d232cf9",
        "Dev02": "efe09b660eb64de4acbd11a92d6de4bfed2bfdd794fe266ab6144eae952b91df",
        "Dev03": "69d77c067bc2f3cb33adb8449e2b9d35082bcc74b5a7599e1bdbc5b0a89224fb",
        "Dev04": "958aed4fef885404257d8aa7bfc20faa6d7d15097d2264e9a9e92d51362c98d7",
        "Dev05": "477ac3a7db2a27c8896c012a9f9d4437231c7347a4f3bcafbe36ed3c4fa5ab3a",
        "Dev06": "1e7b76d4012bf8079d61a38e50e9eb58aaa2b33b46ff01c1bf53164d4b5bc78f",
        "Dev07": "4113cf61cb56cfd32abbe4e6d4326f74fcca81672164da5bf2a483d0bed2ac75",
        "Dev08": "f37e5dc6a382729faf9f2fea7503cc5468d7c7b9edfed941801989cb788087c0",
        "Dev09": "1a85746d11a660ab27acecb6ee5a4f6055001a6c0473a5964c94222913ca6215",
        "Dev10": "3c2a0f0c7d1eac795716e6628379cfcce9097e0b4939c8acaabe57ae7a5c75f0",
        "Dev11": "6579c6060d444a8c4f8b77422d593b8b914d558657a3519b11a0fb9e679c0a05",
        "Dev12": "45f6be470a83e35180f563ea26dbc30d453a2858398938b4cfa2ef43f52a99f8",
    },
    "heldout": {
        "Hold01": "45fca88fb2caa66ab82183809086c721d0ee14036e357890f2e09443669b1891",
        "Hold02": "3e7d1ec15807c7a2e4c9734da35ad0fb9a9b365520892afbb3f9a9d9364e2c09",
        "Hold03": "bdd6da4ea13674d1fa25d1361a7d80e0528727142ab4d0898ab67e0ab3ed881d",
        "Hold04": "752bae2209d150552995646fa80bee6723a85c02bd2895358307240414278694",
        "Hold05": "bf440d8141cad98f14b0ce83dba5baa254157f377d5d719382c08fed5aa47bb5",
        "Hold06": "36e27ae43c862f3917665385dcc39a8f39fac00b6985e95df8b6c4a15792a42c",
    },
}

#: sha256 of every tab of the gold v3 set (the generator's default). v3 changes
#: only the label wording, so any tab whose structures all keep the plain
#: question (Dev01 yes/no rows) is byte-identical to v2 and every other tab is
#: not. These rows are the v3 counterpart of OLD_TAB_SHA256 and pin the generated
#: bytes the same way.
V3_TAB_SHA256 = {
    "dev": {
        "Dev01": "2dc20f1c91027f95bcf839b75bbcb9b95392894c9a0e530d9dcb81771d232cf9",
        "Dev02": "6b2238999ccc1d5c9e50a979237a3a4006db8d4d193daf4e4c9be03a794d3c53",
        "Dev03": "6b779a6fe0844484f270c3186cb7ff6c79934f148f5e44e036c09c999b7a8398",
        "Dev04": "554d9124d29707bf5910c51682a6361ab368217ce2e6d45d9796e7a06c3eccbc",
        "Dev05": "25b70dbd5507134040c8e29e1f7cb40b2b653cfc293c2d21b283b84b62e2f842",
        "Dev06": "deef816dcd986e998e70141f25aff0c57159933512e59d1228bc53c470866563",
        "Dev07": "26b5522b082b6dd2ffb60748b518bdd2bb6d7c47eb008761131c3eccabcd44be",
        "Dev08": "470f2fcd20d6a124248ce6a9309d595478f6e1c2fbb463924e24f099bf6e8b3b",
        "Dev09": "6555763fac0a129ecb9e0db24954cc9d7e7ca037e557425960c80561f5abd8ed",
        "Dev10": "233e1085bd7026abbc9d98d891ead8eabcbe26c4e4a694fd8978a1a03df93f44",
        "Dev11": "bce355559bdd1a2715fcfddcb21837d281ffe0a44f1fe07c4e0f27a09f37ff39",
        "Dev12": "b2b2a4faae1b1dce44d63df187e20287425f22554f9d79e2436f220c8110ca3f",
        "Dev13": "6a63069fd9e1a5c457b16016e776baeefcef6c9c0fb2ddf07b9befd0473288cd",
        "Dev14": "b8f7bb1e095327bf181d2cdb7449b7d894d096ea15447183611fc04247e6d7d0",
        "Dev15": "231297fbacee9c775c32fa539d971ec4788ac28624fd733a41b64fb155c01b67",
        "Dev16": "dae6a55c166c1215c42fcd034422edcfc3b7b23aa4c5d09d32fccc5d806cbc6a",
    },
    "heldout": {
        "Hold01": "69d51aefc118e4b12b4b04ad32220f6baeee542b996abebac6675b8f03e2401a",
        "Hold02": "d86ad118394207274c6b883a04a4c95cf49f3e1126d4d05887a40684b9abadb4",
        "Hold03": "b2ac4e01ee85ed1e9e4e10d7886f28c9bd9c7c063ba84582fc266c729ee01872",
        "Hold04": "10dca1d00f99facedfdbe7183652ec6ed6545fa1a113f3beb71e5c4056b18ec3",
        "Hold05": "2b5f1bfa115f91cf113ab99e976dcbe84fee7350e7be308f701f5778d2116cfd",
        "Hold06": "2bc9ccd7942b0ced59776eec68f0827110dad0fa74d3ec38d7ec7ab5aa37fa3e",
        "Hold07": "78538ab01f26f5871d82f6f405c6e9439f5b6afdd2ed0bee4c72853c61c9abc2",
        "Hold08": "b0ad4c4fdda65d8989709f3557432b2613dd1c593a116804e0e9055178836678",
    },
}

#: sha256 of every tab of the gold v4 set (opt-in). v4 rebuilds only the
#: ``matrix`` tag, so exactly the three tabs that hold a matrix (Dev11, the mixed
#: Dev13, Hold05) differ from v3; every other tab is byte-identical because each
#: tab draws from its own seed-keyed RNG stream. These rows pin the generated
#: bytes the same way OLD_TAB_SHA256 and V3_TAB_SHA256 do.
V4_TAB_SHA256 = {
    "dev": {
        "Dev01": "2dc20f1c91027f95bcf839b75bbcb9b95392894c9a0e530d9dcb81771d232cf9",
        "Dev02": "6b2238999ccc1d5c9e50a979237a3a4006db8d4d193daf4e4c9be03a794d3c53",
        "Dev03": "6b779a6fe0844484f270c3186cb7ff6c79934f148f5e44e036c09c999b7a8398",
        "Dev04": "554d9124d29707bf5910c51682a6361ab368217ce2e6d45d9796e7a06c3eccbc",
        "Dev05": "25b70dbd5507134040c8e29e1f7cb40b2b653cfc293c2d21b283b84b62e2f842",
        "Dev06": "deef816dcd986e998e70141f25aff0c57159933512e59d1228bc53c470866563",
        "Dev07": "26b5522b082b6dd2ffb60748b518bdd2bb6d7c47eb008761131c3eccabcd44be",
        "Dev08": "470f2fcd20d6a124248ce6a9309d595478f6e1c2fbb463924e24f099bf6e8b3b",
        "Dev09": "6555763fac0a129ecb9e0db24954cc9d7e7ca037e557425960c80561f5abd8ed",
        "Dev10": "233e1085bd7026abbc9d98d891ead8eabcbe26c4e4a694fd8978a1a03df93f44",
        "Dev11": "0d9c048296dac8f4e2746bdc6fb163974fba43f66b0f731c3da671de462867f6",
        "Dev12": "b2b2a4faae1b1dce44d63df187e20287425f22554f9d79e2436f220c8110ca3f",
        "Dev13": "2eb8de4943d6e361d2a09eb25600bdbde6d8e264b5c67ba3f45cad2de0285694",
        "Dev14": "b8f7bb1e095327bf181d2cdb7449b7d894d096ea15447183611fc04247e6d7d0",
        "Dev15": "231297fbacee9c775c32fa539d971ec4788ac28624fd733a41b64fb155c01b67",
        "Dev16": "dae6a55c166c1215c42fcd034422edcfc3b7b23aa4c5d09d32fccc5d806cbc6a",
    },
    "heldout": {
        "Hold01": "69d51aefc118e4b12b4b04ad32220f6baeee542b996abebac6675b8f03e2401a",
        "Hold02": "d86ad118394207274c6b883a04a4c95cf49f3e1126d4d05887a40684b9abadb4",
        "Hold03": "b2ac4e01ee85ed1e9e4e10d7886f28c9bd9c7c063ba84582fc266c729ee01872",
        "Hold04": "10dca1d00f99facedfdbe7183652ec6ed6545fa1a113f3beb71e5c4056b18ec3",
        "Hold05": "62eae9dc0de037010e5ded405d14ac51295e9c001f8e02e0ca54932ea6901482",
        "Hold06": "2bc9ccd7942b0ced59776eec68f0827110dad0fa74d3ec38d7ec7ab5aa37fa3e",
        "Hold07": "78538ab01f26f5871d82f6f405c6e9439f5b6afdd2ed0bee4c72853c61c9abc2",
        "Hold08": "b0ad4c4fdda65d8989709f3557432b2613dd1c593a116804e0e9055178836678",
    },
}

PERFECT_COUNTERS = {
    "gold_fields": 244,
    "predicted_fields": 244,
    "matched": 244,
    "matched_ignoring_kind": 244,
    "kind_confusions": 0,
    "kind_confusion_pairs": {},
    "merge_count": 0,
    "split_count": 0,
    "missed": 0,
    "spurious": 0,
    "stray_ref_count": 0,
    "unresolved_ref_count": 0,
    "label_ok": 244,
    "label_total": 244,
    "options_ok": 244,
    "options_total": 244,
    "selected_ok": 180,
    "selected_total": 180,
    "selected_ok_under_convention": 170,
    "selected_ambiguous_expected": 10,
    "unexpected_ambiguous": 0,
    "answer_ok": 64,
    "answer_total": 64,
    "addressed_num": 1140,
    "addressed_den": 1140,
    "precision_num": 244,
    "precision_den": 244,
    "recall_num": 244,
    "recall_den": 244,
    "precision": 1.0,
    "recall": 1.0,
    "addressed": 1.0,
}

MUTATION_DELTAS = {
    "declare_nothing": {
        "selected_ok": -58, "selected_ok_under_convention": -58,
        "unexpected_ambiguous": 58,
    },
    "drop_field": {
        "predicted_fields": -1, "matched": -1, "matched_ignoring_kind": -1,
        "missed": 1, "label_ok": -1,
        "label_total": -1, "options_ok": -1, "options_total": -1,
        "selected_ok": -1, "selected_total": -1,
        "selected_ok_under_convention": -1, "addressed_num": -4,
        "precision_num": -1, "precision_den": -1, "recall_num": -1,
    },
    "duplicate_field": {
        "predicted_fields": 1, "spurious": 1, "precision_den": 1,
    },
    "empty_fields": {
        "predicted_fields": -244, "matched": -244, "matched_ignoring_kind": -244,
        "missed": 244,
        "label_ok": -244, "label_total": -244, "options_ok": -244,
        "options_total": -244, "selected_ok": -180, "selected_total": -180,
        "selected_ok_under_convention": -170, "selected_ambiguous_expected": -10,
        "answer_ok": -64, "answer_total": -64, "addressed_num": -1140,
        "precision_num": -244, "precision_den": -244, "recall_num": -244,
    },
    "merge_two": {
        "predicted_fields": -1, "matched": -2, "matched_ignoring_kind": -2,
        "merge_count": 1,
        "label_ok": -2, "label_total": -2, "options_ok": -2,
        "options_total": -2, "selected_ok": -2, "selected_total": -2,
        "selected_ok_under_convention": -2, "precision_num": -2,
        "precision_den": -2, "recall_num": -2, "recall_den": -2,
    },
    "missing_tab": {
        "predicted_fields": -14, "matched": -14, "matched_ignoring_kind": -14,
        "missed": 14,
        "label_ok": -14, "label_total": -14, "options_ok": -14,
        "options_total": -14, "selected_ok": -14, "selected_total": -14,
        "selected_ok_under_convention": -14, "addressed_num": -42,
        "precision_num": -14, "precision_den": -14, "recall_num": -14,
    },
    "split_one": {
        "predicted_fields": 1, "matched": -1, "matched_ignoring_kind": -1,
        "split_count": 1,
        "label_ok": -1, "label_total": -1, "options_ok": -1,
        "options_total": -1, "selected_ok": -1, "selected_total": -1,
        "selected_ok_under_convention": -1, "precision_num": -1,
        "precision_den": -1, "recall_num": -1, "recall_den": -1,
    },
    "stray_id": {"stray_ref_count": 1},
    "unresolved_ref": {"unresolved_ref_count": 1},
    "wrong_answer": {"answer_ok": -1},
    "wrong_kind": {
        "matched": -1, "kind_confusions": 1, "missed": 1, "spurious": 1,
        "label_ok": -1,
        "label_total": -1, "options_ok": -1, "options_total": -1,
        "selected_ok": -1, "selected_total": -1,
        "selected_ok_under_convention": -1, "precision_num": -1,
        "recall_num": -1,
    },
    "kind_swap": {
        "matched": -116, "kind_confusions": 116, "missed": 116, "spurious": 116,
        "label_ok": -116, "label_total": -116, "options_ok": -116,
        "options_total": -116, "selected_ok": -116, "selected_total": -116,
        "selected_ok_under_convention": -106, "selected_ambiguous_expected": -10,
        "precision_num": -116, "recall_num": -116,
    },
    "wrong_label": {"label_ok": -1},
    "wrong_selected": {"selected_ok": -1, "selected_ok_under_convention": -1},
}


# --------------------------------------------------------------------------
# Generator determinism
# --------------------------------------------------------------------------

def test_generator_is_deterministic(tmp_path):
    """Both gold versions: the same seed is byte-identical, a different seed is not."""
    for version in make_gold.GOLD_VERSIONS:
        one, _, _ = make_gold.build_set("dev", 20260101, tmp_path / f"a{version}",
                                        gold_version=version)
        two, _, _ = make_gold.build_set("dev", 20260101, tmp_path / f"b{version}",
                                        gold_version=version)
        three, _, _ = make_gold.build_set("dev", 999, tmp_path / f"c{version}",
                                          gold_version=version)

        assert one == two, version
        for tab in one["tabs"]:
            assert (tmp_path / f"a{version}" / f"{tab}.xlsx").read_bytes() == (
                tmp_path / f"b{version}" / f"{tab}.xlsx"
            ).read_bytes(), (version, tab)
            assert (tmp_path / f"a{version}" / f"{tab}.xlsx").read_bytes() != (
                tmp_path / f"c{version}" / f"{tab}.xlsx"
            ).read_bytes(), (version, tab)
        assert one != three, version


def test_v3_generator_is_deterministic(tmp_path):
    """v3 specifically: the default build is the same seed twice, a different seed differs."""
    a, _, _ = make_gold.build_set("dev", 20260202, tmp_path / "a")
    b, _, _ = make_gold.build_set("dev", 20260202, tmp_path / "b")
    c, _, _ = make_gold.build_set("dev", 20260303, tmp_path / "c")
    assert a == b
    assert a["gold_version"] == 3
    assert a != c
    for tab in a["tabs"]:
        assert (tmp_path / "a" / f"{tab}.xlsx").read_bytes() == (
            tmp_path / "b" / f"{tab}.xlsx"
        ).read_bytes()
        assert (tmp_path / "a" / f"{tab}.xlsx").read_bytes() != (
            tmp_path / "c" / f"{tab}.xlsx"
        ).read_bytes()


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


# --------------------------------------------------------------------------
# Content hashes: what the tab-byte tables above pin, without the compressor.
#
# The zip bytes of a generated tab depend on the interpreter's zlib: CPython 3.14 on Windows
# bundles zlib-ng and stock 3.11 / Linux builds do not, so the same workbook compresses to
# different bytes (the release workflow's CI failed on exactly this, 2026-10-08, while the
# UNCOMPRESSED members were identical on 3.11 and 3.14). The content hash below is sha256 over
# every member's name and uncompressed bytes in name order. It was derived from the generator
# in the environment where the three zip tables above still hold (the derivation asserted
# that first), so it pins the same tabs. The zip-byte tables stay, checked only on a zlib-ng
# build, where they are valid.
# --------------------------------------------------------------------------

import hashlib as _hashlib  # noqa: E402
import zipfile as _zipfile  # noqa: E402
import zlib as _zlib  # noqa: E402

_ZLIB_NG = getattr(_zlib, "ZLIBNG_VERSION", None) is not None


def _content_sha256(path) -> str:
    with _zipfile.ZipFile(path) as archive:
        digest = _hashlib.sha256()
        for name in sorted(archive.namelist()):
            digest.update(name.encode() + b"\x00" + archive.read(name) + b"\x00")
    return digest.hexdigest()


OLD_CONTENT_SHA256 = {
    "dev": {
        "Dev01": "606af82765a4e8c87904b2e0da58ea896df1ba04651c79b390877f0796c50f4a",
        "Dev02": "2cdb0b921191aeabdadb88ee5fd5dbc4dbbf69544d681d459f940b63ad50fc70",
        "Dev03": "54031b8c1fb66233786dddeef15523926b98dfaf4f53531c3af3d78d814b630a",
        "Dev04": "31a97bd9738a0bab11d23fd5dd74f5e717e00217299ca75d7727084df9de889e",
        "Dev05": "c9ef70fe5bac9ac0e135f3680b0789c659a67ab8d8cc79a807a6563945bca083",
        "Dev06": "58338e9b608f62049e01e75209b55b8b9e4242cebb4fb12177c80ec6c1db3eed",
        "Dev07": "f58f8298e2a8ad0b46876612831d34c4f970cc653e536ee8c6c5d0b8590f7a0a",
        "Dev08": "d9ca2f74a9748afca0796b706c56e9d53a53544314b3e6a3a2e24fe88e8a841b",
        "Dev09": "8a46e4966ba17625b18e9b19f223f93c56708db634e98adc8ef055282d9cb6b7",
        "Dev10": "808756d30f9c609aba34ccc7386b32494669978afacfbc86fef16c3f751912a6",
        "Dev11": "ac526e7eda8fbf87344d0a82d5c2ca0e2cc850fad0a0a72d463f1fcec7faf2aa",
        "Dev12": "48f52997ea19559bba0a91fec06be85c0c4f54bd1fca5cae60f602490df54319",
    },
    "heldout": {
        "Hold01": "aadaf907d5a8ba04c1f8f683df19793a24ebc08ce734debcfec5573fb77cd404",
        "Hold02": "ed3f6b6a76ca431ebcc823336688a70697b19864f183e0148c08179ee6379871",
        "Hold03": "7dba1133217bf0ab260b75f943eb4f1a3eefc3befeeb7d13b5f3f8593f1b462a",
        "Hold04": "20f1acbe16b7ac8f4e60cbb41fc3c2672c27f5b295b2704f5cf7221f927880d1",
        "Hold05": "e1d1d2bdf1b5d5447942607156aa2c9afd9863a6971ade936960b9e5a16f2d64",
        "Hold06": "f7608301b1a478e9dba724e69f1591112ac2f31ebdd3d57bb2e9d893f78e2ad8",
    },
}

V3_CONTENT_SHA256 = {
    "dev": {
        "Dev01": "606af82765a4e8c87904b2e0da58ea896df1ba04651c79b390877f0796c50f4a",
        "Dev02": "8b7e60226f9644d05d4d411b61c0644dfe1e4d32c6bf833c258a647bbabbb1b5",
        "Dev03": "16b60eef7e963e8d0fea99d01ddba11d21a5e400e371d79289bd75500bd976dc",
        "Dev04": "6dbc54e6e58f5fc2aeb94cfb638eea42b7d98ed6b3389d4938ab5eca066c4f05",
        "Dev05": "01db6afd59ff3dfc158da99ff933eeb4b2c1d4a252d9bfa650215e91e03a62b2",
        "Dev06": "db7302d98eb64f5d3947667c6dfc72d82b8790a82be9467cb52a17591c58c8ea",
        "Dev07": "1759ab6d5404cbecfbc464c49fbb3d21588a230da9db2c7f1c054c034fc5a408",
        "Dev08": "1cc2eb1d84412fb9ea9da3b56823208906bae53419bae82b17183331995c459b",
        "Dev09": "580e9c029b7e79975cd62b3c849829e1a3366f0bf2794f1c4780a162c598b9a5",
        "Dev10": "0c080c30cb590e536874e80cc8cd775d5a9477dc8126f696d7781f0fa7e85912",
        "Dev11": "bfd83699eeb0b31f67ad72d5282ed76c4ed3460481edd42fd16f7c6ac2655300",
        "Dev12": "59d82a4a495bec266b990f204c92c9a2d661c81c7779d721367d13ccb343a1e7",
        "Dev13": "2ed68c666e02309e288852967d0f67649685b163b8bcb41066c1eecad88592b8",
        "Dev14": "dd5e5956efd79adedf0f5f18b05a2d27bab76a31b1bea6c5d449427bbf6d66bf",
        "Dev15": "c254be120fa088ce02f3bfa8668b7d1616680c92cfa13882ff23ab07aaf9d27a",
        "Dev16": "7378c95606a022d9a6d9ed36887f6fd74b0b097880bda77a802abae031a8f142",
    },
    "heldout": {
        "Hold01": "a98488cac1771ef517a7e0ced1cffacec2ea032bd7f08066d7896a135e19d87c",
        "Hold02": "511377efe784d39daa601029fbc4b5bab4d3c7f892b3aa18a164cf43aafa8004",
        "Hold03": "fb990a39014d95dabe9e4e750f41167930b7c1bd1c6342bf1ac13693a26ba64d",
        "Hold04": "23f7ba8335a1d092f58a8b47c6d4d4c2c251403e613024d61f745144b3f773db",
        "Hold05": "e2bf383df139197051b41697e27835aeaae619a152dc698418f34cc9962a81f7",
        "Hold06": "0bca95772700a02bc7ecede87815ec7a2a7295761166147b1ce10134cc373d74",
        "Hold07": "1e2ed2613ffc54e56155b7c614efa05d1ffa1f7a6c03d6e1e77e774729d8ec25",
        "Hold08": "4381328c3bebd561c274b195a7492c112cc4ec4f49427f785634bff0fccfc5da",
    },
}

V4_CONTENT_SHA256 = {
    "dev": {
        "Dev01": "606af82765a4e8c87904b2e0da58ea896df1ba04651c79b390877f0796c50f4a",
        "Dev02": "8b7e60226f9644d05d4d411b61c0644dfe1e4d32c6bf833c258a647bbabbb1b5",
        "Dev03": "16b60eef7e963e8d0fea99d01ddba11d21a5e400e371d79289bd75500bd976dc",
        "Dev04": "6dbc54e6e58f5fc2aeb94cfb638eea42b7d98ed6b3389d4938ab5eca066c4f05",
        "Dev05": "01db6afd59ff3dfc158da99ff933eeb4b2c1d4a252d9bfa650215e91e03a62b2",
        "Dev06": "db7302d98eb64f5d3947667c6dfc72d82b8790a82be9467cb52a17591c58c8ea",
        "Dev07": "1759ab6d5404cbecfbc464c49fbb3d21588a230da9db2c7f1c054c034fc5a408",
        "Dev08": "1cc2eb1d84412fb9ea9da3b56823208906bae53419bae82b17183331995c459b",
        "Dev09": "580e9c029b7e79975cd62b3c849829e1a3366f0bf2794f1c4780a162c598b9a5",
        "Dev10": "0c080c30cb590e536874e80cc8cd775d5a9477dc8126f696d7781f0fa7e85912",
        "Dev11": "dfb3d9fd9009a0b796ebcac12a3b0e0532e272fa25da574b648186c4e02799b3",
        "Dev12": "59d82a4a495bec266b990f204c92c9a2d661c81c7779d721367d13ccb343a1e7",
        "Dev13": "2d9fc696a563b6c06fe65fa634fe84c3cc45a00d74e0bb995f2d688936bcb463",
        "Dev14": "dd5e5956efd79adedf0f5f18b05a2d27bab76a31b1bea6c5d449427bbf6d66bf",
        "Dev15": "c254be120fa088ce02f3bfa8668b7d1616680c92cfa13882ff23ab07aaf9d27a",
        "Dev16": "7378c95606a022d9a6d9ed36887f6fd74b0b097880bda77a802abae031a8f142",
    },
    "heldout": {
        "Hold01": "a98488cac1771ef517a7e0ced1cffacec2ea032bd7f08066d7896a135e19d87c",
        "Hold02": "511377efe784d39daa601029fbc4b5bab4d3c7f892b3aa18a164cf43aafa8004",
        "Hold03": "fb990a39014d95dabe9e4e750f41167930b7c1bd1c6342bf1ac13693a26ba64d",
        "Hold04": "23f7ba8335a1d092f58a8b47c6d4d4c2c251403e613024d61f745144b3f773db",
        "Hold05": "11fd17bf8d61caf33bf6f64c3617bdccc6bcfaeabe8cdb30de811f6438a2d775",
        "Hold06": "0bca95772700a02bc7ecede87815ec7a2a7295761166147b1ce10134cc373d74",
        "Hold07": "1e2ed2613ffc54e56155b7c614efa05d1ffa1f7a6c03d6e1e77e774729d8ec25",
        "Hold08": "4381328c3bebd561c274b195a7492c112cc4ec4f49427f785634bff0fccfc5da",
    },
}


def _check_content(fixture, table):
    for set_name, dir_key in (("dev", "dev_dir"), ("heldout", "held_dir")):
        for tab, expected in table[set_name].items():
            got = _content_sha256(fixture[dir_key] / f"{tab}.xlsx")
            assert got == expected, (tab, got, expected)


def _check_zip(fixture, table, key_pairs=(("dev", "dev_manifest"), ("heldout", "held_manifest"))):
    """The zip-byte tables hold only where the compressor matches the one that pinned them."""
    if not _ZLIB_NG:
        return
    for set_name, key in key_pairs:
        sha = fixture[key]["tab_sha256"]
        for tab, expected in table[set_name].items():
            assert sha[tab] == expected, (tab, sha[tab], expected)


def test_v2_tabs_are_byte_identical(gold):
    _check_content(gold, OLD_CONTENT_SHA256)
    _check_zip(gold, OLD_TAB_SHA256)
    for tab in make_gold.MIXED_DEV_TABS:
        assert tab not in OLD_TAB_SHA256["dev"]


def test_v3_tabs_have_a_committed_sha256_table(gold_v3):
    for set_name, key in (("dev", "dev_manifest"), ("heldout", "held_manifest")):
        sha = gold_v3[key]["tab_sha256"]
        assert set(sha) == set(gold_v3[key]["tabs"])
    _check_content(gold_v3, V3_CONTENT_SHA256)
    _check_zip(gold_v3, V3_TAB_SHA256)
    # v3 changes only the wording: only the tab whose structures all keep the
    # plain question (Dev01, yes/no rows) keeps v2's bytes, so the table is not
    # merely the v2 table again.
    assert V3_TAB_SHA256["dev"]["Dev01"] == OLD_TAB_SHA256["dev"]["Dev01"]
    assert V3_TAB_SHA256["dev"]["Dev02"] != OLD_TAB_SHA256["dev"]["Dev02"]
    assert V3_TAB_SHA256["heldout"]["Hold01"] != OLD_TAB_SHA256["heldout"]["Hold01"]
    same_as_v2 = {
        tab
        for tab, sha in V3_TAB_SHA256["dev"].items()
        if OLD_TAB_SHA256["dev"].get(tab) == sha
    }
    assert same_as_v2 == {"Dev01"}


def test_v3_field_counts_per_tag_equal_v2(gold, gold_v3):
    assert gold["dev_manifest"]["fields"] == gold_v3["dev_manifest"]["fields"]
    assert gold["dev_manifest"]["cells"] == gold_v3["dev_manifest"]["cells"]
    assert gold["dev_manifest"]["tab_counts"] == gold_v3["dev_manifest"]["tab_counts"]
    assert gold["dev_manifest"]["tag_counts"] == gold_v3["dev_manifest"]["tag_counts"]
    assert gold["held_manifest"]["fields"] == gold_v3["held_manifest"]["fields"]
    assert gold["held_manifest"]["tag_counts"] == gold_v3["held_manifest"]["tag_counts"]


def test_v4_tabs_have_a_committed_sha256_table(gold_v4):
    for set_name, key in (("dev", "dev_manifest"), ("heldout", "held_manifest")):
        sha = gold_v4[key]["tab_sha256"]
        assert set(sha) == set(gold_v4[key]["tabs"])
    _check_content(gold_v4, V4_CONTENT_SHA256)
    _check_zip(gold_v4, V4_TAB_SHA256)


def test_v4_leaves_non_matrix_tabs_byte_identical_to_v3(gold_v3, gold_v4):
    """Only the tabs that themselves hold a matrix may move; the rest are v3.

    Each tab draws from its own seed-keyed RNG stream, so rebuilding the matrix
    builders cannot perturb a tab that does not run one.
    """
    for set_name, key in (("dev", "dev_manifest"), ("heldout", "held_manifest")):
        doc = gold_v4[f"{'dev' if set_name == 'dev' else 'held'}_gold"]
        matrix_tabs = {f["tab"] for f in doc["fields"] if "matrix" in f["tags"]}
        v4_sha = gold_v4[key]["tab_sha256"]
        v3_sha = gold_v3[key]["tab_sha256"]
        for tab in gold_v4[key]["tabs"]:
            if tab in matrix_tabs:
                assert v4_sha[tab] != v3_sha[tab], tab
            else:
                assert v4_sha[tab] == v3_sha[tab], tab
    dev_matrix = {
        f["tab"] for f in gold_v4["dev_gold"]["fields"] if "matrix" in f["tags"]
    }
    held_matrix = {
        f["tab"] for f in gold_v4["held_gold"]["fields"] if "matrix" in f["tags"]
    }
    assert dev_matrix == {"Dev11", "Dev13"}
    assert held_matrix == {"Hold05"}


def test_v4_field_counts_per_tag_equal_v3(gold_v3, gold_v4):
    """v4 changes the matrix shape, not how many matrix rows a tab holds."""
    assert gold_v3["dev_manifest"]["fields"] == gold_v4["dev_manifest"]["fields"]
    assert gold_v3["dev_manifest"]["tab_counts"] == gold_v4["dev_manifest"]["tab_counts"]
    assert gold_v3["dev_manifest"]["tag_counts"] == gold_v4["dev_manifest"]["tag_counts"]
    assert gold_v3["held_manifest"]["fields"] == gold_v4["held_manifest"]["fields"]
    assert gold_v3["held_manifest"]["tag_counts"] == gold_v4["held_manifest"]["tag_counts"]
    assert gold_v4["dev_manifest"]["tag_counts"]["matrix"] == 7
    assert gold_v4["held_manifest"]["tag_counts"]["matrix"] == 3


def _rc(cell_id: str) -> tuple[int, int]:
    _, rc = cell_id.split("!", 1)
    row, col = rc.split(":", 1)
    return int(row), int(col)


def test_v4_dev_and_heldout_vocabularies_are_disjoint(gold_v4):
    dev = _label_option_texts(gold_v4["dev_gold"])
    held = _label_option_texts(gold_v4["held_gold"])
    assert dev & held == set()


def test_v4_matrix_is_a_header_row_of_options_with_one_mark_per_label(gold_v4):
    """Every v4 matrix row: K option cells in one header row above the labels,
    exactly one marker under the selected header, and an expected-ambiguous gold."""
    for doc in (gold_v4["dev_gold"], gold_v4["held_gold"]):
        cells = {c["id"]: c for c in doc["cells"]}
        fields = doc["fields"]
        # The scorer keys a field by label + option cells: a duplicate identity
        # would silently make a gold field unreachable (a negative kind_confusion).
        identities = {
            (f["tab"], frozenset(f["label_cells"] + f["option_cells"])) for f in fields
        }
        assert len(identities) == len(fields)

        by_tab: dict[str, list[dict]] = {}
        for f in fields:
            if "matrix" in f["tags"]:
                by_tab.setdefault(f["tab"], []).append(f)
        assert by_tab, "no matrix fields"
        for tab, group in by_tab.items():
            # one shared header row of K >= 2 options, the same on every row
            assert len({tuple(f["option_cells"]) for f in group}) == 1, tab
            headers = group[0]["option_cells"]
            assert len(headers) >= 2, tab
            header_rows = {_rc(c)[0] for c in headers}
            assert len(header_rows) == 1, tab
            header_row = header_rows.pop()
            assert all(cells[c]["role"] == "option" for c in headers), tab
            for f in group:
                assert f["kind"] == "single", f
                assert f["expected_ambiguous"] is True, f
                assert f["kind_cue"] == "choose_one", f
                assert cells[f["label_cells"][0]]["text"].endswith("(choose one)"), f
                row, col = _rc(f["label_cells"][0])
                assert col == 1 and row > header_row, f
                selected = f["selected_option_cells"]
                assert len(selected) == 1 and selected[0] in f["option_cells"], f
                marker_id = f"{tab}!{row}:{_rc(selected[0])[1]}"
                assert marker_id in cells, (f["field_id_gold"], marker_id)
                assert cells[marker_id]["role"] == "marker", marker_id
                assert cells[marker_id]["text"] == make_gold.MARKER, marker_id
                assert marker_id not in f["option_cells"], f
            # each matrix label row carries its own marker (never a shared one)
            marker_ids = [
                f"{tab}!{_rc(f['label_cells'][0])[0]}:{_rc(f['selected_option_cells'][0])[1]}"
                for f in group
            ]
            assert len(set(marker_ids)) == len(group), (tab, marker_ids)


def test_v2_gold_has_no_kind_cue_and_v3_has_one(gold, gold_v3):
    assert all("kind_cue" not in f for f in gold["dev_gold"]["fields"])
    assert all("kind_cue" in f for f in gold_v3["dev_gold"]["fields"])
    assert gold["dev_gold"]["gold_version"] == 2
    assert gold_v3["dev_gold"]["gold_version"] == 3


def test_v3_dev_and_heldout_vocabularies_are_disjoint(gold_v3):
    dev = _label_option_texts(gold_v3["dev_gold"])
    held = _label_option_texts(gold_v3["held_gold"])
    assert dev & held == set()


def test_v3_keeps_the_conventions_and_print_conventions_roundtrip(gold_v3, capsys):
    doc = gold_v3["dev_gold"]
    conventions = doc["checkbox_conventions"]
    assert [c["tab"] for c in conventions] == doc["tabs"]
    for entry in conventions:
        assert set(entry) == {"tab", "anchor_pattern", "convention"}
        assert entry["convention"] == "mark_precedes_option"
    assert make_gold.main(
        ["--print-conventions", str(gold_v3["dev_dir"] / "gold_dev.json")]
    ) == 0
    assert json.loads(capsys.readouterr().out) == conventions


def test_manifest_declares_homogeneous_and_mixed_tabs(gold):
    dev = gold["dev_manifest"]
    assert dev["mixed_tabs"] == list(make_gold.MIXED_DEV_TABS)
    assert dev["homogeneous_tabs"] == [
        tab for tab in dev["tabs"] if tab not in make_gold.MIXED_TABS
    ]
    assert set(dev["homogeneous_tabs"]) | set(dev["mixed_tabs"]) == set(dev["tabs"])
    assert set(dev["homogeneous_tabs"]) & set(dev["mixed_tabs"]) == set()
    assert gold["held_manifest"]["mixed_tabs"] == list(make_gold.MIXED_HELD_TABS)


def test_mixed_tabs_have_the_required_shapes(gold):
    doc = gold["dev_gold"]
    mixed: dict[str, list] = {tab: [] for tab in make_gold.MIXED_DEV_TABS}
    for field in doc["fields"]:
        if field["tab"] in mixed:
            mixed[field["tab"]].append(field)
    assert set(mixed) == set(make_gold.MIXED_DEV_TABS)
    for tab, fields in mixed.items():
        workbook = openpyxl.load_workbook(filename=str(gold["dev_dir"] / f"{tab}.xlsx"))
        rows = workbook.active.max_row
        workbook.close()
        assert 60 <= rows <= 100, (tab, rows)
        structures = {t for f in fields for t in f["tags"] if t != "mixed_tab"}
        assert len(structures) >= 5, (tab, structures)
        # the two long-form shapes are present in every mixed tab
        assert "grid_50" in structures, (tab, structures)
        assert sum(1 for f in fields if "yes_no_row" in f["tags"]) >= 10, tab


def test_mixed_fields_also_carry_their_structure_tag(gold):
    for field in gold["dev_gold"]["fields"]:
        if field["tab"] in make_gold.MIXED_DEV_TABS:
            assert "mixed_tab" in field["tags"], field["field_id_gold"]
            assert [t for t in field["tags"] if t != "mixed_tab"], field["field_id_gold"]
        else:
            assert "mixed_tab" not in field["tags"], field["field_id_gold"]


def test_mixed_tabs_add_no_shared_label_option_text(gold):
    # the existing disjointness test already covers the mixed tabs; restate it
    # explicitly so a future held-out mix cannot quietly share a string
    dev = _label_option_texts(gold["dev_gold"])
    held = _label_option_texts(gold["held_gold"])
    assert dev & held == set()


def test_gold_declares_one_checkbox_convention_per_tab(gold):
    doc = gold["dev_gold"]
    conventions = doc["checkbox_conventions"]
    assert [c["tab"] for c in conventions] == doc["tabs"]
    for entry in conventions:
        assert set(entry) == {"tab", "anchor_pattern", "convention"}
        assert entry["anchor_pattern"] is None
        assert entry["convention"] == "mark_precedes_option"


def test_print_conventions_roundtrips_through_normalize(gold, capsys):
    from formextract.pipeline import _normalize_checkbox_conventions

    gold_path = gold["dev_dir"] / "gold_dev.json"
    assert make_gold.main(["--print-conventions", str(gold_path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == gold["dev_gold"]["checkbox_conventions"]
    normalized = _normalize_checkbox_conventions(printed)
    assert [c.tab for c in normalized] == gold["dev_gold"]["tabs"]


def test_perfect_dump_selected_split_is_consistent(gold):
    overall = _overall(
        gold["dev_gold"], gold["dev_dir"] / "canned" / "perfect_dev.json"
    )
    assert overall["selected_ok"] == (
        overall["selected_ok_under_convention"]
        + overall["selected_ambiguous_expected"]
    )
    assert overall["selected_ambiguous_expected"] > 0
    assert overall["unexpected_ambiguous"] == 0


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


def test_matched_ignoring_kind_is_matched_plus_kind_confusions(gold):
    """The identity holds on the perfect dump and on every mutation."""
    perfect = make_gold.perfect_record(gold["dev_gold"])
    reports = [("perfect", _score(gold["dev_gold"], perfect))]
    for name, rec in make_gold.mutations(gold["dev_gold"]).items():
        reports.append((name, _score(gold["dev_gold"], rec)))
    checked = 0
    for name, report in reports:
        o = report["overall"]
        assert o["matched_ignoring_kind"] == o["matched"] + o["kind_confusions"], name
        assert o["matched_ignoring_kind"] >= o["matched"], name
        assert sum(o["kind_confusion_pairs"].values()) == o["kind_confusions"], name
        checked += 1
    assert checked >= len(MUTATION_DELTAS) + 1


def test_kind_swap_moves_only_the_kind_counters(gold):
    """Every single read as a multi: grouping intact, the kind counters alone move."""
    perfect = make_gold.perfect_record(gold["dev_gold"])
    base = _score(gold["dev_gold"], perfect)["overall"]
    swapped = _score(gold["dev_gold"], make_gold.mutations(gold["dev_gold"])["kind_swap"])["overall"]

    n_single = sum(1 for f in perfect["fields"] if f["control_type"] == "single_select")
    assert n_single > 0
    assert swapped["matched_ignoring_kind"] == base["matched_ignoring_kind"]
    assert swapped["matched"] == base["matched"] - n_single
    assert swapped["kind_confusions"] == base["kind_confusions"] + n_single
    assert swapped["kind_confusion_pairs"] == {"single>multi": n_single}


def test_kind_confusion_pairs_vocabulary_is_closed(gold):
    assert tuple(score_gold.KIND_VOCAB) == ("single", "multi", "bool", "text")
    allowed = set(score_gold.KIND_VOCAB) | {"none"}
    seen = set()
    for name, rec in make_gold.mutations(gold["dev_gold"]).items():
        pairs = _score(gold["dev_gold"], rec)["overall"]["kind_confusion_pairs"]
        for key in pairs:
            left, _, right = key.partition(">")
            assert left in score_gold.KIND_VOCAB, (name, key)
            assert right in allowed, (name, key)
            seen.add(key)
    assert {"single>multi", "single>bool"} <= seen


def test_text_output_reports_the_kind_counters(gold, capsys):
    record = gold["dev_dir"] / "canned" / "kind_swap_dev.json"
    code = score_gold.main(
        ["--gold", str(gold["dev_dir"] / "gold_dev.json"), "--record", str(record)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "matched_ignoring_kind 244" in out
    assert "kind_confusions 116" in out
    assert "kind_confusion_pairs single>multi=116" in out
    # json carries the table too
    code = score_gold.main(
        ["--gold", str(gold["dev_dir"] / "gold_dev.json"), "--record", str(record),
         "--json"]
    )
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["overall"]["kind_confusion_pairs"] == {"single>multi": 116}


# --------------------------------------------------------------------------
# Gold v3: the kind cue in the label and in the gold
# --------------------------------------------------------------------------

def _cue_words() -> set[str]:
    return {word.lower() for word in make_gold.CUE_TEMPLATE_WORDS}


def _template_words() -> set[str]:
    """Every word the cue templates add, read from the generator's own output."""
    import random

    pool = {kind: [f"{kind}x"] for kind in ("verb", "noun", "adj", "yesno", "typed")}
    words = random.Random(0)
    introduced: set[str] = set()
    for cue in make_gold.KIND_CUES:
        if cue == "plain_question":
            continue
        text = make_gold.Words(pool, words, 3).label(cue)
        introduced.update(w for w in WORD.findall(text.lower()) if not w.endswith("x"))
    return introduced


def test_cue_words_are_declared_and_are_what_the_templates_add():
    """The declared cue-word list is exactly what the templates introduce."""
    assert _cue_words() == _template_words()


def test_cue_words_are_disjoint_from_the_variant_examples():
    """No cue or template word appears in a variant's example lines / literals."""
    disjoint = _variant_copyable_words()
    assert disjoint, "the extraction found no example line"
    assert _cue_words() & disjoint == set(), sorted(_cue_words() & disjoint)
    # non-vacuous: the variant examples really do carry label-like words
    assert "single" in disjoint and "yes" in disjoint


def test_v3_cue_rule_per_tag_from_the_gold(gold_v3):
    """Every structure tag's label carries its cue, read from the gold cells."""
    doc = gold_v3["dev_gold"]
    cells = {c["id"]: c for c in doc["cells"]}
    seen: set[str] = set()
    for field in doc["fields"]:
        tag = next(t for t in field["tags"] if t in make_gold.TAG_KIND_CUE)
        cue = make_gold.TAG_KIND_CUE[tag]
        assert field["kind_cue"] == cue, (tag, field["kind_cue"], cue)
        label = " ".join(cells[c]["text"] for c in field["label_cells"])
        if cue == "select_all_that_apply":
            assert "(select all that apply)" in label, (tag, label)
        elif cue == "choose_one":
            assert "(choose one)" in label, (tag, label)
        elif cue == "imperative":
            assert not label.rstrip().endswith("?"), (tag, label)
        else:
            assert label.rstrip().endswith("?"), (tag, label)
        seen.add(tag)
    assert set(make_gold.TAG_KIND_CUE) <= seen

    # the two structures the scorer measured as a coin flip on the sheet
    for tag in ("dense_multi", "stacked_multi", "grid_50"):
        labels = [
            " ".join(cells[c]["text"] for c in f["label_cells"])
            for f in doc["fields"] if tag in f["tags"]
        ]
        assert labels and all("(select all that apply)" in t for t in labels), tag
    for tag in ("text_field", "merged_tall", "label_two_rows", "label_two_cells"):
        labels = [
            " ".join(cells[c]["text"] for c in f["label_cells"])
            for f in doc["fields"] if tag in f["tags"]
        ]
        assert labels and all(not t.rstrip().endswith("?") for t in labels), tag
    # the typed-value rows are the one ``bool`` structure and read as a question
    for tag in ("typed_value",):
        labels = [
            " ".join(cells[c]["text"] for c in f["label_cells"])
            for f in doc["fields"] if tag in f["tags"]
        ]
        assert labels and all(t.rstrip().endswith("?") for t in labels), tag


def test_v3_bool_labels_are_questions_and_text_labels_are_not(gold_v3):
    """Every ``bool`` field reads as the plain question; no ``text`` label does.

    The gold kinds are the contract the models are scored against, so the label
    must not contradict the kind: a noun phrase under a typed answer reads as a
    text field, which is why the typed-value rows keep the v2 question wording.
    Both the label's ``?`` and the recorded ``kind_cue`` agree with the kind.
    """
    seen: set[str] = set()
    for key in ("dev_gold", "held_gold"):
        doc = gold_v3[key]
        cells = {c["id"]: c for c in doc["cells"]}
        for field in doc["fields"]:
            if field["kind"] not in ("bool", "text"):
                continue
            label = " ".join(cells[c]["text"] for c in field["label_cells"]).rstrip()
            if field["kind"] == "bool":
                assert label.endswith("?"), (key, field["field_id_gold"], label)
                assert field["kind_cue"] == "plain_question", (
                    key, field["field_id_gold"], field["kind_cue"],
                )
            else:
                assert not label.endswith("?"), (key, field["field_id_gold"], label)
                assert field["kind_cue"] != "plain_question", (
                    key, field["field_id_gold"], field["kind_cue"],
                )
            seen.add(field["kind"])
    assert seen == {"bool", "text"}  # anti-vacuity: both kinds are exercised


def test_v3_kind_cue_vocabulary_is_closed(gold_v3):
    cues = {f["kind_cue"] for f in gold_v3["dev_gold"]["fields"]}
    assert cues <= set(make_gold.KIND_CUES)
    # every cue a structure tag maps to is exercised at least once ...
    assert cues == set(make_gold.TAG_KIND_CUE.values())
    # ... and ``noun_phrase`` is declared but deliberately unassigned: a noun
    # phrase under a typed answer reads as a text field
    assert "noun_phrase" in make_gold.KIND_CUES
    assert "noun_phrase" not in set(make_gold.TAG_KIND_CUE.values())


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

def _tool_imports(name: str) -> set[str]:
    import ast

    source = (REPO / "tools" / name).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    return imported


def test_tools_import_neither_package_nor_each_other():
    for name in ("make_gold.py", "score_gold.py"):
        imported = _tool_imports(name)
        assert "formextract" not in imported, name
        assert "make_gold" not in imported, name
        assert "score_gold" not in imported, name
        assert "live_probe" not in imported, name

    scorer = (REPO / "tools" / "score_gold.py").read_text(encoding="utf-8")
    assert "import openpyxl" not in scorer
    generator = (REPO / "tools" / "make_gold.py").read_text(encoding="utf-8")
    assert "import openpyxl" in generator

    # The direction: the probe MAY import the scorer, the scorer must never
    # import the probe (or the package, or the generator).
    probe_imports = _tool_imports("live_probe.py")
    assert "score_gold" in probe_imports
    assert "live_probe" not in _tool_imports("score_gold.py")
    assert "formextract" in probe_imports
