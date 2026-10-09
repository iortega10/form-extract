"""0.6.0-L3a: the lines-contract prompt variants and the machinery behind them.

The reviewer ran the L1b lines contract live on the dev gold (Gemini 3.5
Flash-Lite: 74 of 244 fields, one unresolved ref) and read what the model wrote
on five tabs that scored zero. This module pins the machinery that turns those
observations into candidate prompts -- a closed registry of variants, a
projection option, a config field and a cache-key token -- and the offline
properties a variant must have before a live run is worth spending on.

What this module does NOT do is measure a variant: no model is called anywhere
here. A prompt only earns a default by being measured live
(``tools/live_probe.py --gold DIR --contract lines --prompt-variant NAME``), and
the tuning protocol (dev only; held-out once per frozen variant; a revision is a
NEW name) is recorded in the design note.

The two frozen byte expectations (the base prompt's sha256 and the two canned
record dumps) were produced on HEAD ``c5324e4`` -- the last commit before this
turn -- by running this turn's own probe against a ``git archive HEAD`` tree and
against the working tree: identical output for the json contract, the lines
contract at ``base``, the base prompt and the default cache keys.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.styles import Color, PatternFill

REPO = Path(__file__).resolve().parents[1]
for _root in (REPO, REPO / "tools"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

import gold_lines  # noqa: E402
import make_gold  # noqa: E402

from formextract.ingest import ingest  # noqa: E402
from formextract.layout import analyze  # noqa: E402
from formextract.lines import parse_lines  # noqa: E402
from formextract.model import InstanceStatus, PIPELINE_VERSION, to_json  # noqa: E402
from formextract.pipeline import (  # noqa: E402
    PROMPT_VARIANTS,
    Pipeline,
    PipelineConfig,
    compute_cache_key,
    validate_prompt_variant,
)
from formextract.resolve import (  # noqa: E402
    LINES_PROMPT_VARIANTS,
    LINES_VARIANT_ROW_TAGS,
    LINES_VARIANT_STYLE_TAGS,
    PROMPT_VERSION,
    LLMResponse,
    ProjectionChunk,
    build_lines_prompt,
    lines_variant_row_tags,
    lines_variant_style_tags,
    project_lines_chunks,
)
from formextract.store import Store  # noqa: E402

# --------------------------------------------------------------------------
# Fixed inputs and the byte expectations frozen from HEAD c5324e4.
# --------------------------------------------------------------------------

#: The chunk every prompt-size assertion is measured on: the same short chunk
#: for every variant, so the ratio is the prose's, not the tab's.
CHUNK = ProjectionChunk(key="Tiny", text="0: 0=Alpha widget variant? | 1=X | 2=Ruby")

#: ``sha256(build_lines_prompt(CHUNK))`` (1768 chars) on HEAD c5324e4.
BASE_PROMPT_CHARS = 1768
BASE_PROMPT_SHA256 = "5231a25717efe07cd64296ad66fdec556ebb0922efa3c92f6a60c8190e5cb42b"

#: ``sha256`` of the other shipped variants' prompts on ``CHUNK``, also frozen
#: from HEAD c5324e4. The style tag is a new variant, so only the four shipped
#: ones are pinned here: ``fix2_notags`` shares ``fix2``'s text byte for byte.
FIX1_PROMPT_SHA256 = "339bb3c0346da1ae3dd434809d5b72de81ce9c9260fc587d5f78397d8fefcd48"
FIX2_PROMPT_SHA256 = "050e09d1300cd945a9a31b4c400792a35b2c57e662041556467b3e4077588878"

#: ``sha256`` of the new ``fix2_style`` prompt on ``CHUNK`` (2705 chars): pinned
#: so a later prose edit is a visible change, not a silent re-baseline.
FIX2_STYLE_PROMPT_SHA256 = "ff188443e421c403d13470b81b499edd0f7e06a0393ed27cc6f7ff27845919e5"

#: The largest prompt a variant may be, relative to ``base``.
MAX_PROMPT_CHAR_RATIO = 1.60

#: Two canned runs on the tiny workbook below, one record dump each, sha256 of
#: the canonical dump (``_canonical_record_sha``): the json contract and the
#: lines contract at ``base``. Both carry ``pipeline_version="6"`` and
#: ``schema_version="2"`` (0.6.1). The tiny workbook's only field is an
#: undecided BETWEEN marker, so 0.6.1-A/B/D leave its ``answers`` and label
#: alone. The json pin moves on ``pipeline_version`` alone: 0.6.1-C's two
#: coverage members are ``omit_if_default`` and a json run omits them, so
#: ``SCHEMA_VERSION`` stays ``"2"``. The lines pin also carries 0.6.1-C's one
#: lines-only member, ``model_declined`` (proved by the PRE_K1 test below).
JSON_RECORD_SHA256 = "c4a89a1e18c0016208ad10315fce011a75c337efbe9f0137103432de436c045d"
LINES_RECORD_SHA256 = "cc7b5e5fd06dbd6a0f160bf3fa1bbfaea060d4d444bd737913adfe1565292d39"

#: The pre-K1 (0.6.0-L1b) values of the two hashes above, when the dump's
#: ``pipeline_version`` is substituted back to ``"4"`` (and, for the lines dump,
#: 0.6.1-C's ``model_declined`` member is dropped; a json dump never carries
#: it). Kept as named historical pins: the build's proof that only those bytes
#: moved (A3).
JSON_RECORD_SHA256_PRE_K1 = "e33bafd6bc3a4b1fe0f8a791dfe62bc0e1f13a6cd0a77317e1695e97a194b1a8"
LINES_RECORD_SHA256_PRE_K1 = "06710633ecc5cb4b43620b900d80737bc59bcdf4ce41ded5807740a217f6d99c"

#: ``compute_cache_key`` for the fixture params, with and without
#: ``output_contract="lines"``, both at the default prompt variant. HEAD's values.
CACHE_KEY_DEFAULT = "0c4dbd908055d0adc34da8684e39e713fca6afe6986e054755cc87e9e2a282ec"
CACHE_KEY_LINES = "0a3523d1fee86f752591e6b99491b00456bb8daf30935911d8bba8d2917b6aea"

JSON_RESPONSE = json.dumps(
    {
        "fields": [
            {
                "label": "Alpha widget variant?",
                "control_type": "single_select",
                "options": [
                    {"text": "Ruby", "selected": False},
                    {"text": "Teal", "selected": False},
                ],
                "answer": [],
                "annotations": [],
                "region_id": "x",
                "source_elements": [],
            }
        ]
    }
)
LINES_RESPONSE = "single L=0.0 O=0.2,0.4\nend"

#: The words a variant could hand the model to copy: the words of every example
#: line (``  kind ...``) and of every quoted literal. A quoted literal is the one
#: place a variant can put a copyable string in front of the model, which is what
#: "label-like" means here.
EXAMPLE_LINE = re.compile(r"^  (?:single|multi|bool|text|hdr|note|skip)\b.*$", re.M)
QUOTED = re.compile(r'"([^"]*)"')
WORD = re.compile(r"[A-Za-z]+")
ROW_ID = re.compile(r"^([0-9]+)(?: \[[a-z]+\])?:", re.M)


# --------------------------------------------------------------------------
# Canned clients and one tiny workbook.
# --------------------------------------------------------------------------

class _Client:
    """A canned client that returns one fixed text for every prompt."""

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


def _tiny_workbook(path: Path) -> Path:
    """One field row, the same fixture the json byte-identity test uses."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Tiny"
    for cell, value in (
        ("A1", "Alpha widget variant?"), ("C1", "X"), ("D1", "Ruby"),
        ("F1", "X"), ("G1", "Teal"),
    ):
        ws[cell] = value
    wb.save(path)
    wb.close()
    return path


