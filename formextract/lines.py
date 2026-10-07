"""0.6.0 L1a: the line-contract response parser (pure and total).

A pure function over the model's raw text: it never raises, never reads the
layout, the lattice or the resolver, and carries no state. The grammar
(``docs/design/0.6.0-line-contract-debate.md`` section 3.2)::

    response = { noise | line } , [ endline , { any } ] ;
    line     = kind , { SP , field } ;
    kind     = "single" | "multi" | "bool" | "text" | "hdr" | "note" | "skip" ;
    field    = key , ( "=" | ":" ) , items ;
    key      = "L" | "O" | "A" | "N" ;
    items    = item , { "," , item } ;
    item     = ref | join | span | literal ;
    join     = ref , "+" , ref , { "+" , ref } ;
    span     = ref , "-" , seg ;
    ref      = [ "r" | "R" ] , row , ( "." | ":" ) , seg ;
    literal  = '"' , { char - ( '"' | NL ) } , '"' ;
    endline  = "end" ;

A ref that does not exist in a projection (a bad row or segment, or a
cross-row range a span cannot express) is not an error here: it becomes
:data:`SENTINEL_REF`, a ref with a negative row that the resolver later
rejects as an unresolved reference, exactly as the JSON path treats one. Only
a structurally unparsable line - one that is not ``kind`` then ``key=items`` -
is a per-line error (``line N: ...``), and a line is a record, never a
physical fragment: a literal cannot contain a newline.

Totality and bounds (0.6.0-L1a-fix):

* Every number is validated *before* it is converted: :func:`_digits_to_int`
  is the module's only ``int()`` call and accepts ASCII digits only, at most
  :data:`MAX_DIGITS` of them. A longer or non-ASCII digit run is a sentinel,
  never an exception - ``\\d`` is not used (it matches every Unicode decimal
  digit) and a bare ``str.isdigit()`` appears nowhere.
* A span may cover at most :data:`MAX_SPAN_WIDTH` elements; a wider (or
  reversed, or cross-row) range is a sentinel. The span item stays
  unexpanded here - the resolver expands it later.
* A literal is allowed under ``O`` and ``N`` only (section 3.2); under ``L``
  or ``A`` it is a sentinel item.
* A record is ``review=True`` when it holds a sentinel, when an ``O`` item
  repeats one of its ``L`` elements (the duplicate ``O`` occurrence is
  dropped, ``L`` wins), or when its kind is unknown. A line whose ``L=`` is
  missing is a per-line error instead: no record can exist without a label.
* An empty or noise-only response is not a truncation: it is reported once as
  :data:`EMPTY_ERROR` with ``stats.empty_response`` set, because nothing was
  cut (section 3.5).
* The trailing segment is decided before it is parsed: when the response is
  cut, the last line is dropped without parsing it, so a dropped tail costs
  O(len) work, never O(items).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

__all__ = [
    "DIGITS",
    "EMPTY_ERROR",
    "FIELD_KINDS",
    "LINE_KINDS",
    "MAX_DIGITS",
    "MAX_SPAN_WIDTH",
    "SENTINEL_REF",
    "SENTINEL_ROW",
    "TRUNCATION_ERROR_PREFIX",
    "JoinItem",
    "LineItem",
    "LineKind",
    "LineParse",
    "LineRecord",
    "LineStats",
    "LiteralItem",
    "RefItem",
    "SegmentRef",
    "SpanItem",
    "item_refs",
    "normalize_finish_reason",
    "parse_lines",
]


class LineKind(str, Enum):
    """A record's kind: a control kind or a pure disposition."""

    SINGLE = "single"
    MULTI = "multi"
    BOOL = "bool"
    TEXT = "text"
    HDR = "hdr"
    NOTE = "note"
    SKIP = "skip"


LINE_KINDS = tuple(kind.value for kind in LineKind)
FIELD_KINDS = (LineKind.SINGLE, LineKind.MULTI, LineKind.BOOL, LineKind.TEXT)

SENTINEL_ROW = -1

# The most digits one number (a row or a segment) may carry. Longer is a
# sentinel: a projection coordinate never needs it, and Python refuses to
# ``int()`` an unbounded digit string at all.
MAX_DIGITS = 9

# The most elements one span may cover. Wider is a sentinel, so a model cannot
# hand the resolver an expansion bomb.
MAX_SPAN_WIDTH = 10_000

# The ASCII digit alphabet. ``str.isdigit`` is deliberately not used: it is
# true for Arabic-Indic and fullwidth digits, which ``int()`` also accepts.
DIGITS = "0123456789"

