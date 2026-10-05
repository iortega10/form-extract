"""Cold-path binding authoring: LLM call plus prompt/response archival.
Authors bindings; does not resolve values (that is replay's job)."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from .model import (
    AuthoredBy,
    BBox,
    BindingDraft,
    BindingProvenance,
    ControlType,
    Element,
    Field,
    LayoutResult,
    LLMCall,
    Option,
    Provenance,
    ProvenanceSource,
    RelationalAddress,
    Region,
)
from .schema import canonical_field, canonical_name, normalize, normalize_label

BINDING_OUTPUT_CONTRACT = """{"fields": [
  {"label": str, "control_type": "single_select|multi_select|bool|text",
   "options": [{"text": str, "selected": bool}],
   "answer": [str], "annotations": [str],
   "region_id": str,
   "address": {"anchor_text": str, "column": int, "row_band": int, "offset_right": int},
   "confidence": number}]}"""

PROMPT_VERSION = "1"

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
    layout: LayoutResult, elements: list[Element], tabs: list[str]
) -> list[ProjectionChunk]:
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
                texts = [
                    by_id[eid].text for eid in bands[band_id] if eid in by_id
                ]
                lines.append(f"- band {band_id}: " + " | ".join(texts))
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
        "- 'X', a checkmark, or a checked box selects an option; resolve it from group "
        "context (sibling options, select-all controls, repeating grids). A row of many "
        "short options is multi_select; Yes/No siblings are single_select.\n"
        "- A '...All' control that selects an entire option grid belongs to that grid's "
        "field: answer includes \"ALL\" and the grid options are merged into that field.\n"
        "- Sub-instructions (e.g. 'if not all please check those that apply') and notes "
        "(e.g. 'Confirm on vendor portal', parenthetical instructions) are annotations, "
        "never answers. One field per user-visible question.\n"
        "- 'answer' holds the selected option texts (or the free text for text fields).\n"
        "- 'address' points at the anchor (left-column label text) and where the value "
        "lives relative to it: column index, band index within that column, and how many "
        "text blocks right of the anchor.\n"
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
                pass  # unparseable cached responses must be re-spent
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


def drafts_to_fields(drafts: list[BindingDraft]) -> list[Field]:
    fields: list[Field] = []
    for draft in drafts:
        cf = canonical_field(canonical_name(draft.label)) if canonical_name(draft.label) else None
        if cf is None:
            normalizer = "text" if draft.control_type is ControlType.TEXT else "none"
            canonical = None
        else:
            normalizer = cf.normalizer_id
            canonical = cf.name
        selected = [o.text for o in draft.options if o.selected]
        if draft.control_type is ControlType.TEXT:
            value_raw = draft.answers[0] if draft.answers else None
        elif selected:
            value_raw = ", ".join(selected)
        else:
            value_raw = ", ".join(draft.answers) or None
        value_normalized = normalize(normalizer, value_raw, draft.options)
        fields.append(
            Field(
                label_text=draft.label,
                control_type=draft.control_type,
                bbox=draft.bbox,
                options=draft.options,
                answers=draft.answers,
                annotations=draft.annotations,
                region_ref=draft.region_id,
                canonical_name=canonical,
                value_raw=value_raw,
                value_normalized=value_normalized,
                normalizer_id=normalizer,
                provenance=Provenance(
                    source=ProvenanceSource.LLM,
                    binding_id=draft.draft_id,
                    llm_confidence=draft.confidence,
                ),
            )
        )
    return fields