def _grid_workbook(path: Path) -> Path:
    """A row of eight short tokens (a GRID region) above one field row.

    The gold tabs project no row tag at all (a spreadsheet row holds labels as
    well as options, so its band is a plain field row), so the projection option
    is exercised on a workbook that does produce one.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Tiny"
    for column, value in enumerate(("AK", "AL", "AZ", "CA", "CO", "CT", "DE", "FL"), 1):
        ws.cell(row=1, column=column, value=value)
    ws["A2"] = "Alpha widget variant?"
    ws["C2"] = "X"
    ws["D2"] = "Ruby"
    ws["F2"] = "X"
    ws["G2"] = "Teal"
    wb.save(path)
    wb.close()
    return path


def _canonical_sha_of(dump: dict) -> str:
    text = json.dumps(dump, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_dump(record) -> dict:
    """A record dump with every per-run byte normalised away.

    Dropped: the run's own ids and clock, the source content hash (openpyxl
    stamps the wall clock, so the same workbook written twice differs), and the
    two per-draft ids a resolver call mints. The archived calls are kept as their
    prompt hashes, so the dump covers the prompt/projection the run sent.
    """
    dump = json.loads(to_json(record))
    for key in ("instance_id", "run_id", "created_at", "idempotency_key", "source"):
        dump.pop(key, None)
    dump["llm_calls"] = [call["prompt_hash"] for call in dump.get("llm_calls") or []]
    for field in dump.get("fields") or []:
        provenance = field.get("provenance")
        if isinstance(provenance, dict):
            provenance.pop("llm_call_ref", None)
            provenance.pop("binding_id", None)
    return dump


def _canonical_record_sha(record) -> str:
    """sha256 of ``_canonical_dump(record)``."""
    return _canonical_sha_of(_canonical_dump(record))


def _cache_key(**overrides) -> str:
    base = dict(
        content_hash="abc",
        pipeline_version="4",
        schema_version="2",
        prompt_version="4",
        model="canned",
        params={"temperature": 0},
    )
    base.update(overrides)
    return compute_cache_key(**base)


# --------------------------------------------------------------------------
# The gold sets: their words and their canned perfect lines.
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    """Dev and held-out gold, built once, plus the bridge that renders lines."""
    dev_dir = tmp_path_factory.mktemp("variants-dev")
    dev, _manifest, _ = make_gold.build_set("dev", make_gold.DEFAULT_SEEDS["dev"], dev_dir)
    held_dir = tmp_path_factory.mktemp("variants-heldout")
    held, _manifest, _ = make_gold.build_set(
        "heldout", make_gold.DEFAULT_SEEDS["heldout"], held_dir
    )
    return {
        "dev": dev, "dev_dir": dev_dir, "dev_lines": gold_lines.GoldLines(dev_dir, dev),
        "held": held, "held_dir": held_dir,
        "held_lines": gold_lines.GoldLines(held_dir, held),
    }


@pytest.fixture(scope="module")
def gold_words(gold) -> set[str]:
    """Every word of every gold cell, plus the generator's whole word lists."""
    words = set()
    for key in ("dev", "held"):
        for cell in gold[key]["cells"]:
            words.update(word.lower() for word in WORD.findall(cell["text"]))
    for kind in ("dev", "heldout"):
        for pool in make_gold.VOCAB[kind].values():
            for item in pool:
                words.update(word.lower() for word in WORD.findall(item))
    return words


