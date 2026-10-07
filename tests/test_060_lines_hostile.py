"""0.6.0 L1a-fix: the line parser under attack (totality, bounds, cost).

Everything here is hand-typed and deterministic: a fixed corrupted-input
corpus with exact expected outcomes, seeded round trips and fuzz, an injected
step counter (never a clock), a static AST scan, and mutation cases that each
carry the anti-vacuity triple (the patched symbol exists, the patch was
reached, the observation differs).
"""
from __future__ import annotations

import ast
import pathlib
import random
import re
import string
import sys

import pytest

from formextract import lines
from formextract.lines import (
    EMPTY_ERROR,
    MAX_DIGITS,
    MAX_SPAN_WIDTH,
    JoinItem,
    LineKind,
    LineRecord,
    LineStats,
    LiteralItem,
    RefItem,
    SENTINEL_REF,
    SegmentRef,
    SpanItem,
    parse_lines,
)
from formextract.resolve import LLMResponse

MODULE_PATH = pathlib.Path(lines.__file__)

STATS_KEYS = {
    "lines_total",
    "records",
    "noise_lines",
    "error_lines",
    "literal_items",
    "refs_total",
    "refs_bad",
    "end_seen",
    "end_missing",
    "truncated",
    "dropped_tail",
    "unknown_kind",
    "no_label",
    "empty_response",
}

KINDS = (
    LineKind.SINGLE,
    LineKind.MULTI,
    LineKind.BOOL,
    LineKind.TEXT,
    LineKind.HDR,
    LineKind.NOTE,
    LineKind.SKIP,
)


def _refs(record: LineRecord) -> list[SegmentRef]:
    return list(record.refs())


def _items(record: LineRecord):
    yield from record.label
    yield from record.options
    yield from record.answer
    yield from record.notes


# --- a. the malformed corpus, with exact outcomes ----------------------------


def _wrap(case: str) -> str:
    """``case`` on line 2, between a good record and a terminator."""
    return "single L=1.0 O=1.1\n" + case + "\nend\n"


_TEN_DIGITS = "1234567890"

MALFORMED_CASES = [
    ("12.", {"error": "line 2: expected a kind word"}),
    ("12", {"error": "line 2: expected a kind word"}),
    (".0", {"error": "line 2: expected a kind word"}),
    ("12.0-", {"error": "line 2: expected a kind word"}),
    ("12.5-2", {"error": "line 2: expected a kind word"}),
    ("12.2-13.1", {"error": "line 2: expected a kind word"}),
    ("12.0+", {"error": "line 2: expected a kind word"}),
    ("+12.0", {"error": "line 2: expected a kind word"}),
    ("single", {"error": "line 2: missing L="}),
    ("single L=", {"error": "line 2: missing L="}),
    ("single O=12.2", {"error": "line 2: missing L="}),
    ("L=12.0", {"error": "line 2: unexpected key '='"}),
    (
        "single L=12.0 O=",
        {
            "record": {
                "kind": LineKind.SINGLE,
                "label": (RefItem(SegmentRef(12, 0)),),
                "options": (),
                "review": False,
            }
        },
    ),
    (
        "xyz L=1.0",
        {"record": {"kind": LineKind.TEXT, "review": True, "unknown_kind": "xyz"}},
    ),
    (
        "single L=999999999999.0",
        {"record": {"label": (RefItem(SENTINEL_REF),), "review": True}},
    ),
    (
        "single L=" + "9" * 5000 + ".0",
        {"record": {"label": (RefItem(SENTINEL_REF),), "review": True}},
    ),
    (
        "single L=\u0661\u0662.\u0660",
        {"record": {"label": (RefItem(SENTINEL_REF),), "review": True}},
    ),
    (
        "single L=\uff11\uff12.\uff10",
        {"record": {"label": (RefItem(SENTINEL_REF),), "review": True}},
    ),
    (
        "single L=1\u00b2.0",
        {"record": {"label": (RefItem(SENTINEL_REF),), "review": True}},
    ),
    (
        'single L="abc"',
        {"record": {"label": (RefItem(SENTINEL_REF),), "review": True}},
    ),
    (
        'single L=1.0 A="abc"',
        {"record": {"answer": (RefItem(SENTINEL_REF),), "review": True}},
    ),
    (
        'single L=1.0 N="unterminated',
        {"record": {"notes": (LiteralItem("unterminated"),), "review": False}},
    ),
    (
        'single L=1.0 N="a=b,c"',
        {"record": {"notes": (LiteralItem("a=b,c"),), "review": False}},
    ),
    (
        "single L=1.0 O=1.1\x00",
        {"record": {"options": (RefItem(SENTINEL_REF),), "review": True}},
    ),
    (
        "single L=1.0 \ud800",
        {"record": {"label": (RefItem(SENTINEL_REF),), "review": True}},
    ),
    (
        "multi L=1.0 O=1.0-",
        {"record": {"options": (RefItem(SENTINEL_REF),), "review": True}},
    ),
    (
        "single L=1",
        {"record": {"label": (RefItem(SENTINEL_REF),), "review": True}},
    ),
    (
        "single L=" + _TEN_DIGITS + ".0",
        {"record": {"label": (RefItem(SENTINEL_REF),), "review": True}},
    ),
]


