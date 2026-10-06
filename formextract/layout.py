"""Deterministic perception: column segmentation, row banding, region typing,
glyph classification, candidate hypotheses, candidate anchors."""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from .model import (
    BBox,
    CandidateHypothesis,
    ControlType,
    Element,
    Glyph,
    GlyphKind,
    LayoutResult,
    Region,
    RegionType,
)
from .schema import normalize_label

_MARKS: dict[str, GlyphKind] = {
    "x": GlyphKind.CROSS,
    "X": GlyphKind.CROSS,
    "✗": GlyphKind.CROSS,
    "✕": GlyphKind.CROSS,
    "✓": GlyphKind.CHECK,
    "✔": GlyphKind.CHECK,
    "☑": GlyphKind.BOX,
    "☒": GlyphKind.BOX,
    "■": GlyphKind.BOX,
    "▣": GlyphKind.BOX,
    "□": GlyphKind.BOX,
    "☐": GlyphKind.BOX,
}

_GRID_TOKEN_MIN = 8
_PROSE_CHARS_MIN = 150
_HEADER_TOP_FRACTION = 0.15
_HEADER_SIZE_FACTOR = 1.15
# Same-row value collection: a candidate value band may be at most this many
# times the label band's height, so a full-height annotation block running
# down the page can't be paired with a single-line label row.
_VALUE_HEIGHT_FACTOR = 3.0


@dataclass
class _Band:
    column: int
    index: int
    elements: list[Element]

    @property
    def bbox(self) -> BBox:
        out = self.elements[0].bbox
        for e in self.elements[1:]:
            out = out.union(e.bbox)
        return out

    @property
    def text(self) -> str:
        return " ".join(e.text for e in self.elements)

    @property
    def tokens(self) -> list[str]:
        return self.text.split()


def _median(values: list[float], default: float = 1.0) -> float:
    return statistics.median(values) if values else default