@pytest.fixture(scope="module")
def canned_lines(gold) -> set[str]:
    """Every rendered canned perfect line of both sets, as whole strings."""
    lines = set()
    for key, bridge in (("dev", "dev_lines"), ("held", "held_lines")):
        geometry = gold[bridge]
        doc = make_gold.perfect_lines(gold[key])
        for tab in gold[key]["tabs"]:
            text = gold_lines.render(doc[tab], geometry.geometry[tab].refs)
            lines.update(line.strip() for line in text.splitlines() if line.strip())
    return lines


# --------------------------------------------------------------------------
# The prompt properties a variant must have.
# --------------------------------------------------------------------------

def _prompt(name: str) -> str:
    return build_lines_prompt(CHUNK, name)


def _example_lines(text: str) -> list[str]:
    return EXAMPLE_LINE.findall(text)


def _copyable_words(text: str) -> set[str]:
    """The words a model could lift out of the prompt into a field."""
    words = set()
    for snippet in _example_lines(text) + QUOTED.findall(text):
        words.update(word.lower() for word in WORD.findall(snippet))
    return words


def _bad_examples(name: str) -> list[tuple[str, str]]:
    """``[(line, why)]`` for every example line of a variant that is not clean."""
    bad: list[tuple[str, str]] = []
    for line in _example_lines(_prompt(name)):
        parsed = parse_lines(line + "\nend", finish_reason="stop")
        if parsed.errors:
            bad.append((line, parsed.errors[0]))
        elif len(parsed.records) != 1:
            bad.append((line, "not exactly one record"))
        elif parsed.stats.refs_bad:
            bad.append((line, "sentinel ref"))
        elif parsed.records[0].review:
            bad.append((line, "review flag"))
    return bad


def _char_ratio(name: str) -> float:
    return len(_prompt(name)) / len(_prompt("base"))


def _assert_within_bound(name: str) -> None:
    ratio = _char_ratio(name)
    assert ratio <= MAX_PROMPT_CHAR_RATIO, (
        f"{name}: {len(_prompt(name))} chars vs base {len(_prompt('base'))} "
        f"(ratio {ratio:.4f} > {MAX_PROMPT_CHAR_RATIO})"
    )


def _shared_gold_words(name: str, gold_words: set[str]) -> set[str]:
    return _copyable_words(_prompt(name)) & gold_words


def test_the_base_variant_is_byte_identical_to_build_lines_prompt():
    """``base`` is today's prompt, byte for byte: the default path cannot move."""
    text = build_lines_prompt(CHUNK)
    assert build_lines_prompt(CHUNK, "base") == text
    assert build_lines_prompt(CHUNK, variant="base") == text
    assert len(text) == BASE_PROMPT_CHARS
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == BASE_PROMPT_SHA256
    # it is the registry entry, not a lookalike copy of it
    assert LINES_PROMPT_VARIANTS["base"](CHUNK) == text


def test_every_variant_is_deterministic_and_within_the_size_bound():
    """Every variant is a pure function of the chunk, and short: base +60%."""
    base = _prompt("base")
    for name in LINES_PROMPT_VARIANTS:
        first, second = _prompt(name), _prompt(name)
        assert first == second, name
        assert first == LINES_PROMPT_VARIANTS[name](CHUNK), name
        # the chunk tail is the same string in every variant
        assert first.endswith(f"Rows of `{CHUNK.key}`:\n{CHUNK.text}"), name
        chars, ratio = len(first), _char_ratio(name)
        print(f"{name}: {chars} chars vs base {len(base)} (ratio {ratio:.4f})")
        _assert_within_bound(name)
    # base is unchanged, fix1 retargets the wording, fix2 adds worked examples,
    # fix2_notags is fix2's own text over a different projection, and fix2_style
    # is fix2's text plus the one style sentence (its own projection too).
    assert _prompt("fix1") != base
    assert _prompt("fix2") != _prompt("fix1")
    assert _prompt("fix2_notags") == _prompt("fix2")
    assert _prompt("fix2_style") != _prompt("fix2")
    assert len(_prompt("fix2_style")) > len(_prompt("fix2"))


def test_the_registry_is_the_closed_vocabulary_the_config_accepts():
    """One vocabulary: the registry, the config's tuple and the tag options."""
    assert tuple(LINES_PROMPT_VARIANTS) == (
        "base", "fix1", "fix2", "fix2_notags", "fix2_style",
    )
    assert PROMPT_VARIANTS == tuple(LINES_PROMPT_VARIANTS)
    # no variant can be added to one mapping without the others (the options)
    assert set(LINES_VARIANT_ROW_TAGS) == set(LINES_PROMPT_VARIANTS)
    assert set(LINES_VARIANT_STYLE_TAGS) == set(LINES_PROMPT_VARIANTS)
    assert LINES_VARIANT_ROW_TAGS == {
        "base": True, "fix1": True, "fix2": True,
        "fix2_notags": False, "fix2_style": True,
    }
    assert LINES_VARIANT_STYLE_TAGS == {
        "base": False, "fix1": False, "fix2": False,
        "fix2_notags": False, "fix2_style": True,
    }
    for name in LINES_PROMPT_VARIANTS:
        assert callable(LINES_PROMPT_VARIANTS[name]), name
        assert isinstance(LINES_PROMPT_VARIANTS[name](CHUNK), str), name
        assert lines_variant_row_tags(name) is LINES_VARIANT_ROW_TAGS[name], name
        assert lines_variant_style_tags(name) is LINES_VARIANT_STYLE_TAGS[name], name
        validate_prompt_variant(name)