@pytest.mark.parametrize("case,spec", MALFORMED_CASES, ids=[c for c, _ in MALFORMED_CASES])
def test_malformed_corpus_exact_outcomes(case, spec):
    parsed = parse_lines(_wrap(case), finish_reason="stop")

    # The good line before the case is always parsed.
    assert _refs(parsed.records[0]) == [SegmentRef(1, 0), SegmentRef(1, 1)]
    assert parsed.truncated is False
    assert parsed.stats.end_seen is True

    if "record" not in spec:
        assert len(parsed.records) == 1
        assert parsed.errors == [spec["error"]]
        assert parsed.stats.error_lines == 1
        return

    assert parsed.errors == []
    assert len(parsed.records) == 2
    record = parsed.records[1]
    assert record.line_no == 2
    for key, value in spec["record"].items():
        assert getattr(record, key) == value, (case, key)


def test_end_mid_stream_then_more_lines_is_noise():
    parsed = parse_lines(
        "single L=1.0 O=1.1\nend\nsingle L=2.0 O=2.1\nEND.\n",
        finish_reason=None,
    )
    assert [r.line_no for r in parsed.records] == [1]
    assert parsed.stats.end_seen is True
    assert parsed.stats.noise_lines == 3  # the two post-end lines and the blank tail
    assert parsed.errors == []


def test_end_inside_a_fence_is_still_the_terminator():
    # Fence markers are per-line noise; their contents are not suppressed.
    parsed = parse_lines("```\nend\n```\n", finish_reason=None)
    assert parsed.records == []
    assert parsed.stats.end_seen is True
    assert parsed.stats.noise_lines == 3  # both fences, the blank tail
    assert parsed.errors == []


# --- the digit cap and ASCII-only numbers ------------------------------------


def test_digit_cap_and_non_ascii_digits_are_sentinels():
    assert MAX_DIGITS == 9

    ok = parse_lines(
        "single L=123456789.0 O=123456789.1-2", finish_reason="stop"
    )
    assert ok.records[0].label == (RefItem(SegmentRef(123456789, 0)),)
    assert ok.records[0].options == (SpanItem(123456789, 1, 2),)
    assert ok.stats.refs_bad == 0

    long_digits = "9" * 5000
    for bad in (
        "single L=1234567890.0",  # 10 digits: one over the cap
        "single L=1." + "0" * 10,
        "single L=" + long_digits + ".0",
        "single L=1." + long_digits,
        "single L=" + "0" * 64 + ".0",
        "single L=\u0661\u0662.\u0660",  # Arabic-Indic
        "single L=\u06f1\u06f2.\u06f0",  # extended Arabic-Indic
        "single L=\uff11\uff12.\uff10",  # fullwidth
        "single L=1\u00b2.0",  # superscript
        "multi L=1.0 O=1.0-" + long_digits,  # seg-only bound, over the cap
        "multi L=1.0 O=1.0-\u0661",  # seg-only bound, non-ASCII
    ):
        parsed = parse_lines(bad, finish_reason="stop")
        assert parsed.errors == [], bad
        assert parsed.records[0].has_sentinel is True, bad
        assert parsed.records[0].review is True, bad
        assert parsed.stats.refs_bad >= 1, bad
        assert SENTINEL_REF in _refs(parsed.records[0]), bad