TRUNCATION_ERROR_PREFIX = "response truncated after "
EMPTY_ERROR = "response empty"

# The recognised-cut and recognised-clean finish reasons (section 3.5). A
# provider's own word may be anything else - an OpenAI proxy's `stop` after a
# hard cut, a safety stop - so anything outside both sets is `unknown` and
# falls to the terminator rule.
_CUT_FINISH = frozenset(
    {"length", "max_tokens", "max_output_tokens", "max_completion_tokens"}
)
_CLEAN_FINISH = frozenset({"stop", "end_turn", "stop_sequence", "eos"})

# Kind accepted by its first three letters (case-insensitive).
_KIND_PREFIX = {
    "sin": LineKind.SINGLE,
    "mul": LineKind.MULTI,
    "boo": LineKind.BOOL,
    "tex": LineKind.TEXT,
    "hdr": LineKind.HDR,
    "hea": LineKind.HDR,
    "not": LineKind.NOTE,
    "ski": LineKind.SKIP,
}

_FIELD_KEYS = ("L", "O", "A", "N")

# A literal is the grammar's escape for text with no element boundary; section
# 3.2 allows it under O and N only.
_LITERAL_KEYS = ("O", "N")

_NO_LABEL = "missing L="

_RE_WORD = re.compile(r"[A-Za-z]+")
# ASCII only: ``\d`` would match Arabic-Indic and fullwidth digits.
_RE_REF = re.compile(r"^[rR]?([0-9]+)[.:]([0-9]+)$")
_RE_SEG = re.compile(r"^[0-9]+$")


def normalize_finish_reason(finish_reason: str | None) -> str:
    """Classify a provider finish reason as ``"cut"``, ``"clean"`` or ``"unknown"``.

    ``None`` (no reason reported) is ``unknown``: a proxy may return ``stop``
    after a hard cut, so absence is not evidence of a clean stop.
    """
    if finish_reason is None:
        return "unknown"
    value = str(finish_reason).strip().lower()
    if value in _CUT_FINISH:
        return "cut"
    if value in _CLEAN_FINISH:
        return "clean"
    return "unknown"


@dataclass(frozen=True)
class SegmentRef:
    """A ``row.seg`` projection coordinate, or :data:`SENTINEL_REF`."""

    row: int
    seg: int

    @property
    def is_sentinel(self) -> bool:
        return self.row < 0 or self.seg < 0


SENTINEL_REF = SegmentRef(SENTINEL_ROW, SENTINEL_ROW)


@dataclass(frozen=True)
class RefItem:
    """One ``row.seg`` element."""

    ref: SegmentRef


@dataclass(frozen=True)
class JoinItem:
    """Several refs joined by ``+``: ONE option or one run of elements."""

    refs: tuple[SegmentRef, ...]


@dataclass(frozen=True)
class SpanItem:
    """A ``ref-seg`` span: one option per element of ``row`` in ``[start, stop]``."""

    row: int
    start: int
    stop: int


@dataclass(frozen=True)
class LiteralItem:
    """A quoted string with no element boundary (``O`` and ``N`` only)."""

    text: str


LineItem = RefItem | JoinItem | SpanItem | LiteralItem


def item_refs(item: LineItem) -> tuple[SegmentRef, ...]:
    """Every ref an item names, in order (a literal names none)."""
    if isinstance(item, RefItem):
        return (item.ref,)
    if isinstance(item, JoinItem):
        return item.refs
    if isinstance(item, SpanItem):
        return tuple(SegmentRef(item.row, seg) for seg in range(item.start, item.stop + 1))
    return ()


def _item_ref_is_bad(item: LineItem) -> bool:
    """Whether one item names a sentinel ref (a span never does: ``[start, stop]``)."""
    if isinstance(item, RefItem):
        return item.ref.is_sentinel
    if isinstance(item, JoinItem):
        return any(ref.is_sentinel for ref in item.refs)
    return False


@dataclass
class LineRecord:
    """One parsed ``line``: a kind plus its four keyed item lists.

    ``review`` is set when the record is suspect without re-scanning its items:
    it holds a sentinel, an ``O`` occurrence of an ``L`` element was dropped,
    or the kind is unknown (``unknown_kind`` keeps the model's word).
    """

    kind: LineKind
    line_no: int
    label: tuple[LineItem, ...] = ()
    options: tuple[LineItem, ...] = ()
    answer: tuple[LineItem, ...] = ()
    notes: tuple[LineItem, ...] = ()
    review: bool = False
    unknown_kind: str | None = None

    def items(self, key: str) -> tuple[LineItem, ...]:
        return {
            "L": self.label,
            "O": self.options,
            "A": self.answer,
            "N": self.notes,
        }[key.upper()]

    def refs(self) -> tuple[SegmentRef, ...]:
        return tuple(
            ref
            for group in (self.label, self.options, self.answer, self.notes)
            for item in group
            for ref in item_refs(item)
        )

    @property
    def has_sentinel(self) -> bool:
        return any(
            _item_ref_is_bad(item)
            for group in (self.label, self.options, self.answer, self.notes)
            for item in group
        )