def test_the_projection_row_tags_are_a_projection_option_not_a_prompt_edit(tmp_path):
    """``row_tags=False`` drops the row tag and nothing else.

    The row ids a response cites come from the lattice, never from the tag, so a
    response written against the tagged projection resolves against the tag-free
    one unchanged -- which is what makes ``fix2_notags`` a projection option
    rather than a second grammar.
    """
    path = _grid_workbook(tmp_path / "grid.xlsx")
    ing = ingest(path)
    layout = analyze(ing.elements)
    tagged = project_lines_chunks(layout, ing.elements, ["Tiny"])[0].text
    plain = project_lines_chunks(layout, ing.elements, ["Tiny"], row_tags=False)[0].text
    assert " [grid]" in tagged
    assert " [" not in plain
    assert plain == re.sub(r" \[(?:hdr|grid|prose)\]", "", tagged)
    assert ROW_ID.findall(tagged) == ROW_ID.findall(plain)
    assert ROW_ID.findall(tagged)

    # end to end: the variant drives the option, and the pipeline actually sends
    # the prompt the variant renders over that projection.
    for variant, has_tag in (("fix2", True), ("fix2_notags", False)):
        client = _Client(LINES_RESPONSE)
        config = PipelineConfig(
            model="canned", output_contract="lines", prompt_variant=variant
        )
        Pipeline(Store(tmp_path / f"store-{variant}"), client, config).run(path)
        sent = client.prompts[0]
        expected = build_lines_prompt(
            project_lines_chunks(layout, ing.elements, ["Tiny"], row_tags=has_tag)[0],
            variant,
        )
        assert sent == expected, variant
        assert (" [grid]" in sent) is has_tag, variant


def test_every_variant_example_line_parses_cleanly():
    """Every example parses with no line error, no sentinel ref and no flag."""
    for name in LINES_PROMPT_VARIANTS:
        assert _bad_examples(name) == [], name
    # non-vacuous: the extraction sees the examples, and the parser objects to a
    # line the prompt should never carry.
    assert len(_example_lines(_prompt("base"))) == 6
    assert len(_example_lines(_prompt("fix2"))) == 12
    assert parse_lines("single L=99.9 O=99.\nend", finish_reason="stop").stats.refs_bad


def test_no_variant_example_shares_a_gold_word(gold_words):
    """No variant hands the model a word from a dev or held-out gold list."""
    assert len(gold_words) >= 75, sorted(gold_words)[:5]
    for pool_word in ("grommet", "zephyr", "affirm", "conform"):
        assert pool_word in gold_words, pool_word
    for name in LINES_PROMPT_VARIANTS:
        shared = _shared_gold_words(name, gold_words)
        assert shared == set(), (name, sorted(shared))
    assert _copyable_words(_prompt("fix2"))


def test_no_variant_example_is_a_gold_canned_line(canned_lines):
    """No example is a line of the gold's own canned perfect responses."""
    assert len(canned_lines) > 100
    for name in LINES_PROMPT_VARIANTS:
        collisions = set(_example_lines(_prompt(name))) & canned_lines
        assert collisions == set(), (name, sorted(collisions))
    # non-vacuous: the check does catch a copied gold line
    sample = next(line for line in sorted(canned_lines) if line.startswith("single L="))
    assert set([sample]) & canned_lines == {sample}


def test_no_variant_text_can_be_copied_into_a_field():
    """The fix variants carry no quoted literal at all (the F2 contamination)."""
    for name in ("fix1", "fix2", "fix2_notags", "fix2_style"):
        assert QUOTED.findall(_prompt(name)) == [], name
        assert '"' not in _prompt(name), name
    assert QUOTED.findall(_prompt("base"))
    # and no variant asks for JSON or a markdown fence: the only mention of
    # either is the prohibition, so no example can conflict with the grammar
    for name in LINES_PROMPT_VARIANTS:
        text = _prompt(name)
        assert "```" not in text, name
        assert "no JSON" in text and "no markdown fences" in text, name


def test_fix1_drops_the_contaminating_literal():
    """``base`` spells out ``"Yes No"`` and fix1 does not: F2's cause is gone."""
    assert '"Yes No"' in _prompt("base")
    for name in ("fix1", "fix2", "fix2_notags", "fix2_style"):
        assert '"Yes No"' not in _prompt(name), name
        # the rule survives, without a concrete string to copy
        assert "A quoted literal" in _prompt(name), name


# --------------------------------------------------------------------------
# Offline acceptance: the canned perfect lines, under every variant.
# --------------------------------------------------------------------------

def _assert_strict_one(res: dict) -> None:
    overall = res["overall"]
    assert overall["precision"] == 1.0
    assert overall["recall"] == 1.0
    assert overall["matched"] == overall["gold_fields"] == overall["predicted_fields"]
    assert overall["merge_count"] == 0
    assert overall["split_count"] == 0
    assert overall["missed"] == 0
    assert overall["spurious"] == 0
    assert overall["stray_ref_count"] == 0
    assert overall["unresolved_ref_count"] == 0
    assert overall["label_ok"] == overall["label_total"] > 0
    assert overall["options_ok"] == overall["options_total"] > 0
    assert overall["addressed_num"] == overall["addressed_den"] > 0