# --- literals under L / A, and the review flag -------------------------------


def test_literal_under_l_or_a_is_a_sentinel_and_flags_review():
    under_l = parse_lines('single L="abc" O=1.1', finish_reason="stop")
    assert under_l.errors == []
    assert under_l.records[0].label == (RefItem(SENTINEL_REF),)
    assert under_l.records[0].review is True
    assert under_l.stats.literal_items == 0

    under_a = parse_lines('single L=1.0 A="yes"', finish_reason="stop")
    assert under_a.records[0].answer == (RefItem(SENTINEL_REF),)
    assert under_a.records[0].review is True
    assert under_a.stats.literal_items == 0

    allowed = parse_lines('single L=9.0 O=9.1,"Yes No" N="note"', finish_reason="stop")
    record = allowed.records[0]
    assert record.options == (RefItem(SegmentRef(9, 1)), LiteralItem("Yes No"))
    assert record.notes == (LiteralItem("note"),)
    assert record.review is False
    assert allowed.stats.literal_items == 2


def test_duplicate_option_under_label_is_dropped_and_flags_review():
    parsed = parse_lines("single L=9.0,9.1 O=9.0,9.2", finish_reason="stop")
    record = parsed.records[0]
    assert record.options == (RefItem(SegmentRef(9, 2)),)
    assert record.review is True


# --- the span bound ----------------------------------------------------------


def test_span_width_bound():
    assert MAX_SPAN_WIDTH == 10_000

    ok = parse_lines("multi L=1.0 O=1.0-9999", finish_reason="stop")  # exactly the bound
    assert ok.records[0].options == (SpanItem(1, 0, 9999),)
    assert ok.stats.refs_total == 1 + MAX_SPAN_WIDTH
    assert ok.stats.refs_bad == 0

    too_wide = parse_lines("multi L=1.0 O=1.0-10000", finish_reason="stop")
    assert too_wide.records[0].options == (RefItem(SENTINEL_REF),)
    assert too_wide.records[0].review is True

    reversed_span = parse_lines("multi L=1.0 O=1.5-3", finish_reason="stop")
    assert reversed_span.records[0].options == (RefItem(SENTINEL_REF),)

    cross_row = parse_lines("multi L=1.0 O=1.2-2.1", finish_reason="stop")
    assert cross_row.records[0].options == (RefItem(SENTINEL_REF),)


# --- the diagnostic counters -------------------------------------------------


def test_stats_keys_and_consistency():
    assert set(LineStats.__dataclass_fields__) == STATS_KEYS

    parsed = parse_lines(
        "junk\n"
        "single L=1.0 O=1.1\n"
        "!! bad !!\n"
        'text L=2.0 A=2.2 N="note"\n'
        "end\n",
        finish_reason="stop",
    )
    stats = parsed.stats
    assert stats.lines_total == 6
    assert stats.records == 2
    assert stats.noise_lines == 2  # the preamble and the blank tail
    assert stats.error_lines == 1
    assert stats.literal_items == 1
    assert stats.refs_total == 4  # L+O then L+A
    assert stats.refs_bad == 0
    assert stats.end_seen is True
    assert stats.end_missing is False
    assert stats.truncated is False
    assert stats.dropped_tail is False
    assert stats.unknown_kind == 0
    assert stats.no_label == 0
    assert stats.empty_response is False
    assert stats.lines_total >= stats.records + stats.error_lines + stats.noise_lines


def test_counts_unknown_kind_and_missing_label():
    parsed = parse_lines(
        "single L=1.0\n"
        "single L=2.0\n"
        "frobnicate L=3.0 O=3.1\n"
        "single O=4.1\n"
        "helicopter L=5.0\n"
        "end\n",
        finish_reason="stop",
    )
    assert parsed.stats.unknown_kind == 2
    assert parsed.stats.no_label == 1
    assert parsed.stats.error_lines == 1
    assert parsed.errors == ["line 4: missing L="]
    assert [r.review for r in parsed.records] == [False, False, True, True]


# --- 7. empty / noise-only responses -----------------------------------------