def _segment_columns(elements: list[Element]) -> list[tuple[float, float]]:
    if not elements:
        return []
    by_x = sorted(elements, key=lambda e: (e.bbox.x0, e.bbox.x1))
    inline_gaps: list[float] = []
    for i, a in enumerate(by_x):
        for b in by_x[i + 1 :]:
            if b.bbox.x0 > a.bbox.x1 and a.bbox.overlaps_vertically(b.bbox):
                inline_gaps.append(b.bbox.x0 - a.bbox.x1)
                break
    space = _median(sorted(inline_gaps)[: max(1, len(inline_gaps) // 2)], default=0.0)
    tolerance = max(2.0 * space, 1e-6)
    runs: list[list[float]] = []
    for e in sorted(elements, key=lambda e: e.bbox.x0):
        if runs and e.bbox.x0 - runs[-1][1] <= tolerance:
            runs[-1][1] = max(runs[-1][1], e.bbox.x1)
        else:
            runs.append([e.bbox.x0, e.bbox.x1])
    return [(r[0], r[1]) for r in runs]


def _make_bands(elements: list[Element], columns: list[tuple[float, float]]) -> dict[int, list[_Band]]:
    heights = [e.bbox.height for e in elements if e.bbox.height > 0]
    tol_y = max(0.6 * _median(heights, default=1.0), 1e-6)
    assigned: dict[int, list[Element]] = {i: [] for i in range(len(columns))}
    for e in elements:
        cx = e.bbox.center_x
        target = None
        for i, (x0, x1) in enumerate(columns):
            if x0 <= cx <= x1:
                target = i
                break
        if target is None:
            target = min(
                range(len(columns)),
                key=lambda i: min(abs(cx - columns[i][0]), abs(cx - columns[i][1])),
            )
        assigned[target].append(e)
    bands: dict[int, list[_Band]] = {}
    for col, col_elements in assigned.items():
        ordered = sorted(col_elements, key=lambda e: (e.bbox.center_y, e.bbox.x0))
        raw: list[list[Element]] = []
        last_center: float | None = None
        for e in ordered:
            cy = e.bbox.center_y
            if last_center is None or cy - last_center > tol_y:
                raw.append([e])
            else:
                raw[-1].append(e)
            last_center = cy
        bands[col] = [
            _Band(column=col, index=i, elements=sorted(b, key=lambda e: e.bbox.x0))
            for i, b in enumerate(raw)
        ]
    return bands


def _band_type(
    band: _Band, y_min: float, y_max: float, page_median_size: float
) -> RegionType:
    tokens = band.tokens
    chars = len(band.text.strip())
    short = [t for t in tokens if len(t.strip(".,;:()")) <= 4]
    if tokens and len(tokens) >= _GRID_TOKEN_MIN and len(short) == len(tokens):
        return RegionType.GRID
    if chars >= _PROSE_CHARS_MIN and len(band.elements) <= 2:
        return RegionType.PROSE
    cy = band.bbox.center_y
    if cy < y_min + _HEADER_TOP_FRACTION * max(y_max - y_min, 1e-6):
        sizes = [e.size for e in band.elements if e.size is not None]
        alpha = [c for c in band.text if c.isalpha()]
        caps_ratio = sum(1 for c in alpha if c.isupper()) / len(alpha) if alpha else 0.0
        if sizes and max(sizes) > _HEADER_SIZE_FACTOR * page_median_size:
            return RegionType.HEADER
        if caps_ratio >= 0.6 and len(alpha) >= 8 and len(band.elements) <= 4:
            return RegionType.HEADER
    return RegionType.FIELD_ROW


def _content_region_id(
    page: int, col: int, rtype: RegionType, first_norm: str, used: set[str]
) -> str:
    """Content-derived region id: `p{page}:c{col}:{type}:{first_anchor_norm}`,
    with a numeric disambiguator only on collision. The first band of a region
    is the anchor for identity, so the id is stable for the same file + pipeline
    and independent of tab names."""
    stem = f"p{page}:c{col}:{rtype.value}:{first_norm.replace(' ', '_') or 'empty'}"
    candidate = stem
    n = 2
    while candidate in used:
        candidate = f"{stem}:{n}"
        n += 1
    used.add(candidate)
    return candidate


def _make_regions(
    bands: dict[int, list[_Band]], page: int, y_min: float, y_max: float,
    page_median_size: float, used_ids: set[str],
) -> tuple[list[Region], dict[int, list[RegionType]]]:
    regions: list[Region] = []
    band_types: dict[int, list[RegionType]] = {}
    for col in sorted(bands):
        types = [_band_type(b, y_min, y_max, page_median_size) for b in bands[col]]
        band_types[col] = types
        start = 0
        while start < len(types):
            end = start
            while end + 1 < len(types) and types[end + 1] == types[start]:
                end += 1
            group = bands[col][start : end + 1]
            region_elements = [e for b in group for e in b.elements]
            bbox = region_elements[0].bbox
            for e in region_elements[1:]:
                bbox = bbox.union(e.bbox)
            first_norm = normalize_label(group[0].text)
            regions.append(
                Region(
                    region_id=_content_region_id(
                        page, col, types[start], first_norm, used_ids
                    ),
                    type=types[start],
                    bbox=bbox,
                    column=col,
                    band_ids=list(range(start, end + 1)),
                    element_ids=[e.element_id for e in region_elements],
                )
            )
            start = end + 1
    return regions, band_types


def _classify_glyphs(elements: list[Element], page: int) -> list[Glyph]:
    glyphs: list[Glyph] = []
    for e in elements:
        for token in e.text.split():
            kind = _MARKS.get(token)
            if kind is not None:
                glyphs.append(Glyph(element_id=e.element_id, kind=kind, bbox=e.bbox))
    return glyphs


def _control_guesses(
    value_elements: list[Element], glyphs: list[Glyph]
) -> list[ControlType]:
    tokens: list[str] = []
    glyph_leading = False
    for e in value_elements:
        parts = e.text.split()
        cleaned = [p for p in parts if p not in _MARKS]
        if parts and parts[0] in _MARKS and e is value_elements[0]:
            glyph_leading = True
        tokens.extend(cleaned)
    has_glyph = bool(glyphs)
    if not tokens:
        return [ControlType.BOOL] if has_glyph else [ControlType.TEXT]
    short = [t for t in tokens if len(t.strip(".,;:()")) <= 4]
    if len(tokens) >= _GRID_TOKEN_MIN and len(short) == len(tokens):
        return [ControlType.MULTI_SELECT]
    if has_glyph:
        if glyph_leading and any(len(t.strip(".,;:()")) > 6 for t in tokens):
            return [ControlType.BOOL, ControlType.SINGLE_SELECT]
        if len(tokens) >= 2:
            return [ControlType.SINGLE_SELECT, ControlType.MULTI_SELECT]
        return [ControlType.SINGLE_SELECT, ControlType.TEXT]
    if len(tokens) <= 3 and all(len(t.strip(".,;:()")) <= 6 for t in tokens):
        return [ControlType.SINGLE_SELECT, ControlType.TEXT]
    return [ControlType.TEXT]


def _make_hypotheses(
    bands: dict[int, list[_Band]],
    band_types: dict[int, list[RegionType]],
    regions: list[Region],
    glyphs: list[Glyph],
    tol_y: float,
) -> list[CandidateHypothesis]:
    glyph_elements = {g.element_id for g in glyphs}
    region_of_band: dict[tuple[int, int], Region] = {}
    for region in regions:
        for band_id in region.band_ids:
            region_of_band[(region.column, band_id)] = region
    hypotheses: list[CandidateHypothesis] = []
    counter = 0
    for col in sorted(bands):
        for band in bands[col]:
            if band_types[col][band.index] is not RegionType.FIELD_ROW:
                continue
            value_elements: list[Element] = []
            for other_col in sorted(bands):
                if other_col == col:
                    continue
                for other in bands[other_col]:
                    same_row = (
                        abs(band.bbox.center_y - other.bbox.center_y) <= tol_y
                        and other.bbox.height <= _VALUE_HEIGHT_FACTOR * band.bbox.height
                    )
                    if same_row:
                        value_elements.extend(other.elements)
            value_glyphs = [g for g in glyphs if g.element_id in {e.element_id for e in value_elements}]
            region = region_of_band.get((col, band.index))
            guesses = _control_guesses(value_elements, value_glyphs)
            hypotheses.append(
                CandidateHypothesis(
                    hypothesis_id=f"h{counter}",
                    region_id=region.region_id if region else "",
                    label_element_ids=[e.element_id for e in band.elements],
                    value_element_ids=[e.element_id for e in value_elements],
                    glyph_element_ids=[
                        e.element_id for e in value_elements if e.element_id in glyph_elements
                    ],
                    control_guesses=guesses,
                    ambiguous=len(guesses) > 1,
                )
            )
            counter += 1
    for region in regions:
        if region.type is not RegionType.GRID:
            continue
        first_band = min(region.band_ids)
        label_candidates: list[Element] = []
        for other_col in sorted(bands):
            if other_col >= region.column:
                continue
            for other in bands[other_col]:
                if region.bbox.overlaps_vertically(other.bbox, tolerance=tol_y):
                    label_candidates.extend(other.elements)
                    break
            if label_candidates:
                break
        if not label_candidates:
            for c in [region.column]:
                for other in bands.get(c, []):
                    if other.index < first_band and other.bbox.y1 <= region.bbox.y0:
                        label_candidates = other.elements
        hypotheses.append(
            CandidateHypothesis(
                hypothesis_id=f"h{counter}",
                region_id=region.region_id,
                label_element_ids=[e.element_id for e in label_candidates],
                value_element_ids=list(region.element_ids),
                glyph_element_ids=[
                    eid for eid in region.element_ids if eid in glyph_elements
                ],
                control_guesses=[ControlType.MULTI_SELECT],
                ambiguous=False,
            )
        )
        counter += 1
    return hypotheses


def _anchor_source_column(
    bands: dict[int, list[_Band]], band_types: dict[int, list[RegionType]]
) -> int | None:
    """Pick the column anchors are extracted from: the densest FIELD_ROW
    column, not the leftmost one.

    Raw count, not fraction: on the real-world PDF the three wrong columns
    are *entirely* FIELD_ROW (fraction 1.0, three-way tie) while the true
    label column has interleaved header/grid bands and would lose on
    fraction. Empirically on that document (page 0): c0=5, c1=4, c2=80 (of
    85), c3=16 — raw count picks c2 by a wide margin. Floor of >=2 eligible
    FIELD_ROW bands stops a one-band decorative column from winning;
    leftmost-index tie-break keeps selection deterministic. Falls back to
    the old leftmost behavior when no column clears the floor.
    """
    counts = {
        c: sum(1 for t in band_types[c] if t is RegionType.FIELD_ROW) for c in bands
    }
    eligible = {c: n for c, n in counts.items() if n >= 2}
    if not eligible:
        return min(bands) if bands else None
    best = max(eligible.values())
    return min(c for c, n in eligible.items() if n == best)


def _make_anchors(
    bands: dict[int, list[_Band]],
    band_types: dict[int, list[RegionType]],
    regions: list[Region],
    page: int,
) -> list["Anchor"]:  # noqa: F821
    from .model import Anchor, AnchorLocation

    region_of_band: dict[tuple[int, int], Region] = {}
    for region in regions:
        for band_id in region.band_ids:
            region_of_band[(region.column, band_id)] = region
    occurrence: dict[str, int] = {}
    anchors: list[Anchor] = []
    source = _anchor_source_column(bands, band_types)
    for col in sorted(bands):
        if col != source:
            continue
        for band in bands[col]:
            if band_types[col][band.index] is not RegionType.FIELD_ROW:
                continue
            if not band.elements:
                continue
            norm = normalize_label(band.text)
            if not norm:
                continue
            ordinal = occurrence.get(norm, 0)
            occurrence[norm] = ordinal + 1
            region = region_of_band.get((col, band.index))
            anchors.append(
                Anchor(
                    anchor_id=f"p{page}:{norm}:{ordinal}",
                    normalized_text=norm,
                    occurrence_ordinal=ordinal,
                    region_id=region.region_id if region else "",
                    relative_address=AnchorLocation(
                        column=col, row_band=band.index, ordinal_in_band=0
                    ),
                )
            )
    return anchors


def analyze(elements: list[Element]) -> LayoutResult:
    by_page: dict[int, list[Element]] = {}
    for e in elements:
        by_page.setdefault(e.bbox.page, []).append(e)

    all_regions: list[Region] = []
    all_hypotheses: list[CandidateHypothesis] = []
    all_glyphs: list[Glyph] = []
    all_anchors = []
    columns: dict[int, list[int]] = {}
    bands_out: dict[int, list[list[str]]] = {}
    used_ids: set[str] = set()

    for page in sorted(by_page):
        page_elements = by_page[page]
        page_h = max((e.bbox.y1 for e in page_elements), default=1.0)
        y_min = min((e.bbox.y0 for e in page_elements), default=0.0)
        sizes = [e.size for e in page_elements if e.size is not None]
        page_median_size = _median(sizes, default=0.0)
        page_columns = _segment_columns(page_elements)
        if not page_columns:
            continue
        bands = _make_bands(page_elements, page_columns)
        regions, band_types = _make_regions(
            bands, page, y_min, page_h, page_median_size, used_ids
        )
        heights = [e.bbox.height for e in page_elements if e.bbox.height > 0]
        tol_y = max(0.6 * _median(heights, default=1.0), 1e-6)
        glyphs = _classify_glyphs(page_elements, page)
        hypotheses = _make_hypotheses(bands, band_types, regions, glyphs, tol_y)
        anchors = _make_anchors(bands, band_types, regions, page)

        all_regions.extend(regions)
        all_hypotheses.extend(hypotheses)
        all_glyphs.extend(glyphs)
        all_anchors.extend(anchors)
        for col, col_bands in bands.items():
            key = page * 1000 + col
            columns[key] = [e.element_id for b in col_bands for e in b.elements]
            bands_out[key] = [[e.element_id for e in b.elements] for b in col_bands]

    return LayoutResult(
        page_count=len(by_page),
        columns=columns,
        regions=all_regions,
        bands=bands_out,
        glyphs=all_glyphs,
        hypotheses=all_hypotheses,
        anchors=all_anchors,
    )