def _run_variant(bridge, texts: dict[str, str], store_dir: Path, variant: str) -> list[dict]:
    """Run every gold tab through the real Pipeline under one prompt variant."""
    config = PipelineConfig(
        model="canned",
        output_contract="lines",
        checkbox_conventions=list(bridge.gold.get("checkbox_conventions") or []),
        prompt_variant=variant,
    )
    client = gold_lines.LinesClient(texts)
    store = Store(Path(store_dir))
    fields: list[dict] = []
    for tab in bridge.gold["tabs"]:
        record = Pipeline(store, client, config).run(bridge.gold_dir / f"{tab}.xlsx")
        fields.extend(json.loads(to_json(record))["fields"])
    return fields


def test_perfect_canned_lines_reach_strict_one_under_every_variant(gold, tmp_path_factory):
    """The variant plumbing changes no parse and no resolution.

    The canned client ignores the prompt, so if a variant (or the tag-free or
    style-tagged projection) changed a row id, the gold's own perfect lines
    would stop resolving. They do not: strict 1.0 on all 244 dev fields under
    all five.
    """
    dev, bridge = gold["dev"], gold["dev_lines"]
    texts = bridge.perfect()
    for name in LINES_PROMPT_VARIANTS:
        fields = _run_variant(bridge, texts, tmp_path_factory.mktemp(f"perfect-{name}"), name)
        res = gold_lines.score(dev, fields)
        assert res["overall"]["gold_fields"] == 244, name
        _assert_strict_one(res)


# --------------------------------------------------------------------------
# Byte identity: the json path and the lines `base` path against the 0.6.1 pins.
# --------------------------------------------------------------------------

def test_the_json_contract_and_the_lines_base_path_are_byte_stable(tmp_path):
    """The two shipped paths are pinned to their 0.6.1 bytes.

    Two canned runs on the same tiny workbook: the json contract (default) and
    the lines contract at ``base``. Both dumps hash to the pins above, and a json
    run that merely NAMES a variant is byte-identical to one that does not --
    the json contract has its own prompt and ignores the field.
    """
    path = _tiny_workbook(tmp_path / "tiny.xlsx")
    runs = {
        "json": (JSON_RESPONSE, "json", "base"),
        "lines": (LINES_RESPONSE, "lines", "base"),
        "json-fix1": (JSON_RESPONSE, "json", "fix1"),
    }
    hashes = {}
    for name, (text, contract, variant) in runs.items():
        config = PipelineConfig(
            model="canned", output_contract=contract, prompt_variant=variant
        )
        record = Pipeline(Store(tmp_path / f"store-{name}"), _Client(text), config).run(path)
        assert record.status is InstanceStatus.COMPLETE, (name, record.errors)
        assert len(record.fields) == 1, name
        hashes[name] = _canonical_record_sha(record)
    assert hashes["json"] == JSON_RECORD_SHA256
    assert hashes["lines"] == LINES_RECORD_SHA256
    assert hashes["json-fix1"] == JSON_RECORD_SHA256


def test_the_kind_rule_off_dump_matches_head_except_the_0_6_1_moves(tmp_path):
    """A3: with ``kind_rule=False`` the dump equals HEAD's but for 0.6.1's moves.

    The rule-off record carries ``pipeline_version="6"`` (both contracts) and,
    on the lines contract, 0.6.1-C's ``model_declined`` member (a json dump never
    carries it: the two coverage members are ``omit_if_default`` and a json run
    omits them, so ``SCHEMA_VERSION`` stays ``"2"``). Substituting
    ``pipeline_version`` back to ``"4"`` and dropping that one lines-only member
    must reproduce the pre-K1 pins, so those are the only bytes this release's
    build moved on this unaffected record (A/B/D touch neither its label nor its
    answers).
    """
    from formextract.schema import SCHEMA_VERSION

    path = _tiny_workbook(tmp_path / "tiny.xlsx")
    runs = {
        "json": (JSON_RESPONSE, "json", JSON_RECORD_SHA256_PRE_K1),
        "lines": (LINES_RESPONSE, "lines", LINES_RECORD_SHA256_PRE_K1),
    }
    for name, (text, contract, old) in runs.items():
        config = PipelineConfig(
            model="canned", output_contract=contract, kind_rule=False
        )
        record = Pipeline(Store(tmp_path / f"off-{name}"), _Client(text), config).run(
            path
        )
        assert record.pipeline_version == PIPELINE_VERSION == "6"
        assert record.schema_version == SCHEMA_VERSION == "2"
        dump = _canonical_dump(record)
        dump["pipeline_version"] = "4"
        if contract == "lines":
            for block in dump.get("coverage") or []:
                block.pop("model_declined", None)
        assert _canonical_sha_of(dump) == old, name


def test_an_unaffected_record_is_byte_identical_rule_on_or_off(tmp_path):
    """A record the rule cannot touch is the same dump with the knob either way."""
    path = _tiny_workbook(tmp_path / "tiny.xlsx")
    hashes = {}
    for flag in (False, True):
        config = PipelineConfig(
            model="canned", output_contract="json", kind_rule=flag
        )
        record = Pipeline(
            Store(tmp_path / f"knob-{flag}"), _Client(JSON_RESPONSE), config
        ).run(path)
        hashes[flag] = _canonical_record_sha(record)
    assert hashes[False] == hashes[True] == JSON_RECORD_SHA256