def test_empty_and_noise_only_responses_are_not_truncated():
    for text in ("", "\n\n\n", "   \n\t\n", "```\n```\n", "Here you go:\n", "```json\n"):
        for reason in (None, "length", "stop"):
            parsed = parse_lines(text, finish_reason=reason)
            assert parsed.records == [], text
            assert parsed.truncated is False, (text, reason)
            assert parsed.stats.empty_response is True, (text, reason)
            assert parsed.stats.dropped_tail is False, (text, reason)
            assert parsed.errors == [EMPTY_ERROR], (text, reason)

    # A terminator with no fields is a clean, explicit empty extraction.
    terminator = parse_lines("end\n", finish_reason=None)
    assert terminator.records == []
    assert terminator.errors == []
    assert terminator.stats.empty_response is False
    assert terminator.stats.end_seen is True


def test_a_real_cut_is_still_truncated():
    parsed = parse_lines("single L=1.0 O=1.1,1.2\nsingle L=2.0 O=2.1,", finish_reason=None)
    assert parsed.truncated is True
    assert parsed.stats.empty_response is False
    assert parsed.dropped_line == 2
    assert parsed.errors == ["response truncated after 1 lines"]


# --- shared invariant checks -------------------------------------------------


def _check_shape(parsed) -> None:
    assert isinstance(parsed.records, list)
    assert isinstance(parsed.errors, list)
    assert isinstance(parsed.truncated, bool)
    stats = parsed.stats
    assert stats.records == len(parsed.records)
    assert stats.truncated is parsed.truncated
    assert stats.dropped_tail is (parsed.dropped_line is not None)
    assert stats.end_missing is not stats.end_seen
    assert stats.lines_total >= stats.records + stats.error_lines + stats.noise_lines


def _check_ref(ref) -> None:
    assert isinstance(ref, SegmentRef)
    assert isinstance(ref.row, int) and isinstance(ref.seg, int)
    assert not isinstance(ref.row, bool) and not isinstance(ref.seg, bool)


def _check_record_invariants(record) -> None:
    for item in _items(record):
        if isinstance(item, RefItem):
            _check_ref(item.ref)
        elif isinstance(item, JoinItem):
            for ref in item.refs:
                _check_ref(ref)
        elif isinstance(item, SpanItem):
            assert isinstance(item.row, int) and not isinstance(item.row, bool)
            assert isinstance(item.start, int) and isinstance(item.stop, int)
            assert item.start <= item.stop
            assert item.stop - item.start + 1 <= MAX_SPAN_WIDTH
        else:
            assert isinstance(item.text, str)


def _count_bad(records) -> int:
    return sum(
        1
        for record in records
        for item in _items(record)
        if lines._item_ref_is_bad(item)
    )


# --- total on a hostile corpus -----------------------------------------------


BIG_LINE = "single L=1.0 O=" + ",".join(f"1.{i % 40}" for i in range(200_000))

HOSTILE = [
    "",
    "\n",
    "\n\n\n",
    "```",
    "```json",
    "```text\n```",
    "end",
    "END.",
    "   end   ",
    "single",
    "single ",
    "single L",
    "single L=",
    "single L=,,",
    "single L=12",
    "single L=12.",
    "single L=.12",
    "single L=1.2-",
    "single L=-1.2",
    "single L=1.2+",
    "single L=+",
    "single L=r",
    "single L=O=1.2",
    "single L=1.2 garbage O=1.3",
    'single L=1.2 N="unterminated',
    'single L=1.2 O="a",""',
    'single L="x" A="y"',
    "single :1.2",
    "===",
    "::",
    "\x00\x01\x02",
    "single L=1.2\n\x00end",
    "single L=1.2\n\x00 end",
    "single L=1.2 O=1.3\x00,1.4",
    "single L=\ud800",
    "\udfff\ud800",
    "single L=1.2 O=1.3,1.4,1.5 ",
    "single L=1.2\n" * 500,
    "single L=" + "9" * 5000 + ".0",
    "single L=1.0 O=1.0-99999999",
    "single L=1.0 O=1.0+1.1+1.2+",
    "single L=1.0 O=+1.1",
    "single L=1.0 O=1.1-1.0-1.2",
    "multi L=1.0 O=1.1-1.2 L=2.0",
    "single L=1.0 N= N=",
    "single L=1.0 O=1.1 O=1.2",
    BIG_LINE,
]


