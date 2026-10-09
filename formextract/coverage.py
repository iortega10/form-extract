"""Deterministic, model-free row coverage over a resolved layout.

The denominator is perception: one unit per anchor row plus one per GRID
region on the tab. The numerator is the model's resolved refs: a unit counts
as consumed when any element of its own band appears in some field's resolved
``source_elements``. Unresolved refs (``Field.unresolved_source_refs``) never
count. This is deliberately over-inclusive: a field that cites an annotation
element living in the anchor's own band counts the row, which errs toward
fewer false alarms.

Nothing here reads element text: only element ids and band membership.
"""
from __future__ import annotations

from .model import RegionType, TabCoverage, UnboundUnit

#: Cap on ``TabCoverage.unbound`` entries; ``unbound_truncated`` marks the cut.
UNBOUND_CAP = 50


def _anchor_page(anchor_id: str) -> int:
    prefix = anchor_id.split(":", 1)[0]
    try:
        return int(prefix[1:])
    except (ValueError, IndexError):
        return 0


def _pages(layout, elements_by_id) -> list[int]:
    if elements_by_id:
        pages = {element.bbox.page for element in elements_by_id.values()}
    else:
        pages = {key // 1000 for key in layout.bands}
    return sorted(pages)


def _tab_name(page: int, page_tabs, tabs) -> str:
    if page_tabs is not None:
        return page_tabs.get(page, f"page {page}")
    if tabs is not None and page < len(tabs):
        return tabs[page]
    return f"page {page}"


def _band_elements(layout, page: int, column: int, band_id: int) -> list[str]:
    bands = layout.bands.get(page * 1000 + column, [])
    if 0 <= band_id < len(bands):
        return bands[band_id]
    return []


def _units_for_page(layout, page: int) -> list[tuple[int, int, str, bool, list[str]]]:
    """(column, band, region_id, is_grid, element_ids) for one page, sorted."""
    units: list[tuple[int, int, str, bool, list[str]]] = []
    for anchor in layout.anchors:
        if _anchor_page(anchor.anchor_id) != page:
            continue
        column = anchor.relative_address.column
        band = anchor.relative_address.row_band
        units.append(
            (column, band, anchor.region_id, False, _band_elements(layout, page, column, band))
        )
    for region in layout.regions:
        if region.type is not RegionType.GRID or region.bbox.page != page:
            continue
        elements: list[str] = []
        for band_id in region.band_ids:
            elements.extend(_band_elements(layout, page, region.column, band_id))
        band = region.band_ids[0] if region.band_ids else -1
        units.append((region.column, band, region.region_id, True, elements))
    units.sort(key=lambda unit: (unit[0], unit[1], unit[2]))
    return units


def _consumed_element_ids(layout, fields) -> set[str]:
    """Element ids a run's resolved refs point at.

    Only ``Field.source_elements`` counts. An unresolved ref
    (``Field.unresolved_source_refs``) contributes nothing, so a dangling
    pointer can never inflate coverage.
    """
    return {eid for field in (fields or []) for eid in field.source_elements}


def _is_excluded(elements: list[str], non_answer: set[str]) -> bool:
    """True when every element of a unit is a non-answer id (vacuously, if none)."""
    return all(eid in non_answer for eid in elements)


def _ratio(units_consumed: int, units_total: int) -> float | None:
    """Consumed share of a tab, or ``None`` when the tab has no units."""
    return round(units_consumed / units_total, 4) if units_total else None


def compute_coverage(
    layout,
    elements_by_id,
    fields,
    *,
    page_tabs=None,
    tabs=None,
    non_answer_element_ids=None,
    chunk_info=None,
    dispositions=None,
) -> list[TabCoverage]:
    """Per-tab row coverage for the fields a run actually resolved.

    Units are (anchor row) and (GRID region). A unit whose elements are all
    non-answer ids is excluded from the denominator (a unit with no elements is
    excluded too); if that leaves no units the tab's ``ratio`` is ``None``
    (ineligible, never ``1.0``). ``chunk_info`` maps a page to the anchor count
    of its largest authoring chunk; absent, ``max_anchors_per_tab`` is the tab's
    own anchor count and ``chunked`` is ``False``.

    ``dispositions`` (0.6.1-C) is the lines contract's ``Disposition`` list, or
    ``None`` for a run whose contract has no dispositions (json). With a list,
    each tab also carries ``dispositions`` (the distinct lattice rows its model
    dispositioned as non-fields) and ``model_declined`` (the tab has anchors, the
    model returned at least one disposition and no field). With ``None`` both are
    the "no information" value (``0`` and ``None``). Neither changes any status.
    """
    resolved_ids = _consumed_element_ids(layout, fields)
    non_answer = set(non_answer_element_ids or ())
    chunk_info = chunk_info or {}
    fields = fields or []

    coverage: list[TabCoverage] = []
    for page in _pages(layout, elements_by_id):
        tab = _tab_name(page, page_tabs, tabs)
        counted: list[tuple[int, int, str, bool, list[str]]] = []
        for unit in _units_for_page(layout, page):
            if _is_excluded(unit[4], non_answer):
                continue
            counted.append(unit)

        anchors = sum(1 for unit in counted if not unit[3])
        grid_regions = sum(1 for unit in counted if unit[3])
        units_total = len(counted)
        units_consumed = sum(
            1 for unit in counted if any(eid in resolved_ids for eid in unit[4])
        )

        unbound = [
            UnboundUnit(region_id=unit[2], band_id=unit[1])
            for unit in counted
            if not any(eid in resolved_ids for eid in unit[4])
        ]
        unbound_truncated = len(unbound) > UNBOUND_CAP

        if dispositions is None:
            tab_disposition_rows = 0
            model_declined: bool | None = None
        else:
            tab_dispositions = [d for d in dispositions if d.page == page]
            tab_disposition_rows = len(
                {row for d in tab_dispositions for row in d.rows}
            )
            model_declined = bool(
                anchors > 0
                and tab_dispositions
                and not any(field.tab == tab for field in fields)
            )

        largest_chunk = chunk_info.get(page)
        coverage.append(
            TabCoverage(
                tab=tab,
                units_total=units_total,
                units_consumed=units_consumed,
                ratio=_ratio(units_consumed, units_total),
                anchors=anchors,
                grid_regions=grid_regions,
                chunked=largest_chunk is not None,
                max_anchors_per_tab=(
                    int(largest_chunk) if largest_chunk is not None else anchors
                ),
                unbound=unbound[:UNBOUND_CAP],
                unbound_truncated=unbound_truncated,
                dispositions=tab_disposition_rows,
                model_declined=model_declined,
            )
        )
    return coverage