# --------------------------------------------------------------------------
# The style cue (L1b): the ``fix2_style`` variant and its projection option.
# --------------------------------------------------------------------------

#: The one line ``fix2_style`` inserts into ``fix2``: the ``[shaded]`` definition.
STYLE_SENTENCE = (
    "Cells printed with a trailing `[shaded]` tag carry a fill colour: options "
    "and checkbox cells are often shaded, typed answers often are not; it is "
    "evidence, not a rule."
)


def _solid(rgb: str) -> PatternFill:
    return PatternFill(fill_type="solid", start_color=rgb, end_color=rgb)


def _strip_shaded(text: str) -> str:
    """The style-tagged projection with every ``[shaded]`` suffix removed."""
    return text.replace(" [shaded]", "")


def _style_workbook(path: Path) -> Path:
    """One field row with every fill boundary the style projection cares about.

    ``B1`` is a plain (unfilled) option, ``C1`` a solid-filled option, ``D1`` a
    theme fill that ``_argb_to_int`` maps to ``None``, ``E1`` a filled cell the
    caller declares a non-answer, and ``G1:H1`` a merged filled cell.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Tiny"
    ws["A1"] = "Alpha widget variant?"
    ws["B1"] = "Ruby"
    ws["C1"] = "Teal"
    ws["C1"].fill = _solid("FFEEDD")
    ws["D1"] = "Olive"
    ws["D1"].fill = PatternFill(fill_type="solid", fgColor=Color(theme=6))
    ws["E1"] = "note here"
    ws["E1"].fill = _solid("FF00FF00")
    ws.merge_cells("G1:H1")
    ws["G1"] = "Merged"
    ws["G1"].fill = _solid("FFABCDEF")
    wb.save(path)
    wb.close()
    return path


def test_the_shipped_variant_prompts_do_not_move():
    """The four shipped prompts are HEAD's bytes; ``fix2_style`` is a pinned new one."""
    for name, digest in (
        ("base", BASE_PROMPT_SHA256),
        ("fix1", FIX1_PROMPT_SHA256),
        ("fix2", FIX2_PROMPT_SHA256),
        ("fix2_notags", FIX2_PROMPT_SHA256),
        ("fix2_style", FIX2_STYLE_PROMPT_SHA256),
    ):
        text = _prompt(name)
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == digest, name


def test_fix2_style_is_fix2_plus_the_style_sentence():
    """``fix2_style`` inserts exactly one line into ``fix2``: no other prose moves."""
    fix2 = _prompt("fix2").splitlines()
    style = _prompt("fix2_style").splitlines()
    assert [line for line in style if line not in fix2] == [STYLE_SENTENCE]
    assert [line for line in fix2 if line not in style] == []
    at = style.index(STYLE_SENTENCE)
    assert style[:at] + style[at + 1:] == fix2
    # the sentence is prose: no copyable literal, and the only mention of the tag
    assert '"' not in STYLE_SENTENCE
    assert "[shaded]" in STYLE_SENTENCE
    assert _prompt("fix2_style").count("[shaded]") == 1
    assert _prompt("fix2").count("[shaded]") == 0
    assert _prompt("fix2_style").isascii()


def test_the_style_option_is_off_on_every_shipped_path(gold):
    """``style_tags=False`` prints today's lines, byte for byte, on both gold sets.

    The style cue ships only with ``fix2_style``; every other path must project
    the lines it did before the field existed, and no shipped line carries the
    ``[shaded]`` suffix.
    """
    tabs = 0
    for key in ("dev", "held"):
        for tab in gold[key]["tabs"]:
            ing = ingest(gold[f"{key}_dir"] / f"{tab}.xlsx")
            layout = analyze(ing.elements)
            default = project_lines_chunks(layout, ing.elements, [tab])[0].text
            off = project_lines_chunks(
                layout, ing.elements, [tab], style_tags=False
            )[0].text
            assert off == default, (key, tab)
            assert "[shaded]" not in off, (key, tab)
            tabs += 1
    assert tabs == len(gold["dev"]["tabs"]) + len(gold["held"]["tabs"]) > 10


def test_the_style_option_only_adds_the_suffix_on_every_shape(tmp_path):
    """On the fixtures too: the option adds `` [shaded]`` and moves nothing else."""
    for name, build in (
        ("tiny", _tiny_workbook),
        ("grid", _grid_workbook),
        ("style", _style_workbook),
    ):
        path = build(tmp_path / f"{name}.xlsx")
        ing = ingest(path)
        layout = analyze(ing.elements)
        off = project_lines_chunks(layout, ing.elements, ["Tiny"])[0].text
        on = project_lines_chunks(layout, ing.elements, ["Tiny"], style_tags=True)[0].text
        assert "[shaded]" not in off, name
        assert _strip_shaded(on) == off, name
        assert ROW_ID.findall(on) == ROW_ID.findall(off), name