def test_total_on_hostile_corpus_never_raises():
    for index, text in enumerate(HOSTILE):
        for reason in (None, "length", "stop", "MAX_TOKENS", "safety", 17, "stop "):
            try:
                parsed = parse_lines(text, finish_reason=reason)
            except Exception:  # pragma: no cover - the failure path
                pytest.fail(f"HOSTILE[{index}] with finish_reason={reason!r} raised")
            _check_shape(parsed)
            for record in parsed.records:
                _check_record_invariants(record)
            assert _count_bad(parsed.records) == parsed.stats.refs_bad


# --- b. round trips ----------------------------------------------------------


def _render_item(item) -> str:
    if isinstance(item, RefItem):
        return f"{item.ref.row}.{item.ref.seg}"
    if isinstance(item, JoinItem):
        return "+".join(f"{ref.row}.{ref.seg}" for ref in item.refs)
    if isinstance(item, SpanItem):
        return f"{item.row}.{item.start}-{item.stop}"
    return '"' + item.text + '"'


def render(records) -> str:
    """The test-only inverse of :func:`parse_lines` for clean records."""
    out: list[str] = []
    for record in records:
        parts = [
            record.kind.value,
            "L=" + ",".join(_render_item(item) for item in record.label),
        ]
        for key, items in (("O", record.options), ("A", record.answer), ("N", record.notes)):
            if items:
                parts.append(key + "=" + ",".join(_render_item(item) for item in items))
        out.append(" ".join(parts))
    out.append("end")
    return "\n".join(out)


_LITERAL_ALPHABET = string.ascii_letters + " .,=?+-"


def _random_items(rng, key, next_row):
    items = []
    for _ in range(rng.randint(1, 3)):
        row = next_row[0]
        next_row[0] += 1
        roll = rng.random()
        if key in ("O", "N") and roll < 0.25:
            text = "".join(
                rng.choice(_LITERAL_ALPHABET) for _ in range(rng.randint(0, 8))
            )
            items.append(LiteralItem(text))
        elif roll < 0.65:
            items.append(RefItem(SegmentRef(row, rng.randint(0, 5))))
        elif roll < 0.85:
            items.append(
                JoinItem(tuple(SegmentRef(row, seg) for seg in range(rng.randint(2, 4))))
            )
        else:
            start = rng.randint(0, 3)
            items.append(SpanItem(row, start, start + rng.randint(0, 4)))
    return items


def _random_records(rng, count):
    next_row = [1]
    records = []
    for index in range(count):
        record = LineRecord(
            kind=rng.choice(KINDS),
            line_no=index + 1,
            label=tuple(_random_items(rng, "L", next_row)),
        )
        if rng.random() < 0.6:
            record.options = tuple(_random_items(rng, "O", next_row))
        if rng.random() < 0.4:
            record.answer = tuple(_random_items(rng, "A", next_row))
        if rng.random() < 0.4:
            record.notes = tuple(_random_items(rng, "N", next_row))
        records.append(record)
    return records


def test_round_trip_random_records():
    rng = random.Random(20261007)
    for _ in range(1000):
        records = _random_records(rng, rng.randint(0, 6))
        parsed = parse_lines(render(records), finish_reason="stop")
        assert parsed.errors == []
        assert parsed.truncated is False
        assert parsed.records == records


# --- c. seeded fuzz ----------------------------------------------------------


FUZZ_SEED = 20261007
FUZZ_TARGET = 2000

_BASE_RESPONSE = "\n".join(
    f"{kind.value} L={i}.0 O={i}.1,{i}.2" for i, kind in enumerate(KINDS * 6)
) + "\nend"

_FUZZ_CHARS = string.printable + "\u0661\u06f1\uff11\u00b2\u2013\ud800\x00"


