"""Page-global row lattice: a derived, never-persisted view over the bands.

Rows are the coordinate system of the 0.6.0 line contract
(``docs/design/0.6.0-line-contract-debate.md`` section 3.6): the model cites a
cell as ``row.seg`` and the resolver translates that back to today's
``(region_id, band_id, segment_index)``. A :class:`RowLattice` is a *view* -- it
is never attached to a :class:`~formextract.model.LayoutResult`, never added to
an ``InstanceRecord`` and never serialised. ``layout.py`` is untouched.

Rule (hearth's fix to "group by y-centre"): rows are built from the page's
**short** bands only, then every other (tall) band is assigned by its **top
edge**. Top-edge assignment never fuses two rows and puts a tall merged label
deterministically on its top row.
"""
from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .layout import _median
from .model import Element, LayoutResult

#: A band is SHORT when its vertical extent is at most this many times the
#: page's median band height. Recorded on every lattice so a later change of
#: the rule is visible in the output.
SHORT_BAND_FACTOR = 1.5

#: The layout's own band tolerance is the y/height tolerance `layout.py`
#: recomputes locally as ``max(0.6 * median(element heights, default 1.0),
#: 1e-6)`` (``layout.py:121`` in ``_make_bands``, also ``:633`` and ``:732``).
#: It is not exported, so the factor and floor are mirrored here against that
#: single source; row grouping then uses the SAME tolerance bands were built
#: with. (Not the x column-gap tolerance at ``layout.py:109``.)
_LAYOUT_TOL_FACTOR = 0.6
_TOL_FLOOR = 1e-6
_DEFAULT_MEDIAN = 1.0

__all__ = [
    "SHORT_BAND_FACTOR",
    "Row",
    "RowLattice",
    "build_rows",
    "build_all_rows",
]

# A band on this page: (column, band_id, y0, y1, element_ids).
_BandInfo = tuple[int, int, float, float, list[str]]


@dataclass(frozen=True)
class Row:
    """One page-global row: its bands and its cells in segment order."""

    index: int
    #: ``(column, band_id)`` ordered by (column x-order, band_id).
    entries: tuple[tuple[int, int], ...]
    #: Element ids of the row's bands, ordered by ``(x0, y0, element_id)``.
    #: Segment ``k`` of the row is ``elements[k]``.
    elements: tuple[str, ...]


@dataclass(frozen=True)
class RowLattice:
    """The page-global row lattice: a frozen view, never persisted."""

    page: int
    rows: tuple[Row, ...]
    #: The band tolerance reused from the layout for this page.
    tolerance: float
    #: The short-band factor in force when this lattice was built.
    short_band_factor: float = SHORT_BAND_FACTOR
    rows_by_element: Mapping[str, int] = field(default_factory=dict, repr=False)
    refs_by_element: Mapping[str, tuple[str, int, int]] = field(
        default_factory=dict, repr=False
    )

    def row_of_element(self, element_id: str) -> int | None:
        """Row index holding ``element_id``, or ``None`` when unplaced."""
        return self.rows_by_element.get(element_id)

    def segment(self, row: Row, k: int) -> str | None:
        """Element id at segment ``k`` of ``row``, or ``None`` when out of range."""
        if 0 <= k < len(row.elements):
            return row.elements[k]
        return None

    def ref_for(self, row: Row, k: int) -> tuple[str, int, int] | None:
        """Translate segment ``k`` of ``row`` back to today's coordinates.

        Returns ``(region_id, band_id, segment_index)`` -- the region that owns
        the band in its column, and the index the existing projection numbers
        the element by *inside the band* -- or ``None`` when the segment is out
        of range or its band has no owning region.
        """
        element_id = self.segment(row, k)
        if element_id is None:
            return None
        return self.refs_by_element.get(element_id)


def _ticker(steps: list[int] | None) -> Callable[[], None]:
    if steps is None:
        return lambda: None

    def tick() -> None:
        steps[0] += 1

    return tick


def _col_x0(band_infos: Iterable[_BandInfo], elements_by_id: Mapping[str, Element]) -> dict[int, float]:
    out: dict[int, float] = {}
    for column, _band_id, _y0, _y1, element_ids in band_infos:
        for element_id in element_ids:
            element = elements_by_id.get(element_id)
            if element is None:
                continue
            if column not in out or element.bbox.x0 < out[column]:
                out[column] = element.bbox.x0
    return out


def _group_by_top_edge(bands: Sequence[_BandInfo], tolerance: float) -> list[list[_BandInfo]]:
    """Group bands into rows: a band joins the open row when its top edge is
    within ``tolerance`` of that row's top edge (the first band's ``y0``)."""
    groups: list[list[_BandInfo]] = []
    for info in sorted(bands, key=lambda b: (b[2], b[3], b[0], b[1])):
        if groups and info[2] - groups[-1][0][2] <= tolerance:
            groups[-1].append(info)
        else:
            groups.append([info])
    return groups