def test_the_style_tag_marks_filled_cells_only(tmp_path):
    """The ingest boundary and the suffix: 0/None are unshaded, a real fill is not."""
    path = _style_workbook(tmp_path / "style.xlsx")
    ing = ingest(path)
    fills = {e.element_id: e.fill for e in ing.elements}
    assert fills["Tiny!1:2"] == 0            # unfilled cell -> 0
    assert fills["Tiny!1:3"] == 0xFFEEDD     # solid fill -> its integer
    assert fills["Tiny!1:4"] is None         # theme colour -> None
    assert fills["Tiny!1:5"] == 0x00FF00
    assert fills["Tiny!1:7"] == 0xABCDEF     # merged cell keeps its fill

    layout = analyze(ing.elements)
    off = project_lines_chunks(
        layout, ing.elements, ["Tiny"], non_answer_element_ids={"Tiny!1:5"}
    )[0].text
    on = project_lines_chunks(
        layout, ing.elements, ["Tiny"], style_tags=True,
        non_answer_element_ids={"Tiny!1:5"},
    )[0].text
    assert "[shaded]" not in off
    assert "Ruby [shaded]" not in on
    assert "Teal [shaded]" in on
    assert "Olive [shaded]" not in on
    assert "note here [annotation] [shaded]" in on
    assert "Merged [shaded]" in on
    # only the suffix moved: same row ids, same segment indices, same text
    assert _strip_shaded(on) == off
    assert ROW_ID.findall(on) == ROW_ID.findall(off)


def test_the_style_variant_drives_the_projection_and_reaches_the_model(tmp_path):
    """The variant, not the caller, turns ``[shaded]`` on for the prompt it sends."""
    path = _style_workbook(tmp_path / "style.xlsx")
    ing = ingest(path)
    layout = analyze(ing.elements)
    for variant, has_tag in (("fix2", False), ("fix2_style", True)):
        client = _Client(LINES_RESPONSE)
        config = PipelineConfig(
            model="canned", output_contract="lines", prompt_variant=variant
        )
        Pipeline(Store(tmp_path / f"store-{variant}"), client, config).run(path)
        sent = client.prompts[0]
        expected = build_lines_prompt(
            project_lines_chunks(layout, ing.elements, ["Tiny"], style_tags=has_tag)[0],
            variant,
        )
        assert sent == expected, variant
        assert (" [shaded]" in sent) is has_tag, variant


def test_the_cache_key_separates_fix2_and_fix2_style():
    """The two variants share a projection tag but not an instance key."""
    fix2 = _cache_key(output_contract="lines", prompt_variant="fix2")
    style = _cache_key(output_contract="lines", prompt_variant="fix2_style")
    assert fix2 != style
    assert style != CACHE_KEY_LINES
    assert style != _cache_key(output_contract="lines", prompt_variant="fix2_notags")


def test_live_probe_accepts_the_style_variant():
    """``live_probe.py --prompt-variant fix2_style`` parses (the name travels)."""
    import live_probe

    assert "fix2_style" in live_probe.PROMPT_VARIANTS
    assert "fix2_style" in live_probe.ALLOWED_STRING_VALUES
    args = live_probe.build_parser().parse_args(
        ["--model", "m", "--prompt-variant", "fix2_style", "x.xlsx"]
    )
    assert args.prompt_variant == "fix2_style"


# --------------------------------------------------------------------------
# Config, versions, cache key.
# --------------------------------------------------------------------------

def test_prompt_variant_is_validated(tmp_path):
    for name in PROMPT_VARIANTS:
        validate_prompt_variant(name)
    with pytest.raises(ValueError):
        validate_prompt_variant("fix3")
    with pytest.raises(ValueError):
        validate_prompt_variant(None)
    with pytest.raises(ValueError) as excinfo:
        Pipeline(
            Store(tmp_path / "store"),
            None,
            PipelineConfig(prompt_variant="fix3"),
        )
    assert "prompt_variant" in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        build_lines_prompt(CHUNK, "fix3")
    assert "prompt_variant" in str(excinfo.value)
    # a variant and a contract are independent knobs: the json contract accepts
    # a variant name (and ignores it), and the lines contract takes the default.
    Pipeline(Store(tmp_path / "ok"), None, PipelineConfig(prompt_variant="fix1"))


def test_the_cache_key_moves_only_for_a_non_base_variant():
    assert _cache_key() == CACHE_KEY_DEFAULT
    assert _cache_key(prompt_variant="base") == CACHE_KEY_DEFAULT
    assert _cache_key(output_contract="lines") == CACHE_KEY_LINES
    keyed = {}
    for name in PROMPT_VARIANTS:
        keyed[name] = _cache_key(output_contract="lines", prompt_variant=name)
        assert _cache_key(output_contract="lines", prompt_variant=name) == keyed[name], name
    assert keyed["base"] == CACHE_KEY_LINES
    assert len(set(keyed.values())) == len(PROMPT_VARIANTS)
    # the token is appended whatever the contract is, so a toggled variant can
    # never alias an instance authored under another one
    assert _cache_key(prompt_variant="fix1") != CACHE_KEY_DEFAULT


def test_the_prompt_version_is_not_bumped_by_a_variant():
    """Adding variants moves no version constant: the default prompt is unchanged."""
    from formextract.model import PIPELINE_VERSION
    from formextract.schema import SCHEMA_VERSION

    assert PROMPT_VERSION == "4"
    # 0.6.0-K1 bumped PIPELINE_VERSION to "5" (the kind rule changed a default
    # run's fields); 0.6.1 bumps it to "6" (A/B/D change a default run's output).
    # SCHEMA_VERSION stays "2": part C's TabCoverage members are omit_if_default
    # and a json record omits them. The prompt itself is unchanged, so
    # PROMPT_VERSION stays.
    assert PIPELINE_VERSION == "6"
    assert SCHEMA_VERSION == "2"
    # the substance: the default variant's prompt, and therefore the default
    # instance key, is HEAD's byte for byte
    assert hashlib.sha256(build_lines_prompt(CHUNK).encode()).hexdigest() == BASE_PROMPT_SHA256
    assert _cache_key() == CACHE_KEY_DEFAULT
    assert _cache_key(output_contract="lines") == CACHE_KEY_LINES


