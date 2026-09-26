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
        "(e.g. 'Confirm on NAIC website', parenthetical instructions) are annotations, "
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
    data = _extract_json(text)
    drafts: list[BindingDraft] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for item in data.get("fields", []):
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
        drafts.append(
            BindingDraft(
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
        )
    return drafts


def author_drafts(
    chunks: list[ProjectionChunk],
    llm_client: LLMClient,
    *,
    model: str,
    params: dict[str, Any],
    store,
    regions: list[Region] | None = None,
    purpose: str = "cold_binding",
) -> tuple[list[BindingDraft], list[LLMCall]]:
    region_bbox = {r.region_id: r.bbox for r in (regions or [])}
    drafts: list[BindingDraft] = []
    calls: list[LLMCall] = []
    for chunk in chunks:
        prompt = build_prompt(chunk)
        started = time.monotonic()
        response = llm_client.complete(prompt, model=model, params=params)
        latency = response.latency_ms
        if latency is None:
            latency = int((time.monotonic() - started) * 1000)
        call = store.archive_llm_call(
            purpose=purpose,
            model=response.model,
            params=response.params,
            prompt=prompt,
            response=response.text,
            tokens=response.tokens,
            latency_ms=latency,
        )
        calls.append(call)
        chunk_drafts = parse_drafts(response.text)
        for draft in chunk_drafts:
            if draft.provenance is not None:
                draft.provenance.llm_call_ref = call.call_id
            if draft.region_id and draft.region_id in region_bbox:
                draft.bbox = region_bbox[draft.region_id]
        drafts.extend(chunk_drafts)
    return drafts, calls


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