def _assign_tall(
    groups: list[list[_BandInfo]], tall: Sequence[_BandInfo]
) -> tuple[list[_BandInfo], list[_BandInfo]]:
    """Assign each tall band to the group containing its top edge.

    Returns ``(leading, trailing)`` bands whose top edge is above / below every
    group; a top edge in a gap between two groups goes to the smaller (upper)
    index. Groups are mutated in place.
    """
    leading: list[_BandInfo] = []
    trailing: list[_BandInfo] = []
    spans = [(g[0][2], max(b[3] for b in g)) for g in groups]
    for info in sorted(tall, key=lambda b: (b[2], b[3], b[0], b[1])):
        top = info[2]
        if top < spans[0][0]:
            leading.append(info)
            continue
        if top > spans[-1][1]:
            trailing.append(info)
            continue
        target: int | None = None
        for i, (gy0, gy1) in enumerate(spans):
            if gy0 <= top <= gy1:
                target = i
                break
        if target is None:
            # Not contained: the top edge sits in a gap between two rows and
            # goes to the smaller row index (the one above).
            for i, (_gy0, gy1) in enumerate(spans):
                if gy1 < top:
                    target = i
        groups[target].append(info)
    return leading, trailing


def build_rows(
    layout: LayoutResult,
    elements_by_id: Mapping[str, Element],
    *,
    page: int,
    steps: list[int] | None = None,
) -> RowLattice:
    """Build the page-global row lattice for ``page``.

    ``layout`` must be an already-computed :class:`LayoutResult`; this function
    never mutates it. ``elements_by_id`` is the element-id index. When ``steps``
    is given, it is a one-element list incremented once per band-level
    operation, so a caller can assert the cost grows linearly with the number
    of bands. Only elements present in ``elements_by_id`` are placed.
    """
    tick = _ticker(steps)

    band_infos: list[_BandInfo] = []
    for key, bands in layout.bands.items():
        if key // 1000 != page:
            continue
        column = key % 1000
        for band_id, element_ids in enumerate(bands):
            tick()
            present = [elements_by_id[e] for e in element_ids if e in elements_by_id]
            if not present:
                continue
            y0 = min(e.bbox.y0 for e in present)
            y1 = max(e.bbox.y1 for e in present)
            band_infos.append((column, band_id, y0, y1, list(element_ids)))

    heights = [
        e.bbox.height
        for e in elements_by_id.values()
        if e.bbox.page == page and e.bbox.height > 0
    ]
    tolerance = max(_LAYOUT_TOL_FACTOR * _median(heights, default=1.0), _TOL_FLOOR)

    band_heights = [y1 - y0 for (_c, _b, y0, y1, _e) in band_infos if y1 - y0 > 0]
    median_band = statistics.median(band_heights) if band_heights else _DEFAULT_MEDIAN
    threshold = SHORT_BAND_FACTOR * median_band

    short = [bi for bi in band_infos if bi[3] - bi[2] <= threshold]
    tall = [bi for bi in band_infos if bi[3] - bi[2] > threshold]

    groups = _group_by_top_edge(short, tolerance)
    leading: list[_BandInfo] = []
    trailing: list[_BandInfo] = []
    if groups:
        leading, trailing = _assign_tall(groups, tall)
    else:
        # No short band on this page (e.g. every cell merged): group every band
        # by its top edge so the page still gets a reading-order lattice.
        groups = _group_by_top_edge(band_infos, tolerance)

    ordered_groups: list[list[_BandInfo]] = []
    if leading:
        ordered_groups.append(leading)
    ordered_groups.extend(groups)
    if trailing:
        ordered_groups.append(trailing)

    col_x0 = _col_x0(band_infos, elements_by_id)

    owner: dict[tuple[int, int], str] = {}
    for region in layout.regions:
        if region.bbox.page != page:
            continue
        for band_id in region.band_ids:
            owner[(region.column, band_id)] = region.region_id

    rows: list[Row] = []
    rows_by_element: dict[str, int] = {}
    refs_by_element: dict[str, tuple[str, int, int]] = {}
    for index, group in enumerate(ordered_groups):
        entries = sorted(
            {(bi[0], bi[1]) for bi in group},
            key=lambda cb: (col_x0.get(cb[0], 0.0), cb[0], cb[1]),
        )
        ordered: list[tuple[float, float, str]] = []
        for column, band_id in entries:
            raw = layout.bands[page * 1000 + column][band_id]
            for segment_index, element_id in enumerate(raw):
                element = elements_by_id.get(element_id)
                if element is None:
                    continue
                tick()
                ordered.append((element.bbox.x0, element.bbox.y0, element_id))
                region_id = owner.get((column, band_id))
                if region_id is not None and element_id not in refs_by_element:
                    refs_by_element[element_id] = (region_id, band_id, segment_index)
        ordered.sort()
        elements = tuple(element_id for (_x, _y, element_id) in ordered)
        for element_id in elements:
            rows_by_element[element_id] = index
        rows.append(Row(index=index, entries=tuple(entries), elements=elements))

    return RowLattice(
        page=page,
        rows=tuple(rows),
        tolerance=tolerance,
        rows_by_element=rows_by_element,
        refs_by_element=refs_by_element,
    )


def build_all_rows(
    layout: LayoutResult,
    elements_by_id: Mapping[str, Element],
    *,
    steps: list[int] | None = None,
) -> dict[int, RowLattice]:
    """Build a lattice for every page that has bands in ``layout``."""
    pages = sorted({key // 1000 for key in layout.bands})
    return {
        page: build_rows(layout, elements_by_id, page=page, steps=steps) for page in pages
    }
