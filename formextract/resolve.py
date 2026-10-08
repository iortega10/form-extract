"""Cold-path binding authoring: LLM call plus prompt/response archival.
Authors bindings; does not resolve values (that is replay's job)."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .layout import strip_marks
from .lines import (
    JoinItem,
    LineKind,
    LineStats,
    LiteralItem,
    SegmentRef,
    item_refs,
    parse_lines,
)
from .model import (
    AMBIGUITY_BETWEEN_OPTIONS,
    AMBIGUITY_COMPETING_OPTIONS,
    AMBIGUITY_CONVENTION_UNSUPPORTED,
    CHECKBOX_MARK_FOLLOWS_OPTION,
    CHECKBOX_MARK_PRECEDES_OPTION,
    AuthoredBy,
    BBox,
    BindingDraft,
    BindingProvenance,
    CheckboxConvention,
    ControlType,
    Element,
    ElementRef,
    Field,
    FieldAmbiguity,
    LayoutResult,
    LLMCall,
    MarkerClass,
    MarkerClassification,
    Option,
    Provenance,
    ProvenanceSource,
    Region,
    RegionType,
    RelationalAddress,
    ReviewReason,
)
from .rows import RowLattice, build_all_rows
from .schema import canonical_field, canonical_name, normalize, normalize_label

BINDING_OUTPUT_CONTRACT = """{"fields": [
  {"label": str, "control_type": "single_select|multi_select|bool|text",
   "options": [{"text": str, "selected": bool}],
   "answer": [str], "annotations": [str],
   "region_id": str,
   "source_elements": [{"region_id": str, "band_id": int, "segment_index": int}],
   "address": {"anchor_text": str, "column": int, "row_band": int, "offset_right": int},
   "confidence": number}]}"""

# 0.4.0's prompt asked for a per-field `address` object and explained it in a
# bullet. 0.5.0 makes that opt-in (include_address=False by default), which
# shortens every projection prompt, so the prompt hash changes and cached
# 0.4.0 calls must not be served.
# 0.6.0-L1b moves this to "4": a second output contract ("lines") ships beside
# the JSON one, so an instance key must name the version that produced it. The
# default json prompt and projection are byte-identical to 0.5.0, and the call
# cache keys on the prompt text, so an unchanged json prompt still hits cached
# 0.5.x calls even though the instance key moves once.
PROMPT_VERSION = "4"
GEOMETRIC_SELECTION_TOKEN = "marker_auto_select"
DECLARED_CONVENTION_TOKEN = "declared_checkbox_convention"

TRANSPORT_MAX_ATTEMPTS = 3
TRANSPORT_BACKOFF_SECONDS = 0.5
CALL_BUDGET_PER_CHUNK = 2


@dataclass
class LLMResponse:
    text: str
    model: str
    params: dict[str, Any]
    tokens: int | None = None
    latency_ms: int | None = None
    #: The provider's stop reason for this response (OpenAI ``length``,
    #: Anthropic ``max_tokens``, Gemini ``MAX_TOKENS``, ...). Defaulted and
    #: source-compatible: a client that does not fill it leaves it ``None``,
    #: which the lines contract reads as ``unknown``. Normalise it with
    #: ``lines.normalize_finish_reason``.
    finish_reason: str | None = None


class NonRetryable(Exception):
    """A request that can never succeed, so the attempt loop must not retry it.

    Raise this from ``LLMClient.complete`` (or wrap the provider's error in it)
    for a non-retryable response such as a 4xx. Raising an exception that merely
    *carries* a 4xx status (``status_code`` or ``code`` as an int, a digit
    string or an ``IntEnum``; also ``response.status_code`` for a
    requests/httpx-style error) has the same effect. Either way the call costs
    exactly one attempt, and the provider's own reason still reaches the record
    unchanged as ``transport failed: {exc}``. ``408`` and ``429`` retry as
    usual.
    """


def _guarded_attr(obj: Any, name: str) -> Any:
    """``getattr`` that is total: a hostile ``__getattr__`` never escapes."""
    try:
        return getattr(obj, name)
    except Exception:  # noqa: BLE001 - a broken attribute is simply absent
        return None


def _status_code_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and text.isdigit() and len(text) <= 3:
            return int(text)
        return None
    inner = _guarded_attr(value, "value")
    if isinstance(inner, int) and not isinstance(inner, bool):
        return int(inner)
    return None


def _http_status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status of a transport error; never raises."""
    for attr in ("status_code", "code"):
        code = _status_code_value(_guarded_attr(exc, attr))
        if code is not None:
            return code
    response = _guarded_attr(exc, "response")
    if response is not None:
        code = _status_code_value(_guarded_attr(response, "status_code"))
        if code is not None:
            return code
    return None


def _is_non_retryable(exc: BaseException) -> bool:
    if isinstance(exc, NonRetryable):
        return True
    code = _http_status_code(exc)
    return code is not None and 400 <= code < 500 and code not in (408, 429)


class LLMClient(Protocol):
    """The single call the pipeline makes per projection chunk.

    ``complete`` receives the whole prompt (layout projection plus the JSON
    output contract) and returns the model's raw text in ``LLMResponse.text``.
    The pipeline calls it once per authored tab, plus at most one repair call
    per tab, and never in an outer loop: one dense tab costs roughly 0.6k-2.7k
    input tokens and one output JSON object per field. Return the text
    unchanged; ``tokens``/``latency_ms`` are archived with the call and are the
    first place to look when a run feels slow. Use a fast non-reasoning model
    with reasoning/thinking switched off, ``temperature`` 0 and a modest
    ``max_tokens``. When ``PipelineConfig.chunk_workers > 1`` the client is
    called from several threads and must be thread-safe.

    Raise on a transport failure, and raise ``NonRetryable`` (or an exception
    carrying a 4xx ``status_code``/``code``) for a request that can never
    succeed: the attempt loop then makes exactly one call for it. Do not retry
    internally, and do not wrap the provider's message away - it ends up in the
    record's ``errors`` as ``transport failed: {exc}``. Fill
    ``LLMResponse.finish_reason`` with the provider's stop reason (it is
    optional and defaults to ``None``): the lines output contract reads it to
    tell a length cut from a clean stop.
    """

    def complete(self, prompt: str, *, model: str, params: dict[str, Any]) -> LLMResponse: ...


@dataclass
class ProjectionChunk:
    key: str
    text: str
    page: int | None = None


@dataclass
class ResolutionError:
    chunk_id: str
    tab: str
    reason: str


def project_chunks(
    layout: LayoutResult,
    elements: list[Element],
    tabs: list[str],
    *,
    non_answer_element_ids: set[str] | None = None,
    page_tabs: dict[int, str] | None = None,
) -> list[ProjectionChunk]:
    non_answer_ids = non_answer_element_ids or set()
    by_id = {e.element_id: e for e in elements}
    by_page: dict[int, list[Region]] = {}
    for region in layout.regions:
        by_page.setdefault(region.bbox.page, []).append(region)

    chunks: list[ProjectionChunk] = []
    for page in sorted(by_page):
        if page_tabs is not None:
            tab = page_tabs.get(page, f"page {page}")
        else:
            tab = tabs[page] if page < len(tabs) else f"page {page}"
        lines: list[str] = []
        for region in sorted(by_page[page], key=lambda r: (r.bbox.y0, r.bbox.x0)):
            lines.append(f"### {region.region_id} [{region.type.value}]")
            band_key = page * 1000 + region.column
            bands = layout.bands.get(band_key, [])
            for band_id in region.band_ids:
                if band_id >= len(bands):
                    continue
                segments: list[str] = []
                for i, eid in enumerate(bands[band_id]):
                    if eid not in by_id:
                        continue
                    text = by_id[eid].text
                    if eid in non_answer_ids:
                        segments.append(f"{i}={text} [annotation]")
                    else:
                        segments.append(f"{i}={text}")
                lines.append(f"- band {band_id}: " + " | ".join(segments))
            lines.append("")
        chunks.append(ProjectionChunk(key=tab, text="\n".join(lines).strip(), page=page))
    if not chunks and elements:
        lines = [f"- {e.text}" for e in sorted(elements, key=lambda e: (e.bbox.y0, e.bbox.x0))]
        chunks.append(
            ProjectionChunk(key=tabs[0] if tabs else "page 0", text="\n".join(lines), page=0)
        )
    return chunks


def _binding_contract(include_address: bool) -> str:
    if include_address:
        return BINDING_OUTPUT_CONTRACT
    return BINDING_OUTPUT_CONTRACT.replace(
        '   "address": {"anchor_text": str, "column": int, "row_band": int, "offset_right": int},\n',
        "",
    )


def build_prompt(chunk: ProjectionChunk, *, include_address: bool = False) -> str:
    contract = _binding_contract(include_address)
    address_bullet = (
        "- 'address' points at the anchor (left-column label text) and where the value "
        "lives relative to it: column index, band index within that column, and how many "
        "text blocks right of the anchor.\n"
        if include_address
        else ""
    )
    return (
        "You extract structured fields from one region group of a non-uniform "
        "form (labels on the left, controls on the right).\n"
        "Return ONLY JSON matching exactly this contract:\n"
        f"{contract}\n"
        "Rules:\n"
        "- A field is {label, control_type, options, answer, annotations}, not a label/value pair.\n"
        "- Marks ('X', a checkmark, or a checked box) are DECIDED BY THE RESOLVER, not by "
        "you. Label the control and list its options; you may return a selected flag, but "
        "the resolver ignores it for ambiguous marks and cross-checks it for unambiguous "
        "ones. A row of many short options is multi_select; Yes/No siblings are "
        "single_select.\n"
        "- A '...All' control that selects an entire option grid belongs to that grid's "
        "field: answer includes \"ALL\" and the grid options are merged into that field.\n"
        "- Sub-instructions (e.g. 'if not all please check those that apply') and notes "
        "(e.g. 'Confirm on vendor portal', parenthetical instructions) are annotations, "
        "never answers. One field per user-visible question.\n"
        "- Segments projected with a trailing `[annotation]` tag are in a caller-declared "
        "non-answer column: treat them as annotations only, never as options, values, "
        "answers or marks.\n"
        "- 'answer' holds the selected option texts (or the free text for text fields).\n"
        f"{address_bullet}"
        "- 'source_elements' lists the projection segments this field is built from: "
        "for each label/option/mark element, give its region_id, band_id and the "
        "segment index shown as the `i=` prefix on that band line.\n"
        "- 'confidence' is your calibrated probability that the grouping is right.\n\n"
        f"Layout projection for `{chunk.key}`:\n{chunk.text}"
    )


# ---------------------------------------------------------------------------
# 0.6.0-L1b: the lines contract
# (docs/design/0.6.0-line-contract-debate.md sections 3.1-3.4 and 4)
# ---------------------------------------------------------------------------

#: A row's projection tag, printed only when the row's chosen band is not a
#: plain FIELD_ROW (section 3.1). Tags are projection hints, not a grid kind.
_ROW_TAGS = {
    RegionType.GRID: "grid",
    RegionType.HEADER: "hdr",
    RegionType.PROSE: "prose",
}

#: The lines path makes exactly one call per chunk: the response is parsed, not
#: repaired (section 3.5), so CALL_BUDGET_PER_CHUNK does not apply to it.
LINES_CALL_BUDGET_PER_CHUNK = 1

#: The kind -> ``ControlType`` mapping (section 4). The model states the kind,
#: which is not recoverable from refs; the resolver derives everything else.
_KIND_CONTROL = {
    LineKind.SINGLE: ControlType.SINGLE_SELECT,
    LineKind.MULTI: ControlType.MULTI_SELECT,
    LineKind.BOOL: ControlType.BOOL,
    LineKind.TEXT: ControlType.TEXT,
}

#: The ``ElementRef`` a ref that resolves to nothing becomes: it fails the
#: resolver's bounds check and lands in ``unresolved_source_refs``, exactly as
#: an unresolvable JSON ref does today (section 3.3).
SENTINEL_ELEMENT_REF = ElementRef(region_id="", band_id=-1, segment_index=-1)


@dataclass
class Disposition:
    """A ``hdr``/``note``/``skip`` line: a lattice row addressed as no field.

    Recorded only (section 5.1). L2a consumes these to score coverage; L1b only
    returns them so a run can report the units a response dispositioned.
    """

    kind: LineKind
    rows: tuple[int, ...]
    line_no: int


def _anchor_column(layout: LayoutResult) -> int | None:
    """The single densest FIELD_ROW column, or ``None`` when there are none.

    ``layout.anchors`` already restricts to FIELD_ROW bands with a non-empty
    label; the column most of them sit in is the anchor column the row tag is
    read from first (section 3.1).
    """
    counts: dict[int, int] = {}
    for anchor in layout.anchors:
        column = anchor.relative_address.column
        counts[column] = counts.get(column, 0) + 1
    if not counts:
        return None
    return max(sorted(counts), key=lambda column: counts[column])


def _row_tag(row, owner, anchor_column) -> str:
    """The ``[hdr]``/``[grid]``/``[prose]`` tag for a row, or ``""``.

    Taken from the row's band in the anchor column when it has one, else the
    row's leftmost band; printed only when that band's region is not a plain
    field row (section 3.1).
    """
    chosen = None
    if anchor_column is not None:
        for column, band_id in row.entries:
            if column == anchor_column:
                chosen = (column, band_id)
                break
    if chosen is None and row.entries:
        chosen = row.entries[0]
    if chosen is None:
        return ""
    region = owner.get(chosen)
    if region is None:
        return ""
    tag = _ROW_TAGS.get(region.type)
    return f" [{tag}]" if tag else ""


def _is_shaded(element: Element) -> bool:
    """Whether an element's cell carries a real (non-default) fill colour.

    ``ingest/xlsx.py`` sets ``fill=_argb_to_int(start_color)``: an unfilled cell
    (openpyxl's default ``00000000``) yields ``0``, a solid ``RRGGBB`` fill its
    integer, and a theme/indexed/auto colour ``None`` (``_argb_to_int`` needs a
    6/8-char ``rgb`` string). So the default is ``0``/``None`` and any other
    value is a fill an author chose. The style cue reads it as evidence, never a
    rule (see the spec's risk note).
    """
    return element.fill is not None and element.fill != 0


def _project_page_rows(
    layout,
    by_id,
    lattice,
    page,
    tabs,
    non_answer_ids,
    page_tabs,
    *,
    row_tags=True,
    style_tags=False,
):
    """One projection chunk for one page: a line per lattice row (section 3.1).

    ``row_tags`` is the projection option a prompt variant may turn off (L3a):
    with it off the ``[hdr]/[grid]/[prose]`` hint is simply not printed, and
    nothing else about the line changes, so a ``row.seg`` coordinate means the
    same thing under either projection.

    ``style_tags`` (default off) is the same kind of projection option: with it
    on, a segment whose element has a real fill is printed ``k=text [shaded]``.
    Only that suffix is added -- the row id, the segment index ``k`` and the
    ``[annotation]`` suffix are unchanged, so a response written against one
    projection resolves against the other. A cell that is both is
    ``[annotation] [shaded]``.
    """
    owner: dict[tuple[int, int], Region] = {}
    for region in layout.regions:
        if region.bbox.page != page:
            continue
        for band_id in region.band_ids:
            owner[(region.column, band_id)] = region
    anchor_column = _anchor_column(layout)

    lines: list[str] = []
    for row in lattice.rows:
        segments: list[str] = []
        for k, element_id in enumerate(row.elements):
            element = by_id.get(element_id)
            if element is None:
                continue
            suffix = ""
            if element_id in non_answer_ids:
                suffix += " [annotation]"
            if style_tags and _is_shaded(element):
                suffix += " [shaded]"
            segments.append(f"{k}={element.text}{suffix}")
        if not segments:
            continue
        tag = _row_tag(row, owner, anchor_column) if row_tags else ""
        lines.append(f"{row.index}{tag}: " + " | ".join(segments))
    if not lines:
        return None
    if page_tabs is not None:
        tab = page_tabs.get(page, f"page {page}")
    elif page < len(tabs):
        tab = tabs[page]
    else:
        tab = f"page {page}"
    return ProjectionChunk(key=tab, text="\n".join(lines).strip(), page=page)


def project_lines_chunks(
    layout: LayoutResult,
    elements: list[Element],
    tabs: list[str],
    *,
    lattices: dict[int, RowLattice] | None = None,
    non_answer_element_ids: set[str] | None = None,
    page_tabs: dict[int, str] | None = None,
    row_tags: bool = True,
    style_tags: bool = False,
) -> list[ProjectionChunk]:
    """0.6.0 lines projection: one line per page-global lattice ROW.

    A new function, not a change to :func:`project_chunks`: the JSON projection
    (region/band blocks) stays byte-identical to 0.5.0. ``lattices`` is the
    page-global row lattice per page (``rows.build_all_rows``), built before any
    chunk split; when omitted it is built here from ``layout``. No region id,
    band id or alias appears in the model-visible text - only row indices,
    segment indices and the ``[hdr]/[grid]/[prose]``/``[annotation]`` hints.

    ``row_tags=False`` drops the ``[hdr]/[grid]/[prose]`` row tags (L3a's
    ``fix2_notags`` variant): the debate's tag A/B, a projection option rather
    than a prompt edit. Only the tag text is dropped - the row id, the segments
    and the ``[annotation]`` segment tag are the same under both projections, so
    a response written against one is valid against the other.

    ``style_tags=False`` (the default) prints exactly today's lines; with it on
    a filled cell gains a trailing ``[shaded]`` (:func:`_is_shaded`), the L1b
    style cue's projection option. It changes no row id and no segment index,
    so the two projections resolve a response identically.
    """
    non_answer_ids = set(non_answer_element_ids or ())
    by_id = {e.element_id: e for e in elements}
    if lattices is None:
        lattices = build_all_rows(layout, by_id)

    chunks: list[ProjectionChunk] = []
    for page in sorted(lattices):
        chunk = _project_page_rows(
            layout,
            by_id,
            lattices[page],
            page,
            tabs,
            non_answer_ids,
            page_tabs,
            row_tags=row_tags,
            style_tags=style_tags,
        )
        if chunk is not None:
            chunks.append(chunk)
    if not chunks and elements:
        lines = [
            f"- {e.text}" for e in sorted(elements, key=lambda e: (e.bbox.y0, e.bbox.x0))
        ]
        chunks.append(
            ProjectionChunk(key=tabs[0] if tabs else "page 0", text="\n".join(lines), page=0)
        )
    return chunks


def _lines_prompt_base(chunk: ProjectionChunk) -> str:
    """The ``base`` variant: today's prompt, byte for byte (sections 3.2-3.4).

    Kept short on purpose: it is input tokens on every call. The worked examples
    use invented vocabulary unrelated to the gold fixtures.
    """
    return (
        "You bind ONE form tab. The tab is drawn as numbered ROWS; every cell in "
        "a row is a numbered segment, written `row.seg` (segments count from 0, "
        "left to right).\n"
        "Write ONE line per FIELD, then a single line `end`, then stop. Nothing "
        "else: no prose, no JSON, no markdown fences, and never copy any cell "
        "text.\n"
        "A line is a kind, then `key=items` groups. Kinds: `single` (one "
        "choice), `multi` (several), `bool` (a yes/no value), `text` (free "
        "text), `hdr` (a heading), `note` (a note), `skip` (not a field).\n"
        "Keys: `L=` the label cells (required), `O=` every option cell, `A=` the "
        "answer evidence, `N=` notes.\n"
        "- A comma JOINs cells in `L=`, `A=` and `N=` (`L=50.0,51.0`), but "
        "SEPARATES options in `O=`. A `+` joins several cells into ONE option: "
        "`O=12.2+12.3`.\n"
        "- `O=` must list EVERY option cell, never abbreviated. `a.b-c` is a "
        "within-row span meaning one option per cell: `multi L=14.0 O=14.1-12`.\n"
        "- No cross-row ranges. A control stacked down rows is a comma list: "
        "`multi L=30.0 O=30.2,31.2,32.2`.\n"
        "- Marks (`X`, a tick) are decided by the resolver: cite the OPTION "
        "cells, never the mark cells. Give `A=` only where no mark decides (a "
        "typed value, a bold or coloured option); for `text`, `A=` holds the "
        "value cells.\n"
        "- `N=` is a note or sub-instruction that belongs to the field.\n"
        "- A quoted literal `\"Yes No\"` is allowed under `O=` or `N=` only, for "
        "one cell holding several options; never under `L=` or `A=`.\n"
        "- A row that is not a field is `hdr L=3.0`, `note L=5.0` or "
        "`skip L=6.0`.\n"
        "Examples (invented rows):\n"
        "  single L=12.0 O=12.2,12.4\n"
        "  multi L=14.0 O=14.1-12\n"
        "  multi L=20.0 N=19.0 O=21.0-9,22.0-9\n"
        "  single L=40.0 O=40.2,40.4\n"
        "  single L=40.5 O=40.7,40.9\n"
        "  text L=50.0,51.0 A=50.2\n"
        "Finish with the single line `end`.\n\n"
        f"Rows of `{chunk.key}`:\n{chunk.text}"
    )


#: The worked examples ``fix2`` adds for the structures the reviewer measured at
#: zero on the dev gold. Invented cells only: no example line is a line of the
#: gold's canned perfect responses, and no word here is in a dev or held-out gold
#: word list (a test asserts both against gold built in a temp dir).
_LINES_FIX2_WORKED_EXAMPLES = (
    "The shapes that scored zero (invented rows):\n"
    "- stacked: the label alone on its row, one option cell per row below it, "
    "ONE `O=` comma list:\n"
    "  multi L=30.0 O=31.0,32.0,33.0\n"
    "- two-row label with a typed answer:\n"
    "  text L=1.0,2.0 A=1.2\n"
    "- grid: the label row is the field's own row, never a `hdr` line:\n"
    "  multi L=20.0 O=21.0-9,22.0-9\n"
    "- matrix: the options are the HEADER cells far above:\n"
    "  single L=9.0 O=3.1,3.2 A=9.1\n"
    "- side by side: two fields, TWO lines:\n"
    "  single L=40.0 O=40.2,40.4\n"
    "  single L=40.5 O=40.7,40.9\n"
)


#: The ONE sentence ``fix2_style`` adds to ``fix2``: it defines the ``[shaded]``
#: projection tag (:func:`_is_shaded`). Deliberately no double quote and not an
#: example line, so the prompt's disjointness checks (no copyable string, no
#: gold word) stay satisfied; the tag is evidence, not a rule, because real forms
#: often invert the synthetic gold's convention and this variant must not be
#: promoted to the default prompt on dev-gold numbers alone.
_LINES_STYLE_SENTENCE = (
    "Cells printed with a trailing `[shaded]` tag carry a fill colour: options "
    "and checkbox cells are often shaded, typed answers often are not; it is "
    "evidence, not a rule.\n"
)


def _lines_prompt_fix_body(
    chunk: ProjectionChunk, worked_examples: str, *, style_sentence: str = ""
) -> str:
    """``fix1``/``fix2``: the base rules retargeted at the observed failures.

    The contaminating literal example is gone, so no string in the prompt can be
    copied into a field (F2); the kind is tied to the ANSWER rather than the
    label's punctuation (F6); a row is a field XOR a disposition (F3/F7); a
    two-label row is two fields (F5); marks are never cited (F8). ``fix2`` adds
    the worked examples for the structures that scored zero; ``style_sentence``
    lets a variant inject the ``[shaded]`` definition without moving ``fix1``'s
    or ``fix2``'s bytes.
    """
    return (
        "You bind ONE form tab. The tab is drawn as numbered ROWS; every cell in "
        "a row is a numbered segment, written `row.seg` (segments count from 0, "
        "left to right).\n"
        "Write ONE line per FIELD, then a line `end`, then stop: no prose, no "
        "JSON, no markdown fences, and never copy any cell text.\n"
        "A line is a kind, then `key=items` groups. Kinds: `single` (one option "
        "is chosen), `multi` (several may be chosen), `bool` (a yes/no value), "
        "`text` (free text), `hdr` (a heading), `note` (a note), `skip` (not a "
        "field).\n"
        "The kind follows the ANSWER, not the label: a typed value or a number "
        "is `text` even when the label ends in `?`; `bool` only for a yes/no "
        "value.\n"
        "Keys: `L=` the label cells (required, in reading order), `O=` every "
        "option cell, `A=` the answer evidence, `N=` notes.\n"
        "- A row is EITHER a field line or a disposition line, never both; a row "
        "holding TWO labels is TWO fields, the second label starting a second "
        "line on the same row.\n"
        "- A comma JOINs cells in `L=`, `A=` and `N=` (`L=50.0,51.0`) but "
        "SEPARATES options in `O=`; a `+` joins cells into ONE option "
        "(`O=12.2+12.3`).\n"
        "- `O=` lists EVERY option cell, never abbreviated. `a.b-c` is a "
        "within-row span, one option per cell: `multi L=14.0 O=14.1-12`.\n"
        "- No cross-row ranges; a control stacked down rows is ONE comma list of "
        "the option cells below its label.\n"
        "- Marks (`X`, a tick) are decided by the resolver: never cite a mark "
        "cell in `L=`, `O=` or `A=`, and give `A=` only where no mark decides (a "
        "typed value, a bold or coloured option, or a `text` value cell).\n"
        + style_sentence
        + "- `N=` is a note that belongs to the field.\n"
        "- A quoted literal may stand for one cell holding several options; it "
        "is allowed under `O=` or `N=` only.\n"
        "- A row that is not a field is `hdr L=3.0`, `note L=5.0` or "
        "`skip L=6.0`.\n"
        + worked_examples
        + "Examples (invented rows):\n"
        "  single L=12.0 O=12.2,12.4\n"
        "  multi L=14.0 O=14.1-12\n"
        "  multi L=20.0 N=19.0 O=21.0-9,22.0-9\n"
        "  single L=40.0 O=40.2,40.4\n"
        "  single L=40.5 O=40.7,40.9\n"
        "  text L=50.0,51.0 A=50.2\n"
        "Finish with the single line `end`.\n\n"
        f"Rows of `{chunk.key}`:\n{chunk.text}"
    )


def _lines_prompt_fix1(chunk: ProjectionChunk) -> str:
    """The ``fix1`` variant: targeted wording and no worked examples."""
    return _lines_prompt_fix_body(chunk, "")


def _lines_prompt_fix2(chunk: ProjectionChunk) -> str:
    """``fix2``: ``fix1`` plus worked examples for the shapes that scored zero."""
    return _lines_prompt_fix_body(chunk, _LINES_FIX2_WORKED_EXAMPLES)


def _lines_prompt_fix2_style(chunk: ProjectionChunk) -> str:
    """``fix2_style``: ``fix2``'s text plus one sentence defining ``[shaded]``.

    Ships with the ``style_tags`` projection on (see ``LINES_VARIANT_STYLE_TAGS``),
    so the model sees the ``[shaded]`` suffix the sentence explains.
    """
    return _lines_prompt_fix_body(
        chunk, _LINES_FIX2_WORKED_EXAMPLES, style_sentence=_LINES_STYLE_SENTENCE
    )


#: The lines-contract prompt variants, by name. This is the closed vocabulary
#: ``PipelineConfig.prompt_variant`` is validated against, and every value is a
#: PURE function of the chunk it is handed: no clock, no counter, no module
#: state, so two calls with the same chunk are byte-identical. ``base`` is
#: today's prompt byte for byte; the others are this turn's experiments, tuned on
#: the dev gold set only, and a revision is always a NEW name (a variant that has
#: held-out numbers is frozen).
LINES_PROMPT_VARIANTS: dict[str, Callable[[ProjectionChunk], str]] = {
    "base": _lines_prompt_base,
    "fix1": _lines_prompt_fix1,
    "fix2": _lines_prompt_fix2,
    "fix2_notags": _lines_prompt_fix2,
    "fix2_style": _lines_prompt_fix2_style,
}

#: The projection option each variant needs. ``fix2_notags`` is ``fix2``'s text
#: over a tag-free projection (the debate's tag A/B), so its prompt function is
#: ``fix2``'s; every other variant projects with the row tags on.
LINES_VARIANT_ROW_TAGS: dict[str, bool] = {
    "base": True,
    "fix1": True,
    "fix2": True,
    "fix2_notags": False,
    "fix2_style": True,
}

#: The style-tag projection option each variant needs (L1b). Only ``fix2_style``
#: turns ``[shaded]`` on; every other variant projects exactly today's lines, so
#: ``style_tags=False`` is the default on every shipped path.
LINES_VARIANT_STYLE_TAGS: dict[str, bool] = {
    "base": False,
    "fix1": False,
    "fix2": False,
    "fix2_notags": False,
    "fix2_style": True,
}


def lines_variant_row_tags(variant: str) -> bool:
    """Whether the projection prints ``[hdr]/[grid]/[prose]`` for ``variant``."""
    return LINES_VARIANT_ROW_TAGS[variant]


def lines_variant_style_tags(variant: str) -> bool:
    """Whether the projection prints ``[shaded]`` for ``variant``."""
    return LINES_VARIANT_STYLE_TAGS[variant]


def build_lines_prompt(chunk: ProjectionChunk, variant: str = "base") -> str:
    """The prompt for the lines contract, in the named variant (sections 3.2-3.4).

    ``base`` is the 0.6.0-L1b prompt byte for byte, so the default path is
    unchanged and ``PROMPT_VERSION`` is not bumped by adding variants. The
    variant is part of the call-cache key through this text and part of the
    instance key through ``PipelineConfig.prompt_variant``, so a name always
    travels with the response it produced.
    """
    if variant not in LINES_PROMPT_VARIANTS:
        raise ValueError(
            f"prompt_variant must be one of {tuple(LINES_PROMPT_VARIANTS)!r}, "
            f"got {variant!r}"
        )
    return LINES_PROMPT_VARIANTS[variant](chunk)


def _resolve_ref(lattice, ref: SegmentRef) -> tuple[str, int, int] | None:
    """``(region_id, band_id, segment_index)`` for a ref, or ``None`` if it is bad."""
    if lattice is None or ref.is_sentinel:
        return None
    if ref.row < 0 or ref.row >= len(lattice.rows):
        return None
    return lattice.ref_for(lattice.rows[ref.row], ref.seg)


def _ref_element_id(lattice, ref: SegmentRef) -> str | None:
    if lattice is None or ref.is_sentinel:
        return None
    if ref.row < 0 or ref.row >= len(lattice.rows):
        return None
    return lattice.segment(lattice.rows[ref.row], ref.seg)


def _ref_to_element_ref(lattice, ref: SegmentRef) -> ElementRef:
    """Translate a projection ref to today's coordinates, or the sentinel."""
    element_id = _ref_element_id(lattice, ref)
    if element_id is None:
        return SENTINEL_ELEMENT_REF
    resolved = lattice.refs_by_element.get(element_id)
    if resolved is None:
        return SENTINEL_ELEMENT_REF
    return ElementRef(region_id=resolved[0], band_id=resolved[1], segment_index=resolved[2])


def _element_ref_for(lattice, element_id: str) -> ElementRef | None:
    if lattice is None:
        return None
    resolved = lattice.refs_by_element.get(element_id)
    if resolved is None:
        return None
    return ElementRef(region_id=resolved[0], band_id=resolved[1], segment_index=resolved[2])


def _element_text(elements_by_id, element_id: str | None) -> str:
    element = elements_by_id.get(element_id) if element_id else None
    if element is None:
        return ""
    text = getattr(element, "text", "")
    return text if isinstance(text, str) else ""


def _ordered_refs(items) -> list[SegmentRef]:
    """Every ref an item group names, in reading order (row, seg).

    A sentinel sorts last so it can never become a field's anchor region.
    """
    refs = [ref for item in items for ref in item_refs(item)]
    refs.sort(key=lambda ref: (ref.is_sentinel, ref.row, ref.seg))
    return refs


def _record_rows(lattice, record) -> tuple[int, ...]:
    """The lattice rows a disposition's ``L`` refs address (valid ones only)."""
    rows: set[int] = set()
    for ref in _ordered_refs(record.label):
        if ref.is_sentinel or lattice is None:
            continue
        if 0 <= ref.row < len(lattice.rows):
            rows.add(ref.row)
    return tuple(sorted(rows))


def _draft_from_record(
    record, lattice, layout, elements_by_id, non_answer_ids, now
) -> BindingDraft:
    """Build one ``BindingDraft`` from a field record, per section 4."""
    label_refs = _ordered_refs(record.label)

    label_parts: list[str] = []
    l_ids: list[str] = []
    for ref in label_refs:
        element_id = _ref_element_id(lattice, ref)
        if element_id is None:
            continue
        l_ids.append(element_id)
        text = strip_marks(_element_text(elements_by_id, element_id))
        if text:
            label_parts.append(text)
    label = " ".join(label_parts)

    options: list[Option] = []
    option_of_element: dict[str, Option] = {}
    cited_ids: set[str] = set(l_ids)

    for item in record.options:
        if isinstance(item, LiteralItem):
            options.append(Option(text=item.text))
            continue
        element_ids = [
            element_id
            for element_id in (
                _ref_element_id(lattice, ref) for ref in item_refs(item)
            )
            if element_id is not None
        ]
        cited_ids.update(element_ids)
        if isinstance(item, JoinItem):
            parts = [
                strip_marks(_element_text(elements_by_id, element_id))
                for element_id in element_ids
            ]
            joined = " ".join(part for part in parts if part)
            if joined:
                option = Option(text=joined)
                options.append(option)
                for element_id in element_ids:
                    option_of_element[element_id] = option
            continue
        for element_id in element_ids:
            text = strip_marks(_element_text(elements_by_id, element_id))
            if not text:
                # An O element whose stripped text is empty is a MARK, not an
                # option (section 3.2).
                continue
            option = Option(text=text)
            options.append(option)
            option_of_element[element_id] = option

    annotations: list[str] = []
    for item in record.notes:
        if isinstance(item, LiteralItem):
            annotations.append(item.text)
            continue
        for ref in item_refs(item):
            element_id = _ref_element_id(lattice, ref)
            if element_id is None:
                continue
            annotations.append(_element_text(elements_by_id, element_id))

    answer_items = _ordered_refs(record.answer)
    answer_element_ids: list[str] = []
    for ref in answer_items:
        element_id = _ref_element_id(lattice, ref)
        if element_id is not None:
            answer_element_ids.append(element_id)
    cited_ids.update(answer_element_ids)

    answer_refs: tuple[ElementRef, ...] = ()
    if record.kind is LineKind.TEXT:
        texts = [
            strip_marks(_element_text(elements_by_id, element_id))
            for element_id in answer_element_ids
        ]
        joined = " ".join(text for text in texts if text)
        answers = [joined] if joined else []
        answer_refs = tuple(_ref_to_element_ref(lattice, ref) for ref in answer_items)
    else:
        answers = []
        for element_id in answer_element_ids:
            option = option_of_element.get(element_id)
            if option is not None:
                # An A element inside the option set is the resolver's second
                # opinion: it selects that option when no glyph decides, and it
                # is cross-checked against a geometric decision (section 4).
                option.selected = True
                continue
            text = strip_marks(_element_text(elements_by_id, element_id))
            if text:
                answers.append(text)

    region_id: str | None = None
    for ref in label_refs:
        resolved = _resolve_ref(lattice, ref)
        if resolved is not None:
            region_id = resolved[0]
            break

    source_refs: list[ElementRef] = [
        _ref_to_element_ref(lattice, ref) for ref in label_refs
    ]
    for group in (record.options, record.answer, record.notes):
        for item in group:
            for ref in item_refs(item):
                source_refs.append(_ref_to_element_ref(lattice, ref))

    if layout is not None:
        source_refs.extend(
            _injected_marker_refs(
                cited_ids, layout, lattice, non_answer_ids
            )
        )

    return BindingDraft(
        draft_id=f"d-{uuid.uuid4().hex[:12]}",
        label=label,
        control_type=_KIND_CONTROL[record.kind],
        bbox=BBox(),
        options=options,
        answers=answers,
        annotations=annotations,
        region_id=region_id,
        source_refs=source_refs,
        answer_refs=answer_refs,
        confidence=None,
        review_reason=ReviewReason.AMBIGUOUS_ROLE if record.review else None,
        provenance=BindingProvenance(
            authored_by=AuthoredBy.LLM,
            authored_at=now,
            llm_call_ref=None,
        ),
    )


def _injected_marker_refs(
    cited_ids: set[str],
    layout: LayoutResult,
    lattice,
    non_answer_ids: set[str],
) -> list[ElementRef]:
    """The marker refs a field's L/O/A cells attach (section 4).

    A marker attaches to every field whose L/O/A element set contains the marker
    itself or any element of a cell that is one of the marker's candidate ids
    (both clauses exclude non-answer ids). One element is one cell in this
    codebase, so the second clause reads the candidate id itself; the phrasing
    is kept for a future cell model. Injected markers let
    ``_geometric_mark_decision`` see a mark the model never cited (the model
    cites option cells, not mark cells). A BETWEEN or COMPETING marker shared by
    two side-by-side fields attaches to both and becomes ``AMBIGUOUS_MARK`` on
    each: over-flag, never silent.
    """
    visible = cited_ids - non_answer_ids
    if not visible:
        return []
    injected: list[ElementRef] = []
    for mc in layout.marker_classes:
        if mc.marker_element_id in non_answer_ids:
            continue
        candidates = {
            c for c in mc.candidate_element_ids if c not in non_answer_ids
        }
        if mc.marker_element_id not in visible and not (visible & candidates):
            continue
        ref = _element_ref_for(lattice, mc.marker_element_id)
        if ref is not None:
            injected.append(ref)
    return injected


def parse_lines_response(
    chunk: ProjectionChunk,
    lattice: RowLattice | None,
    response: LLMResponse,
    *,
    layout: LayoutResult | None = None,
    elements_by_id: dict[str, Element] | None = None,
    non_answer_element_ids: set[str] | None = None,
) -> tuple[list[BindingDraft], list[Disposition], list[str], LineStats]:
    """Turn one lines response into drafts, dispositions, errors and stats.

    ``parse_lines`` is total; every ref is then resolved through the lattice. A
    ``SENTINEL_REF``, an out-of-range row or segment, or a span that leaves its
    row becomes :data:`SENTINEL_ELEMENT_REF`, which the resolver's bounds check
    rejects into ``unresolved_source_refs`` with ``UNRESOLVED_REFERENCE`` exactly
    as an unresolvable JSON ref does. Never raises on any response.
    """
    parsed = parse_lines(response.text, finish_reason=response.finish_reason)
    elements_by_id = elements_by_id or {}
    non_answer_ids = set(non_answer_element_ids or ())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    drafts: list[BindingDraft] = []
    dispositions: list[Disposition] = []
    for record in parsed.records:
        if record.kind not in _KIND_CONTROL:
            dispositions.append(
                Disposition(
                    kind=record.kind,
                    rows=_record_rows(lattice, record),
                    line_no=record.line_no,
                )
            )
            continue
        if lattice is None:
            continue
        drafts.append(
            _draft_from_record(
                record,
                lattice,
                layout,
                elements_by_id,
                non_answer_ids,
                now,
            )
        )
    return drafts, dispositions, list(parsed.errors), parsed.stats


def _lines_response_ok(text: str) -> bool:
    """Whether a lines response is one this path indexes in the call cache.

    A response is indexed only when it parsed with no line errors, was not cut
    and was not empty (the analogue of the JSON field-error rule, section 5.3).
    """
    parsed = parse_lines(text)
    return (
        not parsed.errors
        and not parsed.truncated
        and not parsed.stats.empty_response
    )


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in LLM response")
    return json.loads(text[start : end + 1])


TRUNCATION_ERROR_PREFIX = "response truncated after "


def _scan_unterminated_fields(text: str) -> tuple[list[str], bool]:
    """One linear, string/escape-aware pass over a raw model response.

    Returns ``(complete_objects, unterminated)``. ``complete_objects`` are the
    raw substrings of the top-level objects in the ``fields`` array that closed
    before the end of input; ``unterminated`` is True when the response ends
    with a brace/bracket still open or inside a string. Iterative, no
    recursion, O(len(text)) even on adversarial input, and never raises.
    """
    start = text.find("{")
    if start == -1:
        return [], False
    key = text.find('"fields"', start)
    array_at = text.find("[", key) if key != -1 else -1

    depth = 0
    in_string = False
    escape = False
    array_depth: int | None = None
    obj_start: int | None = None
    objects: list[str] = []

    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{" or ch == "[":
            if i == array_at:
                array_depth = depth + 1
            depth += 1
            if ch == "{" and array_depth is not None and depth == array_depth + 1:
                obj_start = i
        elif ch == "}" or ch == "]":
            if (
                ch == "}"
                and obj_start is not None
                and array_depth is not None
                and depth == array_depth + 1
            ):
                objects.append(text[obj_start : i + 1])
                obj_start = None
            depth -= 1
            if array_depth is not None and depth < array_depth:
                array_depth = None
                obj_start = None
        i += 1

    return objects, (depth > 0 or in_string)


def _salvage_truncated_response(
    text: str, *, include_address: bool
) -> tuple[list[BindingDraft], list[str]] | None:
    """Parse every complete field object a length-truncated response finished.

    Returns ``(drafts, errors)`` when the response is unterminated, else
    ``None`` so the caller can re-raise: a balanced-but-invalid response still
    goes to the repair call, unchanged. The one added error names the cut, so
    the record reads PARTIAL; the caller archives the response unindexed.
    """
    raw_objects, unterminated = _scan_unterminated_fields(text)
    if not unterminated:
        return None
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    drafts: list[BindingDraft] = []
    errors: list[str] = []
    for idx, raw in enumerate(raw_objects):
        try:
            item = json.loads(raw)
            drafts.append(_parse_one_draft(item, now, include_address=include_address))
        except Exception as exc:  # noqa: BLE001 - a salvaged field may still be bad
            errors.append(f"field {idx}: {exc}")
    errors.append(f"{TRUNCATION_ERROR_PREFIX}{len(raw_objects)} fields")
    return drafts, errors


def parse_drafts(text: str, *, include_address: bool = True) -> list[BindingDraft]:
    drafts, _ = parse_drafts_with_errors(text, include_address=include_address)
    return drafts


def parse_drafts_with_errors(
    text: str, *, include_address: bool = True
) -> tuple[list[BindingDraft], list[str]]:
    try:
        data = _extract_json(text)
    except ValueError:
        salvaged = _salvage_truncated_response(text, include_address=include_address)
        if salvaged is None:
            raise
        return salvaged
    if not isinstance(data, dict):
        raise ValueError("LLM response is not a JSON object")
    drafts: list[BindingDraft] = []
    errors: list[str] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    raw_fields = data.get("fields", [])
    if not isinstance(raw_fields, list):
        raise ValueError("'fields' is not a list")
    for idx, item in enumerate(raw_fields):
        try:
            drafts.append(_parse_one_draft(item, now, include_address=include_address))
        except Exception as exc:  # noqa: BLE001 - one bad field must not drop the chunk
            errors.append(f"field {idx}: {exc}")
    return drafts, errors


def _parse_one_draft(item: dict, now: str, *, include_address: bool = True) -> BindingDraft:
    control_type = ControlType(item["control_type"])
    options = [
        Option(
            text=o["text"],
            selected=o.get("selected"),
        )
        for o in item.get("options", [])
    ]
    address = None
    if include_address and item.get("address"):
        a = item["address"]
        address = RelationalAddress(
            anchor_id=str(a.get("anchor_text", "")),
            column=a.get("column"),
            row_band=a.get("row_band"),
            offset_right=int(a.get("offset_right", 0)),
        )
    source_refs = [
        ElementRef(
            region_id=str(ref.get("region_id", "")),
            band_id=int(ref["band_id"]),
            segment_index=int(ref["segment_index"]),
        )
        for ref in item.get("source_elements", [])
    ]
    return BindingDraft(
        draft_id=f"d-{uuid.uuid4().hex[:12]}",
        label=item["label"],
        control_type=control_type,
        bbox=BBox(),  # no layout context at parse time; author_drafts
        # fills the matched region's bbox in when region_id matches
        options=options,
        answers=list(item.get("answer", [])),
        annotations=list(item.get("annotations", [])),
        region_id=item.get("region_id"),
        source_refs=source_refs,
        address=address,
        confidence=item.get("confidence"),
        provenance=BindingProvenance(
            authored_by=AuthoredBy.LLM,
            authored_at=now,
            llm_call_ref=None,
        ),
    )


def _read_call_response(store, call: LLMCall) -> str:
    return (store.root / call.response_ref).read_text(encoding="utf-8")


def _latency_ms(response: LLMResponse, started: float) -> int:
    if response.latency_ms is not None:
        return response.latency_ms
    return int((time.monotonic() - started) * 1000)


def _complete_with_retries(
    llm_client: LLMClient,
    prompt: str,
    *,
    model: str,
    params: dict[str, Any],
    attempts: int,
    backoff_seconds: float,
) -> LLMResponse:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return llm_client.complete(prompt, model=model, params=params)
        except Exception as exc:  # noqa: BLE001 - network/5xx/timeout transport errors
            last_exc = exc
            if _is_non_retryable(exc):
                # A 4xx (other than 408/429) or an explicit NonRetryable can
                # never succeed: one call, no backoff. The reason is not wrapped
                # away - the caller reports `transport failed: {exc}`.
                break
            if attempt + 1 < attempts and backoff_seconds:
                time.sleep(backoff_seconds * (2**attempt))
    assert last_exc is not None
    raise last_exc


class _Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.remaining = limit
        self.lock = threading.Lock()

    def take(self) -> bool:
        with self.lock:
            if self.remaining <= 0:
                return False
            self.remaining -= 1
            return True


def author_drafts(
    chunks: list[ProjectionChunk],
    llm_client: LLMClient,
    *,
    model: str,
    params: dict[str, Any],
    store,
    regions: list[Region] | None = None,
    purpose: str = "cold_binding",
    transport_max_attempts: int = TRANSPORT_MAX_ATTEMPTS,
    backoff_seconds: float = TRANSPORT_BACKOFF_SECONDS,
    call_budget: int | None = None,
    max_workers: int = 1,
    include_address: bool = False,
    force: bool = False,
    output_contract: str = "json",
    prompt_variant: str = "base",
    layout: LayoutResult | None = None,
    elements_by_id: dict[str, Element] | None = None,
    non_answer_element_ids: set[str] | None = None,
    dispositions_out: list[Disposition] | None = None,
) -> tuple[list[BindingDraft], list[LLMCall], list[ResolutionError]]:
    region_bbox = {r.region_id: r.bbox for r in (regions or [])}
    lines_mode = output_contract == "lines"
    per_chunk_budget = (
        LINES_CALL_BUDGET_PER_CHUNK if lines_mode else CALL_BUDGET_PER_CHUNK
    )
    budget = _Budget(
        call_budget if call_budget is not None else per_chunk_budget * len(chunks)
    )
    elements_by_id = elements_by_id or {}
    non_answer_ids = set(non_answer_element_ids or ())
    lattices: dict[int, RowLattice] = {}
    if lines_mode and layout is not None:
        lattices = build_all_rows(layout, elements_by_id)

    def resolve_prompt(
        prompt: str,
        validate,
    ) -> tuple[LLMResponse | None, list[LLMCall], str | None, bool]:
        """Return (response, calls, transport_error, fresh).

        ``fresh`` is True only when ``response`` is a brand-new model response
        the caller must archive (with an index decision made by the caller after
        parsing). A cached response is returned with its archived ``LLMCall``.
        ``validate`` decides whether a cached response still parses (the json
        path keeps ``parse_drafts_with_errors``; the lines path checks that it
        parsed cleanly); a cached response that no longer parses is treated as a
        cache miss and falls through to a fresh call. With ``force`` the
        call-cache lookup is skipped entirely, so the model is called even for
        an unchanged prompt.
        """
        cached = None
        if not force:
            cached = store.find_llm_call(
                purpose=purpose, model=model, params=params, prompt=prompt
            )
        if cached is not None:
            candidate = _read_call_response(store, cached)
            if validate(candidate):
                return (
                    LLMResponse(text=candidate, model=model, params=params),
                    [cached],
                    None,
                    False,
                )

        if not budget.take():
            return None, [], "call budget exhausted", False

        started = time.monotonic()
        try:
            response = _complete_with_retries(
                llm_client,
                prompt,
                model=model,
                params=params,
                attempts=transport_max_attempts,
                backoff_seconds=backoff_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            return None, [], f"transport failed: {exc}", False

        response.latency_ms = _latency_ms(response, started)
        return response, [], None, True

    def archive(
        response: LLMResponse, prompt: str, *, index: bool
    ) -> LLMCall:
        return store.archive_llm_call(
            purpose=purpose,
            model=model,
            params=params,
            prompt=prompt,
            response=response.text,
            tokens=response.tokens,
            latency_ms=response.latency_ms,
            index=index,
        )

    def author_one(idx: int, chunk: ProjectionChunk) -> tuple[
        list[BindingDraft], list[LLMCall], list[ResolutionError], list[Disposition]
    ]:
        try:
            return _author_one_chunk(idx, chunk)
        except Exception as exc:  # noqa: BLE001 - isolate one chunk's failure
            return (
                [],
                [],
                [ResolutionError(str(idx), chunk.key, f"chunk failed: {exc}")],
                [],
            )

    def validate_json(text: str) -> bool:
        try:
            parse_drafts_with_errors(text, include_address=include_address)
        except ValueError:
            return False  # stale/0.4.0 bad entry: ignore, re-call the model
        return True

    def _finish(chunk, chunk_drafts, chunk_calls) -> None:
        """Attach the archived call, the chunk's tab and the region bbox."""
        call_ref = chunk_calls[-1].call_id if chunk_calls else None
        for draft in chunk_drafts:
            if draft.provenance is not None:
                draft.provenance.llm_call_ref = call_ref
            # Carry the chunk's tab onto the draft so a field whose region_id
            # does not resolve still knows its tab (and a declared convention
            # matched by tab name can still apply to it).
            draft.tab = chunk.key
            if draft.region_id and draft.region_id in region_bbox:
                draft.bbox = region_bbox[draft.region_id]

    def _author_one_lines_chunk(
        idx: int, chunk: ProjectionChunk
    ) -> tuple[
        list[BindingDraft], list[LLMCall], list[ResolutionError], list[Disposition]
    ]:
        """One call per chunk, no repair call; parsed by the line parser (3.5)."""
        chunk_drafts: list[BindingDraft] = []
        chunk_calls: list[LLMCall] = []
        chunk_errors: list[ResolutionError] = []
        chunk_dispositions: list[Disposition] = []
        if layout is None:
            chunk_errors.append(
                ResolutionError(str(idx), chunk.key, "lines contract needs a layout")
            )
            return chunk_drafts, chunk_calls, chunk_errors, chunk_dispositions

        prompt = build_lines_prompt(chunk, prompt_variant)
        response, calls, transport_error, fresh = resolve_prompt(
            prompt, _lines_response_ok
        )
        chunk_calls.extend(calls)
        if transport_error is not None:
            chunk_errors.append(ResolutionError(str(idx), chunk.key, transport_error))
            return chunk_drafts, chunk_calls, chunk_errors, chunk_dispositions

        page = chunk.page if chunk.page is not None else 0
        chunk_drafts, dispositions, line_errors, stats = parse_lines_response(
            chunk,
            lattices.get(page),
            response,
            layout=layout,
            elements_by_id=elements_by_id,
            non_answer_element_ids=non_answer_ids,
        )
        chunk_dispositions.extend(dispositions)
        if fresh:
            # Indexed only when it parsed with no line errors, was not cut and
            # was not empty; a bad response is archived unindexed so a rerun
            # re-sends it and the re-ask set can differ (section 5.3).
            indexed = (
                not line_errors and not stats.truncated and not stats.empty_response
            )
            chunk_calls.append(archive(response, prompt, index=indexed))
        _finish(chunk, chunk_drafts, chunk_calls)
        for reason in line_errors:
            chunk_errors.append(ResolutionError(str(idx), chunk.key, reason))
        return chunk_drafts, chunk_calls, chunk_errors, chunk_dispositions

    def _author_one_chunk(
        idx: int, chunk: ProjectionChunk
    ) -> tuple[
        list[BindingDraft], list[LLMCall], list[ResolutionError], list[Disposition]
    ]:
        chunk_drafts: list[BindingDraft] = []
        chunk_calls: list[LLMCall] = []
        chunk_errors: list[ResolutionError] = []
        chunk_dispositions: list[Disposition] = []
        if lines_mode:
            return _author_one_lines_chunk(idx, chunk)

        prompt = build_prompt(chunk, include_address=include_address)

        response, calls, transport_error, fresh = resolve_prompt(prompt, validate_json)
        chunk_calls.extend(calls)
        if transport_error is not None:
            chunk_errors.append(
                ResolutionError(str(idx), chunk.key, transport_error)
            )
            return chunk_drafts, chunk_calls, chunk_errors, chunk_dispositions

        try:
            chunk_drafts, field_errors = parse_drafts_with_errors(
                response.text, include_address=include_address
            )
        except ValueError as exc:
            if fresh:
                chunk_calls.append(archive(response, prompt, index=False))
            repair_prompt = (
                prompt
                + "\n\nYour previous response was not valid JSON. "
                + f"Parse error: {exc}\nReturn ONLY valid JSON matching the contract."
            )
            response, calls, transport_error, fresh = resolve_prompt(
                repair_prompt, validate_json
            )
            chunk_calls.extend(calls)
            if transport_error is not None:
                chunk_errors.append(
                    ResolutionError(str(idx), chunk.key, f"repair {transport_error}")
                )
                return chunk_drafts, chunk_calls, chunk_errors, chunk_dispositions
            try:
                chunk_drafts, field_errors = parse_drafts_with_errors(
                    response.text, include_address=include_address
                )
            except ValueError as exc2:
                if fresh:
                    chunk_calls.append(archive(response, repair_prompt, index=False))
                chunk_errors.append(
                    ResolutionError(
                        str(idx), chunk.key, f"parse failed after repair: {exc2}"
                    )
                )
                return chunk_drafts, chunk_calls, chunk_errors, chunk_dispositions
            else:
                if fresh:
                    chunk_calls.append(
                        archive(response, repair_prompt, index=not field_errors)
                    )
        else:
            if fresh:
                # A response with per-field errors still parses, but indexing it
                # would serve the same partial answer on every rerun. A salvaged
                # length-truncated response lands here too (its field list
                # carries the `response truncated after N fields` error), which
                # is what skips the repair call: the cut is a reported condition
                # the repair provably reproduces.
                chunk_calls.append(archive(response, prompt, index=not field_errors))

        _finish(chunk, chunk_drafts, chunk_calls)
        for reason in field_errors:
            chunk_errors.append(ResolutionError(str(idx), chunk.key, reason))
        return chunk_drafts, chunk_calls, chunk_errors, chunk_dispositions

    if max_workers <= 1:
        results = [
            author_one(idx, chunk) for idx, chunk in enumerate(chunks)
        ]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(
                executor.map(author_one, range(len(chunks)), chunks)
            )

    drafts: list[BindingDraft] = []
    calls: list[LLMCall] = []
    errors: list[ResolutionError] = []
    for chunk_drafts, chunk_calls, chunk_errors, chunk_dispositions in results:
        drafts.extend(chunk_drafts)
        calls.extend(chunk_calls)
        errors.extend(chunk_errors)
        if dispositions_out is not None:
            dispositions_out.extend(chunk_dispositions)
    return drafts, calls, errors


def _remap_region_id(region_id: str | None, source_prefix: str, target_prefix: str) -> str | None:
    if region_id is None:
        return None
    if region_id.startswith(source_prefix):
        return target_prefix + region_id[len(source_prefix):]
    return region_id


def remap_drafts_for_page(
    drafts: list[BindingDraft],
    source_page: int,
    target_page: int,
    *,
    regions: list[Region] | None = None,
    region_bbox: dict[str, BBox] | None = None,
) -> list[BindingDraft]:
    """Rebase exemplar drafts onto another page of the same workbook.

    Region pointers are remapped structurally, never by text matching: each
    ``source_ref`` is resolved to its source region to recover the column, then
    pointed at the target page's region for that same ``(column, band_id)``.
    Band and segment indices stay the same because reuse is only attempted
    across geometrically equal tabs. This keeps value refs working even when a
    content-derived value region id differs across tabs (the target's own
    answer text is never matched). The exemplar's authored answers and option
    selections are cleared so a reused tab re-derives values from its own
    elements rather than replaying someone else's filled answers.
    """
    regions = regions or []
    region_bbox = region_bbox or {}
    source_regions = {r.region_id: r for r in regions if r.bbox.page == source_page}
    target_region_for_band: dict[tuple[int, int], Region] = {}
    for region in regions:
        if region.bbox.page != target_page:
            continue
        for band_id in region.band_ids:
            target_region_for_band[(region.column, band_id)] = region

    out: list[BindingDraft] = []
    for draft in drafts:
        anchor_band = None
        for ref in draft.source_refs:
            if ref.region_id == draft.region_id:
                anchor_band = ref.band_id
                break

        def remap_refs(refs: list[ElementRef]) -> list[ElementRef]:
            out_refs: list[ElementRef] = []
            for ref in refs:
                source_region = source_regions.get(ref.region_id)
                if source_region is None:
                    out_refs.append(ref)  # unresolvable: verification refuses the tab
                    continue
                target_region = target_region_for_band.get(
                    (source_region.column, ref.band_id)
                )
                if target_region is None:
                    out_refs.append(ref)  # no such column/band in the target tab
                    continue
                out_refs.append(
                    ElementRef(
                        region_id=target_region.region_id,
                        band_id=ref.band_id,
                        segment_index=ref.segment_index,
                    )
                )
            return out_refs

        new_refs = remap_refs(draft.source_refs)
        # The lines contract's TEXT answer refs point at the value cells
        # directly, so they are remapped by the same rule and re-read from the
        # target tab (reuse.apply_binding). Empty on the JSON path.
        new_answer_refs = remap_refs(list(draft.answer_refs))

        new_region: str | None
        if draft.region_id is None:
            new_region = None
        else:
            source_region = source_regions.get(draft.region_id)
            if source_region is not None and anchor_band is not None:
                target_region = target_region_for_band.get(
                    (source_region.column, anchor_band)
                )
                new_region = target_region.region_id if target_region else None
            else:
                new_region = _remap_region_id(
                    draft.region_id,
                    f"p{source_page}:",
                    f"p{target_page}:",
                )

        out.append(
            BindingDraft(
                draft_id=f"replay-{uuid.uuid4().hex[:12]}",
                label=draft.label,
                control_type=draft.control_type,
                bbox=region_bbox.get(new_region, draft.bbox),
                options=[
                    Option(text=o.text, selected=None, raw_span=o.raw_span, bbox=o.bbox)
                    for o in draft.options
                ],
                answers=[],
                annotations=list(draft.annotations),
                canonical_name=draft.canonical_name,
                region_id=new_region,
                source_refs=new_refs,
                address=draft.address,
                confidence=draft.confidence,
                provenance=draft.provenance,
                answer_refs=tuple(new_answer_refs),
                review_reason=draft.review_reason,
            )
        )
    return out


def drafts_resolve_cleanly(drafts: list[BindingDraft], layout: LayoutResult) -> bool:
    """True when every draft's region and source-element pointers resolve.

    This is the third rung of the reuse ladder: a geometry+label match still
    falls back to a fresh call if any reused binding points at a region, band
    or segment that does not exist in the current tab.
    """
    region_by_id = {r.region_id: r for r in layout.regions}
    for draft in drafts:
        if draft.region_id is None or draft.region_id not in region_by_id:
            return False
        for ref in draft.source_refs:
            region = region_by_id.get(ref.region_id)
            if region is None:
                return False
            bands = layout.bands.get(region.bbox.page * 1000 + region.column)
            if bands is None:
                return False
            if ref.band_id < 0 or ref.band_id >= len(bands):
                return False
            if ref.segment_index < 0 or ref.segment_index >= len(bands[ref.band_id]):
                return False
    return True


def _anchor_band_id(draft: BindingDraft, region_id: str | None) -> int | None:
    for ref in draft.source_refs:
        if ref.region_id == region_id:
            return ref.band_id
    return None


def _anchor_sort_key(
    draft: BindingDraft, region_id: str | None, position: int = 0
) -> tuple[int, tuple[int, int], str, int]:
    """Deterministic order for the drafts that share one region.

    The primary key is the anchor band the draft cites inside its own region.
    Drafts tied on that band used to keep the model's response order (the sort
    is stable), so the output order depended on the order the model happened to
    emit. The secondary key is the smallest (band, segment) the draft cites,
    then the normalised label, then the draft's original position as the final
    fallback, which fixes ``field_id`` ordinals for tied drafts.
    """
    anchor = _anchor_band_id(draft, region_id)
    refs = draft.source_refs
    smallest_ref = (
        min((ref.band_id, ref.segment_index) for ref in refs)
        if refs
        else (10**9, 10**9)
    )
    return (
        anchor if anchor is not None else 10**9,
        smallest_ref,
        normalize_label(draft.label),
        position,
    )


def _field_id(
    page: int, column: int, region_id: str | None, anchor_norm: str, ordinal: int
) -> str:
    raw = "|".join([str(page), str(column), region_id or "", anchor_norm, str(ordinal)])
    return "f-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _section_path(
    region: Region | None,
    layout: LayoutResult | None,
    elements_by_id: dict[str, Element],
) -> list[str]:
    """Header regions above `region` on the same page, top-to-bottom."""
    if region is None or layout is None:
        return []
    headers: list[tuple[float, float, str]] = []
    for other in layout.regions:
        if other.type is not RegionType.HEADER:
            continue
        if other.bbox.page != region.bbox.page:
            continue
        if other.bbox.y1 > region.bbox.y0:
            continue
        texts = [
            elements_by_id[eid].text
            for eid in other.element_ids
            if eid in elements_by_id
        ]
        if texts:
            headers.append(
                (other.bbox.y0, other.bbox.x0, normalize_label(" ".join(texts)))
            )
    headers.sort(key=lambda item: (item[0], item[1]))
    return [text for _, _, text in headers]


@dataclass
class _MarkDecision:
    kind: str  # "auto_select" | "declared_convention" | "ambiguous" | "declining"
    option_text: str | None = None
    #: Every option text an ``auto_select`` decision selected (the union over a
    #: field's right-only single-candidate markers, de-duplicated by normalised
    #: text, in marker order). ``option_text`` stays the first element for the
    #: consumers that predate the union.
    option_texts: list[str] = field(default_factory=list)
    #: Markers that were right-only single-candidate but declined for themselves
    #: (missing candidate or an empty stripped text). They do not cancel the
    #: selectable markers; their presence is what forces a review flag.
    declined_marker_element_ids: list[str] = field(default_factory=list)
    marker_element_id: str | None = None
    candidate_element_ids: list[str] = field(default_factory=list)
    left_candidate_element_ids: list[str] = field(default_factory=list)
    right_candidate_element_ids: list[str] = field(default_factory=list)
    reason: str | None = None


def _match_option(option_text: str, options: list[Option]) -> str | None:
    target = normalize_label(option_text)
    if not target:
        return None
    for o in options:
        if normalize_label(o.text) == target:
            return o.text
    for o in options:
        candidate = normalize_label(o.text)
        if candidate and (target.startswith(candidate) or candidate.startswith(target)):
            return o.text
    return None


def _between_competing_short_circuit(
    between: list[MarkerClassification],
    competing: list[MarkerClassification],
) -> _MarkDecision | None:
    """The BETWEEN/COMPETING ambiguity short-circuits, decided before the union."""
    if between:
        c = between[0]
        return _MarkDecision(
            kind="ambiguous",
            marker_element_id=c.marker_element_id,
            candidate_element_ids=c.candidate_element_ids,
            left_candidate_element_ids=c.left_candidate_element_ids,
            right_candidate_element_ids=c.right_candidate_element_ids,
            reason=AMBIGUITY_BETWEEN_OPTIONS,
        )
    if competing:
        c = competing[0]
        return _MarkDecision(
            kind="ambiguous",
            marker_element_id=c.marker_element_id,
            candidate_element_ids=c.candidate_element_ids,
            left_candidate_element_ids=c.left_candidate_element_ids,
            right_candidate_element_ids=c.right_candidate_element_ids,
            reason=AMBIGUITY_COMPETING_OPTIONS,
        )
    return None


def _right_only_option_texts(
    right_only: list[MarkerClassification],
    elements_by_id: dict[str, Element],
) -> tuple[list[str], list[str]]:
    """Union of the options a field's right-only single-candidate markers select.

    Returns ``(option_texts, declined_marker_ids)``. ``option_texts`` is every
    marker's single candidate text, in marker order, de-duplicated by normalised
    text, so two markers pointing at one option select it once. A marker whose
    candidate element is missing or whose stripped text is empty declines for
    itself: it is recorded in ``declined_marker_ids`` and never cancels the
    other markers.
    """
    option_texts: list[str] = []
    seen: set[str] = set()
    declined: list[str] = []
    for c in right_only:
        candidate = elements_by_id.get(c.right_candidate_element_ids[0])
        option_text = strip_marks(candidate.text) if candidate is not None else None
        if not option_text:
            declined.append(c.marker_element_id)
            continue
        key = normalize_label(option_text)
        if key in seen:
            continue
        seen.add(key)
        option_texts.append(option_text)
    return option_texts, declined


def _geometric_mark_decision(
    draft: BindingDraft,
    source_elements: list[str],
    layout: LayoutResult,
    elements_by_id: dict[str, Element],
    non_answer_ids: set[str],
) -> _MarkDecision | None:
    """Decide a field's mark from layout classification, ignoring the model."""
    glyph_ids = {g.element_id for g in layout.glyphs}
    marker_by_id = {mc.marker_element_id: mc for mc in layout.marker_classes}

    draft_glyph_ids = [
        eid
        for eid in source_elements
        if eid in glyph_ids and eid not in non_answer_ids
    ]
    if not draft_glyph_ids:
        return None

    classifications = [
        marker_by_id[eid] for eid in draft_glyph_ids if eid in marker_by_id
    ]
    unclassified = [eid for eid in draft_glyph_ids if eid not in marker_by_id]

    between = [c for c in classifications if c.marker_class is MarkerClass.BETWEEN]
    competing = [
        c
        for c in classifications
        if c.marker_class is MarkerClass.RIGHT_ONLY
        and len(c.right_candidate_element_ids) > 1
    ]
    right_only = [
        c
        for c in classifications
        if c.marker_class is MarkerClass.RIGHT_ONLY
        and len(c.right_candidate_element_ids) == 1
        and not c.left_candidate_element_ids
    ]

    short_circuit = _between_competing_short_circuit(between, competing)
    if short_circuit is not None:
        return short_circuit
    if right_only:
        option_texts, declined = _right_only_option_texts(right_only, elements_by_id)
        first = right_only[0]
        if not option_texts:
            return _MarkDecision(
                kind="declining",
                marker_element_id=first.marker_element_id,
                candidate_element_ids=first.candidate_element_ids,
                left_candidate_element_ids=first.left_candidate_element_ids,
                right_candidate_element_ids=first.right_candidate_element_ids,
            )
        return _MarkDecision(
            kind="auto_select",
            option_text=option_texts[0],
            option_texts=option_texts,
            declined_marker_element_ids=declined,
            marker_element_id=first.marker_element_id,
            candidate_element_ids=first.candidate_element_ids,
            left_candidate_element_ids=first.left_candidate_element_ids,
            right_candidate_element_ids=first.right_candidate_element_ids,
        )
    if classifications:
        c = classifications[0]
        return _MarkDecision(
            kind="declining",
            marker_element_id=c.marker_element_id,
            candidate_element_ids=c.candidate_element_ids,
        )
    if unclassified:
        return _MarkDecision(kind="declining", marker_element_id=unclassified[0])
    return None


def _tab_has_wildcard(tab: str) -> bool:
    return any(ch in tab for ch in ("*", "?", "[", "]"))


def _convention_specificity(
    convention: CheckboxConvention,
) -> tuple[bool, bool, int]:
    return (
        convention.anchor_pattern is not None,
        not _tab_has_wildcard(convention.tab),
        len(convention.tab),
    )


def _matching_conventions(
    conventions: list[CheckboxConvention] | None,
    tab: str | None,
    label: str,
) -> list[CheckboxConvention]:
    if not conventions:
        return []
    norm_label = normalize_label(label)
    out: list[CheckboxConvention] = []
    for c in conventions:
        if tab is not None and not fnmatch.fnmatch(tab, c.tab):
            continue
        if c.anchor_pattern is not None and re.search(c.anchor_pattern, norm_label) is None:
            continue
        out.append(c)
    return out


def _most_specific_convention(
    matches: list[CheckboxConvention],
) -> CheckboxConvention | None:
    if not matches:
        return None
    best = matches[0]
    best_key = _convention_specificity(best)
    for c in matches[1:]:
        key = _convention_specificity(c)
        if key > best_key:
            best, best_key = c, key
    return best


def _apply_declared_convention(
    decision: _MarkDecision,
    draft: BindingDraft,
    tab: str | None,
    elements_by_id: dict[str, Element],
    conventions: list[CheckboxConvention] | None,
) -> _MarkDecision:
    """Apply a caller-declared convention to a marker decision.

    The model is never consulted here. A convention can only *select* for the
    BETWEEN class; any other class keeps its own rule. A convention the geometry
    cannot support (no candidate on the required side) selects nothing, keeps
    the ambiguity and is recorded with ``AMBIGUITY_CONVENTION_UNSUPPORTED``.
    """
    convention = _most_specific_convention(
        _matching_conventions(conventions, tab, draft.label)
    )
    if convention is None:
        return decision

    if convention.convention == CHECKBOX_MARK_FOLLOWS_OPTION:
        candidate_ids = decision.left_candidate_element_ids
    else:
        candidate_ids = decision.right_candidate_element_ids

    option_text = None
    if candidate_ids:
        candidate = elements_by_id.get(candidate_ids[0])
        option_text = strip_marks(candidate.text) if candidate is not None else None
    if option_text is None:
        return _MarkDecision(
            kind="ambiguous",
            marker_element_id=decision.marker_element_id,
            candidate_element_ids=decision.candidate_element_ids,
            left_candidate_element_ids=decision.left_candidate_element_ids,
            right_candidate_element_ids=decision.right_candidate_element_ids,
            reason=AMBIGUITY_CONVENTION_UNSUPPORTED,
        )
    if decision.reason != AMBIGUITY_BETWEEN_OPTIONS:
        return decision
    return _MarkDecision(
        kind="declared_convention",
        option_text=option_text,
        marker_element_id=decision.marker_element_id,
        candidate_element_ids=decision.candidate_element_ids,
        left_candidate_element_ids=decision.left_candidate_element_ids,
        right_candidate_element_ids=decision.right_candidate_element_ids,
    )


def _selections_disagree(
    model_selected: list[str], geometric_selected: list[str]
) -> bool:
    """Whether the model's ``selected`` flags differ from the geometric set."""
    return {normalize_label(t) for t in model_selected} != {
        normalize_label(t) for t in geometric_selected
    }


def drafts_to_fields(
    drafts: list[BindingDraft],
    *,
    layout: LayoutResult | None = None,
    elements_by_id: dict[str, Element] | None = None,
    tabs: list[str] | None = None,
    non_answer_element_ids: set[str] | None = None,
    checkbox_conventions: list[CheckboxConvention] | None = None,
    replayed_draft_ids: set[str] | None = None,
    page_tabs: dict[int, str] | None = None,
) -> list[Field]:
    elements_by_id = elements_by_id or {}
    non_answer_ids = non_answer_element_ids or set()
    regions = layout.regions if layout else []
    region_by_id = {r.region_id: r for r in regions}

    by_region: dict[str | None, list[BindingDraft]] = {}
    for draft in drafts:
        by_region.setdefault(draft.region_id, []).append(draft)

    fields: list[Field] = []
    ordinal_by_region: dict[str | None, int] = {}
    for region_id, group in by_region.items():
        group = [
            draft
            for _position, draft in sorted(
                enumerate(group),
                key=lambda pair: _anchor_sort_key(pair[1], region_id, pair[0]),
            )
        ]
        for draft in group:
            region = region_by_id.get(region_id) if region_id is not None else None
            page = region.bbox.page if region else 0
            column = region.column if region else -1
            ordinal = ordinal_by_region.get(region_id, 0)
            ordinal_by_region[region_id] = ordinal + 1

            canon = canonical_name(draft.label)
            cf = canonical_field(canon) if canon else None
            if cf is None:
                normalizer = "text" if draft.control_type is ControlType.TEXT else "none"
                canonical = None
            else:
                normalizer = cf.normalizer_id
                canonical = cf.name

            source_elements: list[str] = []
            unresolved: list[str] = []
            for ref in draft.source_refs:
                ref_region = region_by_id.get(ref.region_id)
                band_key = (
                    ref_region.bbox.page * 1000 + ref_region.column
                    if ref_region
                    else None
                )
                bands = layout.bands if layout else {}
                band = (
                    bands.get(band_key)
                    if band_key is not None and band_key in bands
                    else None
                )
                if (
                    band is None
                    or ref.band_id < 0
                    or ref.band_id >= len(band)
                    or ref.segment_index < 0
                    or ref.segment_index >= len(band[ref.band_id])
                ):
                    unresolved.append(
                        f"{ref.region_id}:{ref.band_id}:{ref.segment_index}"
                    )
                    continue
                source_elements.append(band[ref.band_id][ref.segment_index])

            tab = draft.tab
            if region is not None:
                if page_tabs is not None:
                    tab = page_tabs.get(page, f"page {page}")
                else:
                    tab = (
                        tabs[page]
                        if tabs is not None and page < len(tabs)
                        else f"page {page}"
                    )

            decision = (
                _geometric_mark_decision(
                    draft, source_elements, layout, elements_by_id, non_answer_ids
                )
                if layout is not None
                else None
            )
            if decision is not None and checkbox_conventions:
                decision = _apply_declared_convention(
                    decision, draft, tab, elements_by_id, checkbox_conventions
                )

            if decision is not None and decision.kind in ("auto_select", "declared_convention"):
                option_texts = decision.option_texts or (
                    [decision.option_text] if decision.option_text else []
                )
                matched_set: list[str] = []
                matched_keys: set[str] = set()
                for text in option_texts:
                    matched = _match_option(text, draft.options)
                    if matched is None:
                        continue
                    key = normalize_label(matched)
                    if key in matched_keys:
                        continue
                    matched_keys.add(key)
                    matched_set.append(matched)
                if not matched_set:
                    decision = _MarkDecision(
                        kind="declining",
                        marker_element_id=decision.marker_element_id,
                        candidate_element_ids=decision.candidate_element_ids,
                    )
                    options = [
                        Option(
                            text=o.text,
                            selected=None,
                            raw_span=o.raw_span,
                            bbox=o.bbox,
                        )
                        for o in draft.options
                    ]
                elif decision.kind == "declared_convention":
                    options = [
                        Option(
                            text=o.text,
                            selected=(True if o.text in matched_set else None),
                            raw_span=o.raw_span,
                            bbox=o.bbox,
                        )
                        for o in draft.options
                    ]
                else:
                    options = [
                        Option(
                            text=o.text,
                            selected=(o.text in matched_set),
                            raw_span=o.raw_span,
                            bbox=o.bbox,
                        )
                        for o in draft.options
                    ]
            elif decision is not None:
                options = [
                    Option(
                        text=o.text,
                        selected=None,
                        raw_span=o.raw_span,
                        bbox=o.bbox,
                    )
                    for o in draft.options
                ]
            else:
                options = list(draft.options)

            selected = [o.text for o in options if o.selected]
            if decision is not None and decision.kind in ("ambiguous", "declining"):
                value_raw = None
            elif draft.control_type is ControlType.TEXT:
                value_raw = draft.answers[0] if draft.answers else None
            elif selected:
                value_raw = ", ".join(selected)
            else:
                value_raw = ", ".join(draft.answers) or None
            value_normalized = (
                None
                if decision is not None and decision.kind in ("ambiguous", "declining")
                else normalize(normalizer, value_raw, options)
            )

            replayed = replayed_draft_ids is not None and draft.draft_id in replayed_draft_ids
            provenance = Provenance(
                source=ProvenanceSource.REPLAY if replayed else ProvenanceSource.LLM,
                binding_id=draft.draft_id,
                llm_confidence=draft.confidence,
            )
            if unresolved:
                provenance.review_flag = True
                provenance.review_reason = ReviewReason.UNRESOLVED_REFERENCE
            if (
                replayed
                and draft.control_type is ControlType.TEXT
                and not draft.answers
            ):
                provenance.review_flag = True
                provenance.review_reason = ReviewReason.REPLAY_MISMATCH
            if decision is not None and decision.kind in ("ambiguous", "declining"):
                provenance.review_flag = True
                provenance.review_reason = ReviewReason.AMBIGUOUS_MARK
            if decision is not None and decision.kind in (
                "auto_select",
                "declared_convention",
            ):
                if decision.kind == "auto_select":
                    provenance.heuristic_agreement.append(GEOMETRIC_SELECTION_TOKEN)
                else:
                    provenance.heuristic_agreement.append(DECLARED_CONVENTION_TOKEN)
                # The one independent second opinion (section 4): the model's
                # `selected` flags on the json path, the `A=` option refs on the
                # lines path. A declared convention had no check before L1b.
                model_selected = [o.text for o in draft.options if o.selected]
                geometric_selected = [o.text for o in options if o.selected]
                if model_selected and _selections_disagree(
                    model_selected, geometric_selected
                ):
                    provenance.review_flag = True
                    provenance.review_reason = ReviewReason.AMBIGUOUS_MARK
                if decision.declined_marker_element_ids:
                    provenance.review_flag = True
                    provenance.review_reason = ReviewReason.AMBIGUOUS_MARK
            if draft.review_reason is not None and not provenance.review_flag:
                # A review reason the response decided without re-scanning its
                # refs (lines only): an unknown kind degrades to AMBIGUOUS_ROLE.
                provenance.review_flag = True
                provenance.review_reason = draft.review_reason

            ambiguity = None
            if decision is not None and decision.kind == "ambiguous":
                ambiguity = FieldAmbiguity(
                    marker_element_id=decision.marker_element_id,
                    candidate_element_ids=decision.candidate_element_ids,
                    reason=decision.reason,
                )

            fields.append(
                Field(
                    label_text=draft.label,
                    control_type=draft.control_type,
                    bbox=draft.bbox,
                    options=options,
                    answers=draft.answers,
                    annotations=draft.annotations,
                    region_ref=draft.region_id,
                    canonical_name=canonical,
                    value_raw=value_raw,
                    value_normalized=value_normalized,
                    normalizer_id=normalizer,
                    provenance=provenance,
                    tab=tab,
                    source_elements=source_elements,
                    field_id=_field_id(
                        page, column, region_id, normalize_label(draft.label), ordinal
                    ),
                    section_path=_section_path(region, layout, elements_by_id),
                    unresolved_source_refs=unresolved,
                    ambiguity=ambiguity,
                )
            )
    return fields

