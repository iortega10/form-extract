"""0.6.0 L1a: the line-contract parser (pure, total, no resolver)."""
from __future__ import annotations

import random
import string

from formextract.lines import (
    SENTINEL_REF,
    JoinItem,
    LineKind,
    LiteralItem,
    RefItem,
    SegmentRef,
    SpanItem,
    normalize_finish_reason,
    parse_lines,
)
from formextract.resolve import LLMResponse


def _refs(record) -> list[SegmentRef]:
    return list(record.refs())


# --- shape corpus (docs/design/0.6.0-line-contract-debate.md section 3.4) ----


def test_worked_examples_parse_to_records():
    text = "\n".join(
        [
            "single L=12.0 O=12.2,12.4",
            "multi L=14.0 O=14.1-12",
            "multi L=20.0 N=19.0 O=21.0-9,22.0-9,23.0-9,24.0-9,25.0-9",
            "multi L=30.0 O=30.2,31.2,32.2,33.2",
            "single L=40.0 O=40.2,40.4",
            "single L=40.5 O=40.7,40.9",
            "text L=50.0,51.0 A=50.2",
            "hdr L=3.0",
            "note L=5.0",
            "skip L=6.0",
            "text L=7.0 A=7.2",
            "bool L=8.0 A=8.2",
            "single L=9.0 O=9.1,9.2 A=9.2",
            "end",
        ]
    )
    parsed = parse_lines(text, finish_reason="stop")

    assert parsed.errors == []
    assert parsed.truncated is False
    assert parsed.stats.end_seen is True
    assert [r.kind for r in parsed.records] == [
        LineKind.SINGLE,
        LineKind.MULTI,
        LineKind.MULTI,
        LineKind.MULTI,
        LineKind.SINGLE,
        LineKind.SINGLE,
        LineKind.TEXT,
        LineKind.HDR,
        LineKind.NOTE,
        LineKind.SKIP,
        LineKind.TEXT,
        LineKind.BOOL,
        LineKind.SINGLE,
    ]
    assert parsed.records[0].label == (RefItem(SegmentRef(12, 0)),)
    assert parsed.records[0].options == (
        RefItem(SegmentRef(12, 2)),
        RefItem(SegmentRef(12, 4)),
    )
    assert parsed.records[1].options == (SpanItem(14, 1, 12),)
    assert parsed.records[2].notes == (RefItem(SegmentRef(19, 0)),)
    assert parsed.records[6].label == (
        RefItem(SegmentRef(50, 0)),
        RefItem(SegmentRef(51, 0)),
    )
    assert parsed.records[6].answer == (RefItem(SegmentRef(50, 2)),)


def test_span_expands_to_every_element_in_order():
    parsed = parse_lines("multi L=14.0 O=14.1-12", finish_reason="stop")
    span = parsed.records[0].options[0]
    assert isinstance(span, SpanItem)
    assert [ref.seg for ref in span_refs(span)] == list(range(1, 13))


def span_refs(span: SpanItem):
    return [SegmentRef(span.row, seg) for seg in range(span.start, span.stop + 1)]


def test_join_is_one_option_over_several_elements():
    parsed = parse_lines("multi L=12.0 O=12.2+12.3+12.4", finish_reason="stop")
    assert parsed.records[0].options == (
        JoinItem((SegmentRef(12, 2), SegmentRef(12, 3), SegmentRef(12, 4))),
    )


def test_literal_items_are_counted_and_kept_out_of_the_refs():
    parsed = parse_lines('single L=9.0 O=9.1,"Yes No"', finish_reason="stop")
    record = parsed.records[0]
    assert record.options == (RefItem(SegmentRef(9, 1)), LiteralItem("Yes No"))
    assert parsed.stats.literal_items == 1
    # Only the ref options contribute refs; the literal names no element.
    assert _refs(record) == [SegmentRef(9, 0), SegmentRef(9, 1)]


def test_literal_keeps_an_internal_comma():
    parsed = parse_lines('note L=5.0 N="see A, B"', finish_reason="stop")
    assert parsed.records[0].notes == (LiteralItem("see A, B"),)


# --- lexical leniency (section 3.3) ------------------------------------------


def test_kind_and_key_are_case_insensitive_and_three_letters_work():
    parsed = parse_lines(
        "SIN l:12.0 O=12.2,12.4\n"
        "MUL L=14.0 o=14.1-2\n"
        "BOO L=8.0 A=8.2\n"
        "TEX L=7.0 A=7.2\n"
        "HDR L=3.0\n"
        "HEA L=4.0\n"
        "NOT L=5.0\n"
        "SKI L=6.0\n"
        "end",
        finish_reason="stop",
    )
    assert parsed.errors == []
    assert [r.kind for r in parsed.records] == [
        LineKind.SINGLE,
        LineKind.MULTI,
        LineKind.BOOL,
        LineKind.TEXT,
        LineKind.HDR,
        LineKind.HDR,
        LineKind.NOTE,
        LineKind.SKIP,
    ]