def _fuzz_inputs(rng):
    yield _BASE_RESPONSE
    for index in range(len(_BASE_RESPONSE) + 1):
        yield _BASE_RESPONSE[:index]
    for _ in range(FUZZ_TARGET):
        text = _BASE_RESPONSE
        mutation = rng.random()
        if mutation < 0.25:
            position = rng.randrange(len(text))
            text = text[:position] + rng.choice(_FUZZ_CHARS) + text[position + 1 :]
        elif mutation < 0.45:
            cut = rng.randrange(len(text) + 1)
            text = text[:cut] + text[:cut]
        elif mutation < 0.6:
            position = rng.randrange(len(text) + 1)
            text = text[:position] + "end\n" + text[position:]
        elif mutation < 0.72:
            position = rng.randrange(len(text) + 1)
            text = text[:position] + "\x00" + text[position:]
        elif mutation < 0.84:
            text = "```text\n" + text + "\n```\n"
        elif mutation < 0.94:
            position = rng.randrange(len(text))
            text = text[:position] + "9" * rng.choice((9, 10, 64, 5000)) + text[position:]
        else:
            half = len(text) // 2
            text = text[half:] + text[:half]
        if rng.random() < 0.2:
            text += rng.choice(("", "\n", "end", "END.", "\ud800", "9" * 5000))
        yield text


def _fuzz(seed, calls):
    rng = random.Random(seed)
    checked = 0
    for text in _fuzz_inputs(rng):
        calls.append(seed)
        parsed = lines.parse_lines(
            text,
            finish_reason=rng.choice((None, "stop", "length", "MAX_TOKENS", "safety")),
        )
        _check_shape(parsed)
        for record in parsed.records:
            _check_record_invariants(record)
        numbers = [record.line_no for record in parsed.records]
        assert numbers == sorted(set(numbers)), (seed, numbers)
        assert _count_bad(parsed.records) == parsed.stats.refs_bad
        checked += 1
    return checked


def test_fuzz_mutations_never_raise():
    calls: list[int] = []
    try:
        checked = _fuzz(FUZZ_SEED, calls)
    except Exception:  # pragma: no cover - the failure path
        pytest.fail(f"fuzz seed {FUZZ_SEED} raised")
    assert checked > FUZZ_TARGET
    assert len(calls) == checked


def test_fuzz_planted_raiser_is_caught(monkeypatch):
    assert hasattr(lines, "parse_lines")  # the patched symbol exists
    reached: list[int] = []

    def planted(text, *, finish_reason=None, steps=None):
        reached.append(1)
        raise RuntimeError("planted")

    real = lines.parse_lines
    monkeypatch.setattr(lines, "parse_lines", planted)
    with pytest.raises(RuntimeError):
        _fuzz(FUZZ_SEED, [])
    assert reached, "the planted parse_lines was never reached"
    monkeypatch.setattr(lines, "parse_lines", real)
    assert _fuzz(FUZZ_SEED, []) > FUZZ_TARGET  # and the real parser passes


# --- d. the injected step counter (never a clock) ----------------------------


def test_steps_exact_for_fixed_inputs():
    steps = [0]
    parse_lines("single L=1.0 O=1.1,1.2\nend", finish_reason="stop", steps=steps)
    assert steps[0] == 5

    steps = [0]
    parse_lines("single L=1.0", finish_reason="stop", steps=steps)
    assert steps[0] == 2

    steps = [0]
    parsed = parse_lines(
        "single L=1.0\nsingle L=2.0 O=2.1", finish_reason="length", steps=steps
    )
    assert steps[0] == 3
    assert parsed.dropped_line == 2

    steps = [0]
    parse_lines("", finish_reason="length", steps=steps)
    assert steps[0] == 1


def test_steps_grow_linearly():
    def cost(lines_count):
        text = (
            "\n".join(f"single L={i}.0 O={i}.1,{i}.2" for i in range(lines_count))
            + "\nend"
        )
        steps = [0]
        parse_lines(text, finish_reason="stop", steps=steps)
        return steps[0]

    small = cost(1000)
    large = cost(2000)
    assert small > 1000
    assert large > small
    assert large < 3 * small  # linear: doubling the input doubles the steps


def test_dropped_tail_costs_steps_not_items():
    text = "single L=1.0 O=" + ",".join(f"1.{i % 1000}" for i in range(50_000))

    dropped = [0]
    parsed = parse_lines(text, finish_reason="length", steps=dropped)
    assert parsed.dropped_line == 1
    assert parsed.records == []
    assert dropped[0] == 1  # one physical line, zero items parsed

    kept = [0]
    kept_parsed = parse_lines(text, finish_reason="stop", steps=kept)
    assert len(kept_parsed.records) == 1
    assert kept[0] > 50_000  # the same text, parsed: one step per item

    ten_mb = "single L=1.0 O=" + "1.1," * 2_500_000 + "1.2"
    steps = [0]
    big = parse_lines(ten_mb, finish_reason="length", steps=steps)
    assert big.dropped_line == 1
    assert big.errors == ["response truncated after 0 lines"]
    assert steps[0] == 1