# --------------------------------------------------------------------------
# Mutation cases: each seam patch, each observation must move. The triple is the
# same every time -- the patched symbol exists, the patch was reached (a counter
# on the replacement), and the observation differs (usually: the real assertion
# above would now fail).
# --------------------------------------------------------------------------

def _tracked(target):
    """Wrap ``target`` and expose the call count, for the reached leg."""
    calls: list[tuple] = []

    def wrapper(*args, **kwargs):
        calls.append((args, kwargs))
        return target(*args, **kwargs)

    wrapper.calls = calls  # type: ignore[attr-defined]
    return wrapper


def test_mutation_the_registry_returning_the_wrong_variant(monkeypatch):
    """A registry that hands back another variant's prompt is caught."""
    assert "fix1" in LINES_PROMPT_VARIANTS
    replacement = _tracked(lambda chunk: LINES_PROMPT_VARIANTS["base"](chunk))
    monkeypatch.setitem(LINES_PROMPT_VARIANTS, "fix1", replacement)
    wrong = build_lines_prompt(CHUNK, "fix1")
    assert replacement.calls, "the registry patch was never reached"
    assert wrong == build_lines_prompt(CHUNK)
    with pytest.raises(AssertionError):
        assert _prompt("fix1") != _prompt("base")


def test_mutation_the_variant_not_in_the_cache_key(monkeypatch):
    """Dropping the token from the key makes two variants share one instance."""
    import formextract.pipeline as pipeline

    assert callable(pipeline._prompt_variant_token)
    replacement = _tracked(lambda variant: "")
    monkeypatch.setattr(pipeline, "_prompt_variant_token", replacement)
    fix1 = pipeline.compute_cache_key(
        content_hash="abc", pipeline_version="4", schema_version="2",
        prompt_version="4", model="canned", params={"temperature": 0},
        output_contract="lines", prompt_variant="fix1",
    )
    assert replacement.calls, "the cache-key patch was never reached"
    assert fix1 == _cache_key(output_contract="lines")
    with pytest.raises(AssertionError):
        assert fix1 != _cache_key(output_contract="lines")


def test_mutation_fix1_re_adding_the_contaminating_literal(monkeypatch):
    """Re-adding the literal example string re-breaks the no-quote check."""
    assert '"' not in _prompt("fix1")
    original = LINES_PROMPT_VARIANTS["fix1"]
    replacement = _tracked(lambda chunk: original(chunk) + ' "Yes No"\n')
    monkeypatch.setitem(LINES_PROMPT_VARIANTS, "fix1", replacement)
    quoted = QUOTED.findall(_prompt("fix1"))
    assert replacement.calls, "the registry patch was never reached"
    assert quoted == ["Yes No"]
    with pytest.raises(AssertionError):
        assert QUOTED.findall(_prompt("fix1")) == []


def test_mutation_an_example_line_that_does_not_parse(monkeypatch):
    """An example with a sentinel ref is caught by the parse check."""
    assert _bad_examples("fix2") == []
    original = LINES_PROMPT_VARIANTS["fix2"]
    replacement = _tracked(lambda chunk: original(chunk) + "\n  text L=1.0 A=1.\n")
    monkeypatch.setitem(LINES_PROMPT_VARIANTS, "fix2", replacement)
    bad = _bad_examples("fix2")
    assert replacement.calls, "the registry patch was never reached"
    assert bad and bad[-1][0] == "  text L=1.0 A=1."
    assert bad[-1][1] == "sentinel ref"


def test_mutation_the_plus_60_percent_bound_is_load_bearing(monkeypatch):
    """The size bound is read, and the measured variants really are over 1.0."""
    assert MAX_PROMPT_CHAR_RATIO == 1.60
    for name in LINES_PROMPT_VARIANTS:
        assert _char_ratio(name) > 1.0 or name == "base", name
    _assert_within_bound("fix2")
    monkeypatch.setattr(sys.modules[__name__], "MAX_PROMPT_CHAR_RATIO", 1.0)
    assert MAX_PROMPT_CHAR_RATIO == 1.0, "the bound patch was never applied"
    assert _char_ratio("fix2") > 1.0
    with pytest.raises(AssertionError):
        _assert_within_bound("fix2")


def test_mutation_the_vocabulary_check_is_load_bearing(monkeypatch, gold_words):
    """A variant that plants a gold word in a copyable string is caught."""
    assert _shared_gold_words("fix2", gold_words) == set()
    planted = "grommet"
    assert planted in gold_words
    original = LINES_PROMPT_VARIANTS["fix2"]
    replacement = _tracked(
        lambda chunk: original(chunk) + f'  note L=1.0 "{planted}"\n'
    )
    monkeypatch.setitem(LINES_PROMPT_VARIANTS, "fix2", replacement)
    shared = _shared_gold_words("fix2", gold_words)
    assert replacement.calls, "the registry patch was never reached"
    assert shared == {planted}
    with pytest.raises(AssertionError):
        assert shared == set()