def test_ref_accepts_row_prefix_colon_separator_and_spaces_around_commas():
    parsed = parse_lines("single L=r12:0 O=r12:2, 12.4", finish_reason="stop")
    assert parsed.records[0].label == (RefItem(SegmentRef(12, 0)),)
    assert parsed.records[0].options == (
        RefItem(SegmentRef(12, 2)),
        RefItem(SegmentRef(12, 4)),
    )


def test_unknown_kind_degrades_to_a_review_flagged_field():
    with_options = parse_lines("kindof L=9.0 O=9.1,9.2", finish_reason="stop")
    record = with_options.records[0]
    assert record.kind is LineKind.SINGLE
    assert record.review is True
    assert record.unknown_kind == "kindof"

    without_options = parse_lines("kindof L=9.0", finish_reason="stop")
    record = without_options.records[0]
    assert record.kind is LineKind.TEXT
    assert record.review is True
    assert without_options.errors == []


def test_unknown_kind_is_review_flagged_not_a_line_error():
    parsed = parse_lines("frobnicate L=1.0 O=1.1", finish_reason="stop")
    assert parsed.errors == []
    assert len(parsed.records) == 1


# --- sentinel refs and line errors (sections 3.2, 3.3) -----------------------


def test_cross_row_range_becomes_a_sentinel_ref_not_a_line_error():
    parsed = parse_lines("multi L=12.2-13.1\nend", finish_reason="stop")
    assert parsed.errors == []
    assert parsed.records[0].label == (RefItem(SENTINEL_REF),)
    assert parsed.records[0].label[0].ref.is_sentinel is True


def test_a_bad_ref_is_a_sentinel():
    parsed = parse_lines("single L=notaref O=9.1", finish_reason="stop")
    assert parsed.errors == []
    assert parsed.records[0].label == (RefItem(SENTINEL_REF),)
    assert parsed.records[0].options == (RefItem(SegmentRef(9, 1)),)


def test_a_descending_span_is_a_sentinel():
    parsed = parse_lines("multi L=1.0 O=1.5-3", finish_reason="stop")
    assert parsed.errors == []
    assert parsed.records[0].options == (RefItem(SENTINEL_REF),)


def test_missing_label_is_a_line_error():
    parsed = parse_lines("single L=1.0 O=1.1\nsingle O=9.1,9.2\nend", finish_reason="stop")
    assert [r.label for r in parsed.records] == [(RefItem(SegmentRef(1, 0)),)]
    assert parsed.errors == ["line 2: missing L="]


def test_text_before_the_first_record_is_noise_not_an_error():
    # A malformed line before any record is preamble noise (section 3.3); the
    # same line between records is a per-line error.
    parsed = parse_lines("this is not a record\nsingle L=1.0\nend", finish_reason="stop")
    assert [r.label for r in parsed.records] == [(RefItem(SegmentRef(1, 0)),)]
    assert parsed.errors == []
    assert parsed.stats.noise_lines == 1


def test_one_bad_line_between_records_does_not_drop_the_chunk():
    parsed = parse_lines(
        "single L=1.0 O=1.1\n"
        "!! garbage !!\n"
        "single L=2.0 O=2.1,2.2\n"
        "end",
        finish_reason="stop",
    )
    assert [r.label for r in parsed.records] == [
        (RefItem(SegmentRef(1, 0)),),
        (RefItem(SegmentRef(2, 0)),),
    ]
    assert parsed.errors == ["line 2: expected a kind word"]


def test_missing_equals_is_a_line_error():
    parsed = parse_lines("single L=1.0\nsingle L 1.0\nend", finish_reason="stop")
    assert [r.label for r in parsed.records] == [(RefItem(SegmentRef(1, 0)),)]
    assert parsed.errors == ["line 2: expected '=' after 'L'"]


# --- noise and the terminator (sections 3.3, 3.5) ----------------------------


def test_noise_is_blank_lines_fences_preamble_and_post_end_text():
    parsed = parse_lines(
        "Here are the fields:\n"
        "```text\n"
        "single L=1.0 O=1.1\n"
        "\n"
        "```\n"
        "end\n"
        "any trailing prose is dropped",
        finish_reason="stop",
    )
    assert [r.label for r in parsed.records] == [(RefItem(SegmentRef(1, 0)),)]
    assert parsed.errors == []
    assert parsed.stats.noise_lines == 5