# --- e. the static scan ------------------------------------------------------


def test_lines_module_is_stdlib_only_and_guards_int():
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported

    for node in ast.walk(tree):
        assert not (
            isinstance(node, ast.Attribute) and node.attr == "isdigit"
        ), f"lines.py:{node.lineno} calls .isdigit()"

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compile"
        ):
            pattern = ast.literal_eval(node.args[0])
            assert "\\d" not in pattern, f"lines.py:{node.lineno} regex uses \\d: {pattern}"

    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_digits_to_int"
    )
    int_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "int"
    ]
    assert len(int_calls) == 1, [call.lineno for call in int_calls]
    assert helper.lineno <= int_calls[0].lineno <= helper.end_lineno, (
        f"int() outside _digits_to_int at line {int_calls[0].lineno}"
    )


# --- f. mutation cases (anti-vacuity: exists, reached, differs) --------------


def test_mutation_non_ascii_digit_accepted(monkeypatch):
    assert hasattr(lines, "_parse_ref")  # the patched symbol exists
    text = "single L=\u0661\u0662.\u0660"
    before = parse_lines(text, finish_reason="stop").records[0].label
    assert before == (RefItem(SENTINEL_REF),)

    real = lines._parse_ref
    reached: list[int] = []

    def lenient(token):
        reached.append(1)
        match = re.match(r"^[rR]?(\d+)[.:](\d+)$", token.strip())
        if match is None:
            return real(token)
        return lines.SegmentRef(int(match.group(1)), int(match.group(2)))

    monkeypatch.setattr(lines, "_parse_ref", lenient)
    after = parse_lines(text, finish_reason="stop").records[0].label
    assert reached
    assert after == (RefItem(SegmentRef(12, 0)),)
    assert after != before


def test_mutation_digit_cap_removed(monkeypatch):
    assert hasattr(lines, "MAX_DIGITS")
    text = "single L=" + "9" * 5000 + ".0"
    before = parse_lines(text, finish_reason="stop").records[0].label
    assert before == (RefItem(SENTINEL_REF),)

    monkeypatch.setattr(lines, "MAX_DIGITS", 10**9)
    with pytest.raises(ValueError) as excinfo:  # int() was reached
        parse_lines(text, finish_reason="stop")
    assert "limit" in str(excinfo.value).lower()


def test_mutation_cross_row_range_accepted(monkeypatch):
    assert hasattr(lines, "_parse_item")
    text = "multi L=12.0 O=12.2-13.1"
    before = parse_lines(text, finish_reason="stop").records[0].options
    assert before == (RefItem(SENTINEL_REF),)

    real = lines._parse_item
    reached: list[int] = []

    def lenient(token, allow_literal):
        if "-" in token and not token.startswith('"'):
            head, _, tail = token.partition("-")
            ref = lines._parse_ref(head)
            match = re.match(r"^[rR]?[0-9]+[.:]([0-9]+)$", tail.strip())
            if ref is not None and match is not None:
                reached.append(1)
                return lines.SpanItem(ref.row, ref.seg, int(match.group(1)))
        return real(token, allow_literal)

    monkeypatch.setattr(lines, "_parse_item", lenient)
    after = parse_lines(text, finish_reason="stop").records[0].options
    assert reached
    assert after == (SpanItem(12, 2, 1),)
    assert after != before


def test_mutation_dropped_tail_parsed_and_kept(monkeypatch):
    assert hasattr(lines, "_is_cut")
    text = "single L=1.0 O=1.1,1.2\nsingle L=2.0 O=2.1,"
    before = parse_lines(text, finish_reason=None)
    assert before.truncated is True
    assert len(before.records) == 1

    reached: list[int] = []

    def never_cut(finish_reason, end_seen, has_records, tail_has_content):
        reached.append(1)
        return False

    monkeypatch.setattr(lines, "_is_cut", never_cut)
    after = parse_lines(text, finish_reason=None)
    assert reached
    assert after.truncated is False
    assert len(after.records) == 2


