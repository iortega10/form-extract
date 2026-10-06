"""Cold-path binding authoring: LLM call plus prompt/response archival.
Authors bindings; does not resolve values (that is replay's job)."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from .layout import strip_marks
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
from .schema import canonical_field, canonical_name, normalize, normalize_label

BINDING_OUTPUT_CONTRACT = """{"fields": [
  {"label": str, "control_type": "single_select|multi_select|bool|text",
   "options": [{"text": str, "selected": bool}],
   "answer": [str], "annotations": [str],
   "region_id": str,
   "source_elements": [{"region_id": str, "band_id": int, "segment_index": int}],
   "address": {"anchor_text": str, "column": int, "row_band": int, "offset_right": int},
   "confidence": number}]}"""

PROMPT_VERSION = "2"
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


class LLMClient(Protocol):
    def complete(self, prompt: str, *, model: str, params: dict[str, Any]) -> LLMResponse: ...


@dataclass
class ProjectionChunk:
    key: str
    text: str


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
) -> list[ProjectionChunk]:
    non_answer_ids = non_answer_element_ids or set()
    by_id = {e.element_id: e for e in elements}
    by_page: dict[int, list[Region]] = {}
    for region in layout.regions:
        by_page.setdefault(region.bbox.page, []).append(region)

    chunks: list[ProjectionChunk] = []
    for page in sorted(by_page):
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
        chunks.append(ProjectionChunk(key=tab, text="\n".join(lines).strip()))
    if not chunks and elements:
        lines = [f"- {e.text}" for e in sorted(elements, key=lambda e: (e.bbox.y0, e.bbox.x0))]
        chunks.append(ProjectionChunk(key=tabs[0] if tabs else "page 0", text="\n".join(lines)))
    return chunks


def build_prompt(chunk: ProjectionChunk) -> str:
    return (
        "You extract structured fields from one region group of a non-uniform "
        "form (labels on the left, controls on the right).\n"
        "Return ONLY JSON matching exactly this contract:\n"
        f"{BINDING_OUTPUT_CONTRACT}\n"
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
        "- 'address' points at the anchor (left-column label text) and where the value "
        "lives relative to it: column index, band index within that column, and how many "
        "text blocks right of the anchor.\n"
        "- 'source_elements' lists the projection segments this field is built from: "
        "for each label/option/mark element, give its region_id, band_id and the "
        "segment index shown as the `i=` prefix on that band line.\n"
        "- 'confidence' is your calibrated probability that the grouping is right.\n\n"
        f"Layout projection for `{chunk.key}`:\n{chunk.text}"
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


def parse_drafts(text: str) -> list[BindingDraft]:
    drafts, _ = parse_drafts_with_errors(text)
    return drafts


def parse_drafts_with_errors(text: str) -> tuple[list[BindingDraft], list[str]]:
    data = _extract_json(text)
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
            drafts.append(_parse_one_draft(item, now))
        except Exception as exc:  # noqa: BLE001 - one bad field must not drop the chunk
            errors.append(f"field {idx}: {exc}")
    return drafts, errors


def _parse_one_draft(item: dict, now: str) -> BindingDraft:
    control_type = ControlType(item["control_type"])
    options = [
        Option(
            text=o["text"],
            selected=o.get("selected"),
        )
        for o in item.get("options", [])
    ]
    address = None
    if item.get("address"):
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
            if attempt + 1 < attempts and backoff_seconds:
                time.sleep(backoff_seconds * (2**attempt))
    assert last_exc is not None
    raise last_exc


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
) -> tuple[list[BindingDraft], list[LLMCall], list[ResolutionError]]:
    region_bbox = {r.region_id: r.bbox for r in (regions or [])}
    drafts: list[BindingDraft] = []
    calls: list[LLMCall] = []
    errors: list[ResolutionError] = []
    budget = call_budget if call_budget is not None else CALL_BUDGET_PER_CHUNK * len(chunks)

    def resolve_prompt(
        prompt: str,
    ) -> tuple[str | None, list[LLMCall], str | None]:
        """Return (response_text, chunk_calls, transport_error)."""
        cached = store.find_llm_call(
            purpose=purpose, model=model, params=params, prompt=prompt
        )
        if cached is not None:
            candidate = _read_call_response(store, cached)
            try:
                parse_drafts_with_errors(candidate)
            except ValueError:
                # A cached unparseable original goes straight to the repair
                # prompt; returning it here lets the caller skip re-spending
                # the original and only pay for the repair call.
                return candidate, [cached], None
            else:
                return candidate, [cached], None

        nonlocal budget
        if budget <= 0:
            return None, [], "call budget exhausted"
        budget -= 1
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
            return None, [], f"transport failed: {exc}"
        call = store.archive_llm_call(
            purpose=purpose,
            model=model,
            params=params,
            prompt=prompt,
            response=response.text,
            tokens=response.tokens,
            latency_ms=_latency_ms(response, started),
        )
        return response.text, [call], None

    for idx, chunk in enumerate(chunks):
        prompt = build_prompt(chunk)
        response_text, chunk_calls, transport_error = resolve_prompt(prompt)
        if transport_error is not None:
            calls.extend(chunk_calls)
            errors.append(ResolutionError(str(idx), chunk.key, transport_error))
            continue

        try:
            chunk_drafts, field_errors = parse_drafts_with_errors(response_text)
        except ValueError as exc:
            repair_prompt = (
                prompt
                + "\n\nYour previous response was not valid JSON. "
                + f"Parse error: {exc}\nReturn ONLY valid JSON matching the contract."
            )
            response_text, repair_calls, transport_error = resolve_prompt(repair_prompt)
            chunk_calls.extend(repair_calls)
            if transport_error is not None:
                calls.extend(chunk_calls)
                errors.append(
                    ResolutionError(str(idx), chunk.key, f"repair {transport_error}")
                )
                continue
            try:
                chunk_drafts, field_errors = parse_drafts_with_errors(response_text)
            except ValueError as exc2:
                calls.extend(chunk_calls)
                errors.append(
                    ResolutionError(
                        str(idx), chunk.key, f"parse failed after repair: {exc2}"
                    )
                )
                continue

        calls.extend(chunk_calls)
        call_ref = chunk_calls[-1].call_id if chunk_calls else None
        for draft in chunk_drafts:
            if draft.provenance is not None:
                draft.provenance.llm_call_ref = call_ref
            if draft.region_id and draft.region_id in region_bbox:
                draft.bbox = region_bbox[draft.region_id]
        drafts.extend(chunk_drafts)
        for reason in field_errors:
            errors.append(ResolutionError(str(idx), chunk.key, reason))

    return drafts, calls, errors


def _anchor_band_id(draft: BindingDraft, region_id: str | None) -> int | None:
    for ref in draft.source_refs:
        if ref.region_id == region_id:
            return ref.band_id
    return None


def _anchor_sort_key(draft: BindingDraft, region_id: str | None) -> int:
    anchor = _anchor_band_id(draft, region_id)
    return anchor if anchor is not None else 10**9


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
    if right_only:
        c = right_only[0]
        candidate = elements_by_id.get(c.right_candidate_element_ids[0])
        option_text = strip_marks(candidate.text) if candidate is not None else None
        if option_text is None:
            return _MarkDecision(
                kind="declining",
                marker_element_id=c.marker_element_id,
                candidate_element_ids=c.candidate_element_ids,
                left_candidate_element_ids=c.left_candidate_element_ids,
                right_candidate_element_ids=c.right_candidate_element_ids,
            )
        return _MarkDecision(
            kind="auto_select",
            option_text=option_text,
            marker_element_id=c.marker_element_id,
            candidate_element_ids=c.candidate_element_ids,
            left_candidate_element_ids=c.left_candidate_element_ids,
            right_candidate_element_ids=c.right_candidate_element_ids,
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


def drafts_to_fields(
    drafts: list[BindingDraft],
    *,
    layout: LayoutResult | None = None,
    elements_by_id: dict[str, Element] | None = None,
    tabs: list[str] | None = None,
    non_answer_element_ids: set[str] | None = None,
    checkbox_conventions: list[CheckboxConvention] | None = None,
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
        group.sort(key=lambda d: _anchor_sort_key(d, region_id))
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

            tab = None
            if region is not None:
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
                matched = _match_option(decision.option_text or "", draft.options)
                if matched is None:
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
                            selected=(True if o.text == matched else None),
                            raw_span=o.raw_span,
                            bbox=o.bbox,
                        )
                        for o in draft.options
                    ]
                else:
                    options = [
                        Option(
                            text=o.text,
                            selected=(o.text == matched),
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

            provenance = Provenance(
                source=ProvenanceSource.LLM,
                binding_id=draft.draft_id,
                llm_confidence=draft.confidence,
            )
            if unresolved:
                provenance.review_flag = True
                provenance.review_reason = ReviewReason.UNRESOLVED_REFERENCE
            if decision is not None and decision.kind in ("ambiguous", "declining"):
                provenance.review_flag = True
                provenance.review_reason = ReviewReason.AMBIGUOUS_MARK
            if decision is not None and decision.kind == "auto_select":
                provenance.heuristic_agreement.append(GEOMETRIC_SELECTION_TOKEN)
                model_selected = [o.text for o in draft.options if o.selected]
                geometric_selected = [o.text for o in options if o.selected]
                if model_selected and {
                    normalize_label(t) for t in model_selected
                } != {normalize_label(t) for t in geometric_selected}:
                    provenance.review_flag = True
                    provenance.review_reason = ReviewReason.AMBIGUOUS_MARK
            if decision is not None and decision.kind == "declared_convention":
                provenance.heuristic_agreement.append(DECLARED_CONVENTION_TOKEN)

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