def test_terminator_forms_are_normalised():
    for terminator in ("end", "END", "End", "end."):
        parsed = parse_lines(f"single L=1.0\n{terminator}", finish_reason=None)
        assert parsed.stats.end_seen is True
        assert parsed.stats.end_missing is False
        assert parsed.errors == []


def test_end_is_not_a_record_even_with_a_form_row_named_end():
    parsed = parse_lines("single L=1.0\nend", finish_reason="stop")
    assert len(parsed.records) == 1


# --- truncation and the finish reason (section 3.5) --------------------------


def test_normalize_finish_reason_maps_the_recognised_sets():
    assert normalize_finish_reason("length") == "cut"
    assert normalize_finish_reason("MAX_TOKENS") == "cut"
    assert normalize_finish_reason("stop") == "clean"
    assert normalize_finish_reason("end_turn") == "clean"
    assert normalize_finish_reason("safety") == "unknown"
    assert normalize_finish_reason("other") == "unknown"
    assert normalize_finish_reason(None) == "unknown"


def test_a_clean_stop_without_a_terminator_is_not_truncated():
    parsed = parse_lines("single L=1.0 O=1.1,1.2", finish_reason="stop")
    assert parsed.truncated is False
    assert parsed.errors == []
    assert parsed.dropped_line is None
    assert parsed.stats.end_missing is True


def test_unknown_reason_with_a_terminator_is_not_truncated():
    parsed = parse_lines("single L=1.0\nend", finish_reason=None)
    assert parsed.truncated is False
    assert parsed.errors == []


def test_unknown_reason_without_a_terminator_truncates_and_drops_the_tail():
    parsed = parse_lines(
        "single L=1.0 O=1.1,1.2\nsingle L=2.0 O=2.1,", finish_reason=None
    )
    assert parsed.truncated is True
    assert [r.label for r in parsed.records] == [(RefItem(SegmentRef(1, 0)),)]
    assert parsed.dropped_line == 2
    assert parsed.stats.dropped_tail is True
    assert parsed.errors == ["response truncated after 1 lines"]


def test_a_recognised_cut_drops_the_last_line_too():
    parsed = parse_lines(
        "single L=1.0 O=1.1\nsingle L=2.0 O=2.1,2.2", finish_reason="length"
    )
    assert parsed.truncated is True
    assert [r.label for r in parsed.records] == [(RefItem(SegmentRef(1, 0)),)]
    assert parsed.errors == ["response truncated after 1 lines"]


def test_a_cut_that_ended_on_a_newline_keeps_every_record_but_reports_the_cut():
    parsed = parse_lines("single L=1.0 O=1.1\n", finish_reason="length")
    assert parsed.truncated is True
    assert len(parsed.records) == 1
    assert parsed.dropped_line is None
    assert parsed.errors == ["response truncated after 1 lines"]


def test_a_cut_of_an_empty_response_is_empty_not_truncated():
    # Nothing was cut: an empty response is its own diagnostic (section 3.5),
    # even when the provider reports a cut.
    parsed = parse_lines("", finish_reason="length")
    assert parsed.records == []
    assert parsed.truncated is False
    assert parsed.stats.empty_response is True
    assert parsed.stats.dropped_tail is False
    assert parsed.errors == ["response empty"]


# --- totality (section 3.3: the parser never raises) -------------------------


HOSTILE = [
    "",
    "\n\n\n",
    "```",
    "```json",
    "end",
    "END.",
    "single",
    "single L=",
    "single L=,,",
    "single L=12",
    "single L=1.2-",
    "single L=-1.2",
    "single L=1.2+",
    "single L=r",
    "single L=O=1.2",
    "single L=1.2 garbage O=1.3",
    'single L=1.2 N="unterminated',
    'single L=1.2 O="a",""',
    "single :1.2",
    "===",
    "\x00\x01\x02",
    "single L=1.2\n\x00end",
    "single L=99999999999999999999999999.0",
    "single L=1.2 O=1.3 O=1.4",
    "single L=1.2\n" * 500,
]


def test_parser_is_total_on_a_hostile_corpus():
    for text in HOSTILE:
        parsed = parse_lines(text, finish_reason="length")
        assert parsed.records is not None
        assert isinstance(parsed.errors, list)
        assert isinstance(parsed.truncated, bool)


def test_parser_is_total_on_random_bytes():
    rng = random.Random(20261007)
    alphabet = string.printable + "áé\u00ff\u2013\u2014\u2212"
    for _ in range(500):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
        parsed = parse_lines(text, finish_reason=rng.choice([None, "stop", "length"]))
        assert isinstance(parsed.records, list)


# --- the client layer --------------------------------------------------------


def test_llm_response_finish_reason_defaults_to_none():
    response = LLMResponse(text="x", model="m", params={})
    assert response.finish_reason is None
    assert normalize_finish_reason(response.finish_reason) == "unknown"