@dataclass(frozen=True)
class LineStats:
    """Diagnostic counters the parser reports beside the records.

    ``lines_total`` is the number of physical lines in the input;
    ``records`` the records kept; ``noise_lines`` the blank, fenced, preamble
    and post-``end`` lines; ``error_lines`` the per-line errors (the
    truncation and empty-response errors are not counted here);
    ``literal_items`` the ``LiteralItem``s kept; ``refs_total`` every ref the
    kept records name (a span counts its width, it is not expanded);
    ``refs_bad`` the sentinel refs among them; ``end_seen``/``end_missing``
    the terminator; ``truncated``/``dropped_tail`` the section-3.5 cut;
    ``unknown_kind`` the records whose kind word was unknown; ``no_label``
    the lines rejected for a missing ``L=``; ``empty_response`` when the
    response held neither a record, nor an error, nor an ``end``.
    """

    lines_total: int = 0
    records: int = 0
    noise_lines: int = 0
    error_lines: int = 0
    literal_items: int = 0
    refs_total: int = 0
    refs_bad: int = 0
    end_seen: bool = False
    end_missing: bool = True
    truncated: bool = False
    dropped_tail: bool = False
    unknown_kind: int = 0
    no_label: int = 0
    empty_response: bool = False


@dataclass
class LineParse:
    """The parser's total output: records, per-line errors, stats, truncation.

    ``dropped_line`` is the 1-based number of the trailing line a cut dropped
    (it is never parsed: see the module docstring), or ``None``.
    """

    records: list[LineRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stats: LineStats = field(default_factory=LineStats)
    truncated: bool = False
    dropped_line: int | None = None


def _ticker(steps: list[int] | None) -> Callable[[], None]:
    if steps is None:
        return lambda: None

    def tick() -> None:
        steps[0] += 1

    return tick


def parse_lines(
    text: str,
    *,
    finish_reason: str | None = None,
    steps: list[int] | None = None,
) -> LineParse:
    """Parse ``text`` into line records; total, never raises.

    ``finish_reason`` is the provider's stop reason for this response (see
    :func:`normalize_finish_reason`). An empty or noise-only response is not a
    truncation (nothing was cut): it is reported as :data:`EMPTY_ERROR` with
    ``stats.empty_response``. Otherwise a recognised cut, or an ``unknown``
    reason with no ``end`` and content, marks the response truncated and the
    trailing segment is dropped - decided *before* it is parsed, so a dropped
    tail never costs its items - with the error
    ``response truncated after N lines``.

    When ``steps`` is given, it is a one-element list incremented once per
    physical line and once per item parsed, so a caller can assert the cost is
    linear in the input (and that a dropped tail costs O(len), not O(items)).
    """
    tick = _ticker(steps)
    lines = text.split("\n")
    total = len(lines)
    head = lines[:-1]
    tail_stripped = lines[-1].strip()

    records: list[LineRecord] = []
    errors: list[str] = []
    noise_lines = 0
    error_lines = 0
    no_label = 0
    end_seen = False
    ended = False
    seen_record = False

    def absorb(stripped: str, line_no: int) -> None:
        nonlocal noise_lines, error_lines, no_label, end_seen, ended, seen_record
        if stripped == "" or stripped.startswith("```"):
            noise_lines += 1
            return
        if _is_end(stripped):
            end_seen = True
            ended = True
            return
        record, error = _parse_line(stripped, line_no, tick)
        if record is None:
            if seen_record:
                errors.append(f"line {line_no}: {error}")
                error_lines += 1
                if error == _NO_LABEL:
                    no_label += 1
            else:
                # Text before the first record is dropped as noise.
                noise_lines += 1
            return
        records.append(record)
        seen_record = True

    for index, raw_line in enumerate(head):
        tick()
        if ended:
            noise_lines += 1
            continue
        absorb(raw_line.strip(), index + 1)

    # Classify the trailing segment without parsing a line we may drop.
    tail_is_noise = tail_stripped == "" or tail_stripped.startswith("```")
    tail_is_end = _is_end(tail_stripped)
    tail_pending = False
    tick()
    if ended:
        noise_lines += 1
    elif tail_is_end:
        end_seen = True
    elif tail_is_noise:
        noise_lines += 1
    else:
        tail_pending = True

    truncated = False
    empty_response = False
    dropped_line: int | None = None

    if _is_empty_response(bool(records), error_lines, end_seen, tail_pending):
        empty_response = True
        errors.append(EMPTY_ERROR)
    elif _is_cut(finish_reason, end_seen, bool(records), tail_pending):
        truncated = True
        if tail_pending:
            dropped_line = total
        errors.append(f"{TRUNCATION_ERROR_PREFIX}{len(records)} lines")
    elif tail_pending:
        absorb(tail_stripped, total)

    literal_items = 0
    refs_total = 0
    refs_bad = 0
    unknown_kind = 0
    for record in records:
        literals, refs, bad = _count_items(record)
        literal_items += literals
        refs_total += refs
        refs_bad += bad
        if record.unknown_kind is not None:
            unknown_kind += 1

    stats = LineStats(
        lines_total=total,
        records=len(records),
        noise_lines=noise_lines,
        error_lines=error_lines,
        literal_items=literal_items,
        refs_total=refs_total,
        refs_bad=refs_bad,
        end_seen=end_seen,
        end_missing=not end_seen,
        truncated=truncated,
        dropped_tail=dropped_line is not None,
        unknown_kind=unknown_kind,
        no_label=no_label,
        empty_response=empty_response,
    )
    return LineParse(
        records=records,
        errors=errors,
        stats=stats,
        truncated=truncated,
        dropped_line=dropped_line,
    )


def _count_items(record: LineRecord) -> tuple[int, int, int]:
    """``(literals, refs, bad_refs)`` for one record; a span counts its width."""
    literals = 0
    refs = 0
    bad = 0
    for group in (record.label, record.options, record.answer, record.notes):
        for item in group:
            if isinstance(item, LiteralItem):
                literals += 1
            elif isinstance(item, RefItem):
                refs += 1
                bad += 1 if item.ref.is_sentinel else 0
            elif isinstance(item, JoinItem):
                refs += len(item.refs)
                bad += sum(1 for ref in item.refs if ref.is_sentinel)
            else:
                refs += item.stop - item.start + 1
    return literals, refs, bad


def _is_empty_response(
    has_records: bool, error_lines: int, end_seen: bool, tail_pending: bool
) -> bool:
    """Whether nothing at all was cut: no record, no error, no terminator, no data tail."""
    return not has_records and error_lines == 0 and not end_seen and not tail_pending


def _is_cut(
    finish_reason: str | None,
    end_seen: bool,
    has_records: bool,
    tail_has_content: bool,
) -> bool:
    """The section-3.5 decision rule, in order: finish reason, then terminator."""
    klass = normalize_finish_reason(finish_reason)
    if klass == "cut":
        return True
    if klass == "clean":
        return False
    # `unknown`: only a terminator rules out a cut, and only when there is
    # something a cut could have taken (a record or a non-empty tail).
    return not end_seen and (has_records or tail_has_content)


def _is_end(stripped: str) -> bool:
    return stripped.lower().rstrip(".").strip() == "end"


def _parse_line(
    stripped: str, line_no: int, tick: Callable[[], None]
) -> tuple[LineRecord | None, str]:
    match = _RE_WORD.match(stripped)
    if match is None:
        return None, "expected a kind word"
    word = match.group(0)
    keys, error = _parse_fields(stripped[match.end() :])
    if error is not None:
        return None, error

    kind = _KIND_PREFIX.get(word.lower()[:3])
    review = False
    unknown_kind: str | None = None
    if kind is None:
        # An unknown kind degrades to a review-flagged field: single when it
        # lists options, else text.
        review = True
        unknown_kind = word
        kind = LineKind.SINGLE if "O" in keys else LineKind.TEXT

    label = tuple(_parse_items(keys.get("L", ""), "L", tick))
    if not label:
        return None, _NO_LABEL
    options = _parse_items(keys.get("O", ""), "O", tick)
    label_refs = {ref for item in label for ref in item_refs(item)}
    kept: list[LineItem] = []
    for item in options:
        refs = item_refs(item)
        if refs and all(ref in label_refs for ref in refs):
            # An element is never both L and O: L wins, the O occurrence is
            # dropped and the field is review-flagged.
            review = True
            continue
        kept.append(item)
    answer = tuple(_parse_items(keys.get("A", ""), "A", tick))
    notes = tuple(_parse_items(keys.get("N", ""), "N", tick))
    record = LineRecord(
        kind=kind,
        line_no=line_no,
        label=label,
        options=tuple(kept),
        answer=answer,
        notes=notes,
        review=review,
        unknown_kind=unknown_kind,
    )
    if record.has_sentinel:
        review = True
        record.review = True
    return record, ""


def _parse_fields(rest: str) -> tuple[dict[str, str], str | None]:
    """Split the text after the kind word into ``key -> raw items`` groups."""
    keys: dict[str, str] = {}
    index = 0
    length = len(rest)
    while index < length:
        while index < length and rest[index].isspace():
            index += 1
        if index >= length:
            break
        key = rest[index]
        upper = key.upper()
        if upper not in _FIELD_KEYS:
            return keys, f"unexpected key {key!r}"
        cursor = index + 1
        if cursor >= length or rest[cursor] not in "=:":
            return keys, f"expected '=' after {key!r}"
        start = cursor + 1
        cursor = start
        in_string = False
        while cursor < length:
            char = rest[cursor]
            if in_string:
                if char == '"':
                    in_string = False
                cursor += 1
            elif char == '"':
                in_string = True
                cursor += 1
            elif char.isspace():
                ahead = cursor
                while ahead < length and rest[ahead].isspace():
                    ahead += 1
                if _starts_field(rest, ahead):
                    break
                cursor += 1
            else:
                cursor += 1
        raw = rest[start:cursor].strip()
        keys[upper] = f"{keys[upper]},{raw}" if upper in keys else raw
        index = cursor
    return keys, None


def _starts_field(text: str, index: int) -> bool:
    return (
        index < len(text)
        and text[index].upper() in _FIELD_KEYS
        and index + 1 < len(text)
        and text[index + 1] in "=:"
    )


def _parse_items(raw: str, key: str, tick: Callable[[], None]) -> list[LineItem]:
    raw = raw.strip()
    if not raw:
        return []
    allow_literal = key in _LITERAL_KEYS
    items: list[LineItem] = []
    for part in _split_items(raw):
        token = part.strip()
        if token:
            tick()
            items.append(_parse_item(token, allow_literal))
    return items


def _split_items(raw: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    in_string = False
    for char in raw:
        if in_string:
            buffer.append(char)
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            buffer.append(char)
        elif char == ",":
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer))
    return parts


