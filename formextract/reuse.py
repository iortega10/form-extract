"""Deterministic value read for a reused binding (no model, no text matching)."""
from __future__ import annotations

from typing import Any

from .layout import strip_marks
from .model import (
    BindingDraft,
    ControlType,
    Element,
    LayoutResult,
)


def apply_binding(
    binding_drafts: list[BindingDraft],
    target_elements: dict[str, Element] | list[Element],
    layout: LayoutResult,
    config: Any | None = None,
) -> list[BindingDraft]:
    """Fill a reused tab's text answers from that tab's own elements.

    Deterministic, no model, no text matching. The binding shape is left
    intact (labels, options, relational addresses); only ``answers`` for TEXT
    controls is re-read. Checkbox/single-select selections are resolved later
    by ``drafts_to_fields`` from the target tab's own marker geometry.

    Read rule for a replayed TEXT control: resolve every ``source_ref``
    against the target tab's ``layout.bands``, drop the anchor (the ref in the
    binding's own label region) and any mark glyph, then join the remaining
    value-column elements' text (marks stripped). If that yields nothing,
    ``answers`` stays empty and the resolver flags the field
    ``REPLAY_MISMATCH`` — never copied from the exemplar and never silently
    empty.
    """
    del config  # reserved for a future per-control read policy

    if isinstance(target_elements, list):
        elements_by_id = {e.element_id: e for e in target_elements}
    else:
        elements_by_id = target_elements

    region_by_id = {r.region_id: r for r in layout.regions}
    glyph_ids = {g.element_id for g in layout.glyphs}

    def resolve(ref) -> str | None:
        region = region_by_id.get(ref.region_id)
        if region is None:
            return None
        bands = layout.bands.get(region.bbox.page * 1000 + region.column)
        if bands is None or ref.band_id < 0 or ref.band_id >= len(bands):
            return None
        band = bands[ref.band_id]
        if ref.segment_index < 0 or ref.segment_index >= len(band):
            return None
        return band[ref.segment_index]

    def value_texts_of(refs) -> list[str]:
        texts: list[str] = []
        for ref in refs:
            element_id = resolve(ref)
            if element_id is None or element_id in glyph_ids:
                continue
            element = elements_by_id.get(element_id)
            if element is None:
                continue
            text = strip_marks(element.text)
            if text:
                texts.append(text)
        return texts

    for draft in binding_drafts:
        if draft.control_type is not ControlType.TEXT:
            continue
        if draft.answer_refs:
            # 0.6.0-L1b lines contract: ``answer_refs`` name the value cells
            # directly, so the value is re-read from the TARGET tab even when
            # the label region holds the whole row (the one-region defect the
            # label-region drop rule has). The JSON path leaves ``answer_refs``
            # empty and keeps that rule byte-identically.
            value_texts = value_texts_of(list(draft.answer_refs))
        else:
            value_texts = value_texts_of(
                [
                    ref
                    for ref in draft.source_refs
                    if ref.region_id != draft.region_id  # drop the label element
                ]
            )
        draft.answers = [" ".join(value_texts)] if value_texts else []

    return binding_drafts