def test_mutation_cut_ignored_when_end_present(monkeypatch):
    assert hasattr(lines, "_is_cut")
    text = "single L=1.0\nend"
    before = parse_lines(text, finish_reason="length")
    assert before.truncated is True

    reached: list[int] = []

    def cut_ignores_end(finish_reason, end_seen, has_records, tail_has_content):
        reached.append(1)
        return lines.normalize_finish_reason(finish_reason) == "cut" and not end_seen

    monkeypatch.setattr(lines, "_is_cut", cut_ignores_end)
    after = parse_lines(text, finish_reason="length")
    assert reached
    assert after.truncated is False


def test_mutation_unknown_treated_as_clean(monkeypatch):
    assert hasattr(lines, "_is_cut")
    text = "single L=1.0\nsingle L=2.0 O=2.1,"
    before = parse_lines(text, finish_reason=None)
    assert before.truncated is True

    reached: list[int] = []

    def unknown_as_clean(finish_reason, end_seen, has_records, tail_has_content):
        reached.append(1)
        return lines.normalize_finish_reason(finish_reason) == "cut"

    monkeypatch.setattr(lines, "_is_cut", unknown_as_clean)
    after = parse_lines(text, finish_reason=None)
    assert reached
    assert after.truncated is False
    assert len(after.records) == 2


def test_mutation_one_bad_line_fails_whole_parse(monkeypatch):
    assert hasattr(lines, "_parse_line")
    text = "single L=1.0\n!! bad !!\nsingle L=2.0\nend"
    before = parse_lines(text, finish_reason="stop")
    assert len(before.records) == 2
    assert before.errors == ["line 2: expected a kind word"]

    real = lines._parse_line
    reached: list[int] = []

    def strict(stripped, line_no, tick):
        record, error = real(stripped, line_no, tick)
        reached.append(1)
        if record is None:
            raise ValueError("one bad line fails the chunk")
        return record, error

    monkeypatch.setattr(lines, "_parse_line", strict)
    with pytest.raises(ValueError):
        parse_lines(text, finish_reason="stop")
    assert reached


def test_mutation_literal_allowed_under_label(monkeypatch):
    assert hasattr(lines, "_LITERAL_KEYS")
    text = 'single L="abc"'
    before = parse_lines(text, finish_reason="stop").records[0].label
    assert before == (RefItem(SENTINEL_REF),)

    monkeypatch.setattr(lines, "_LITERAL_KEYS", ("L", "O", "A", "N"))
    after = parse_lines(text, finish_reason="stop").records[0].label
    assert after == (LiteralItem("abc"),)
    assert after != before


def test_mutation_span_bound_removed(monkeypatch):
    assert hasattr(lines, "MAX_SPAN_WIDTH")
    text = "multi L=14.0 O=14.0-9999999"
    before = parse_lines(text, finish_reason="stop").records[0].options
    assert before == (RefItem(SENTINEL_REF),)

    monkeypatch.setattr(lines, "MAX_SPAN_WIDTH", 10**9)
    after = parse_lines(text, finish_reason="stop").records[0].options
    assert after == (SpanItem(14, 0, 9999999),)
    assert after != before


def test_mutation_empty_response_reported_truncated(monkeypatch):
    assert hasattr(lines, "_is_empty_response")
    before = parse_lines("", finish_reason="length")
    assert before.truncated is False
    assert before.errors == [EMPTY_ERROR]

    reached: list[int] = []

    def never_empty(*args, **kwargs):
        reached.append(1)
        return False

    monkeypatch.setattr(lines, "_is_empty_response", never_empty)
    after = parse_lines("", finish_reason="length")
    assert reached
    assert after.truncated is True
    assert after.errors == ["response truncated after 0 lines"]


# --- g. the client layer -----------------------------------------------------


def test_llm_response_positional_construction():
    response = LLMResponse("text", "model", {}, 12, 34)
    assert response.text == "text"
    assert response.model == "model"
    assert response.params == {}
    assert response.tokens == 12
    assert response.latency_ms == 34
    assert response.finish_reason is None
    assert lines.normalize_finish_reason(response.finish_reason) == "unknown"

    filled = LLMResponse("t", "m", {}, finish_reason="length")
    assert filled.finish_reason == "length"
    assert lines.normalize_finish_reason(filled.finish_reason) == "cut"