def _parse_item(token: str, allow_literal: bool) -> LineItem:
    if token.startswith('"'):
        if not allow_literal:
            # A literal under L or A names no element: a sentinel item.
            return RefItem(SENTINEL_REF)
        text = token[1:]
        if text.endswith('"'):
            text = text[:-1]
        return LiteralItem(text)
    if "+" in token:
        refs: list[SegmentRef] = []
        for part in token.split("+"):
            ref = _parse_ref(part)
            if ref is None:
                return RefItem(SENTINEL_REF)
            refs.append(ref)
        return JoinItem(tuple(refs))
    if "-" in token:
        head, _, tail = token.partition("-")
        ref = _parse_ref(head)
        seg = _parse_seg(tail)
        if (
            ref is None
            or seg is None
            or seg < ref.seg
            or seg - ref.seg + 1 > MAX_SPAN_WIDTH
        ):
            # A cross-row range a span cannot express, a bad bound, a
            # descending span, or an expansion bomb.
            return RefItem(SENTINEL_REF)
        return SpanItem(row=ref.row, start=ref.seg, stop=seg)
    ref = _parse_ref(token)
    return RefItem(ref if ref is not None else SENTINEL_REF)


def _digits_to_int(digits: str) -> int | None:
    """The module's only ``int()``: ASCII digits, at most :data:`MAX_DIGITS`.

    ``None`` for anything else (empty, non-ASCII, or over-long), so a
    malformed number is a sentinel and never an exception.
    """
    if not digits or len(digits) > MAX_DIGITS:
        return None
    for char in digits:
        if char not in DIGITS:
            return None
    return int(digits)


def _parse_ref(token: str) -> SegmentRef | None:
    match = _RE_REF.match(token.strip())
    if match is None:
        return None
    row = _digits_to_int(match.group(1))
    seg = _digits_to_int(match.group(2))
    if row is None or seg is None:
        return None
    return SegmentRef(row, seg)


def _parse_seg(token: str) -> int | None:
    match = _RE_SEG.match(token.strip())
    if match is None:
        return None
    return _digits_to_int(match.group(0))
