"""Deterministic structure probe: integers-only census of a real workbook.

Runs only ingest + layout (no model, no network, no store writes) and prints a
single JSON object. The object contains integers plus one boolean check field;
no label, answer, element text, address, bbox, sheet name or file name appears
in the output, so the owner can run it on a real workbook outside this repo and
commit the numbers without committing the source data.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..ingest import ingest
from ..layout import analyze
from ..model import CHECKBOX_MARK_FOLLOWS_OPTION, MarkerClass
from ..pipeline import _normalize_checkbox_conventions
from ..resolve import _matching_conventions, _most_specific_convention

ALLOWED_KEYS = {
    "tab_count",
    "hidden_tab_count",
    "markers_right_only",
    "markers_between",
    "markers_left_only",
    "markers_unattached",
    "markers_non_answer",
    "controls_ambiguous",
    "controls_auto_selected",
    "controls_convention_selected",
    "review_flag_count",
    "region_counts_by_tab",
    "markers_total_equals_bucket_sum",
}


def run_probe(path: str | Path, conventions: Any = None) -> dict[str, Any]:
    """Return the integers-only structure census for ``path``.

    ``conventions`` accepts the same shapes as
    ``PipelineConfig.checkbox_conventions`` (instances or dicts), and is used
    only to count how many between-markers a declared convention would select.
    """
    ing = ingest(path)
    elements = ing.elements
    layout = analyze(elements)
    elements_by_id = {e.element_id: e for e in elements}
    conventions = _normalize_checkbox_conventions(conventions)

    glyph_hypothesis: dict[str, Any] = {}
    for hypothesis in layout.hypotheses:
        for glyph_id in hypothesis.glyph_element_ids:
            glyph_hypothesis[glyph_id] = hypothesis

    counts = {cls: 0 for cls in MarkerClass}
    markers_non_answer = 0
    controls_ambiguous = 0
    controls_auto_selected = 0
    controls_convention_selected = 0
    review_flag_count = 0

    for mc in layout.marker_classes:
        cls = mc.marker_class
        counts[cls] = counts.get(cls, 0) + 1

        if cls is MarkerClass.RIGHT_ONLY:
            if len(mc.right_candidate_element_ids) == 1 and not mc.left_candidate_element_ids:
                controls_auto_selected += 1
                continue
            if len(mc.right_candidate_element_ids) > 1:
                controls_ambiguous += 1
                review_flag_count += 1
                continue
            review_flag_count += 1
            continue

        if cls is MarkerClass.BETWEEN:
            hypothesis = glyph_hypothesis.get(mc.marker_element_id)
            label = ""
            if hypothesis is not None:
                label = " ".join(
                    elements_by_id[eid].text
                    for eid in hypothesis.label_element_ids
                    if eid in elements_by_id
                )
            marker = elements_by_id.get(mc.marker_element_id)
            tab = marker.sheet if marker is not None else None
            convention = _most_specific_convention(
                _matching_conventions(conventions, tab, label)
            )
            if convention is None:
                controls_ambiguous += 1
                review_flag_count += 1
                continue
            if convention.convention == CHECKBOX_MARK_FOLLOWS_OPTION:
                supported = bool(mc.left_candidate_element_ids)
            else:
                supported = bool(mc.right_candidate_element_ids)
            if not supported:
                controls_ambiguous += 1
                review_flag_count += 1
                continue
            controls_convention_selected += 1
            continue

        review_flag_count += 1

    total_markers = len(layout.glyphs)
    bucket_sum = (
        counts[MarkerClass.RIGHT_ONLY]
        + counts[MarkerClass.BETWEEN]
        + counts[MarkerClass.LEFT_ONLY]
        + counts[MarkerClass.UNATTACHED]
        + markers_non_answer
    )
    region_counts_by_tab = []
    for page in range(len(ing.sheet_names or [])):
        region_counts_by_tab.append(
            sum(1 for r in layout.regions if r.bbox.page == page)
        )

    return {
        "tab_count": len(ing.sheet_names or []),
        "hidden_tab_count": sum(
            1
            for n in (ing.sheet_names or [])
            if ing.sheet_state.get(n, "visible") != "visible"
        ),
        "markers_right_only": counts[MarkerClass.RIGHT_ONLY],
        "markers_between": counts[MarkerClass.BETWEEN],
        "markers_left_only": counts[MarkerClass.LEFT_ONLY],
        "markers_unattached": counts[MarkerClass.UNATTACHED],
        "markers_non_answer": markers_non_answer,
        "controls_ambiguous": controls_ambiguous,
        "controls_auto_selected": controls_auto_selected,
        "controls_convention_selected": controls_convention_selected,
        "review_flag_count": review_flag_count,
        "region_counts_by_tab": region_counts_by_tab,
        "markers_total_equals_bucket_sum": bucket_sum == total_markers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print an integers-only structure census of a workbook"
    )
    parser.add_argument("path", help="path to a .xlsx workbook")
    parser.add_argument(
        "--conventions",
        default=None,
        help="JSON list of {tab, anchor_pattern, convention} selectors",
    )
    args = parser.parse_args(argv)

    conventions = None
    if args.conventions is not None:
        try:
            conventions = json.loads(args.conventions)
        except json.JSONDecodeError as exc:
            parser.error(f"--conventions is not valid JSON: {exc}")

    try:
        result = run_probe(args.path, conventions)
    except ValueError as exc:
        parser.error(str(exc))

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
