#!/usr/bin/env python3
"""Profile one workbook's deterministic stages and prompt sizes.

Stdlib only, no network. The input file is read and profiled; this tool does
not write anything to the repo and prints only aggregate/per-chunk numbers and
tab-position labels, not cell text. Chunking mirrors ``Pipeline.run``: hidden
sheets are skipped unless ``--include-hidden``, and each chunk is named after
its own page's sheet.

Usage: python tools/profile_workbook.py <path.xlsx|path.pdf>
                                      [--include-hidden] [--no-names]
"""
from __future__ import annotations

import argparse
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from formextract.ingest import ingest
from formextract.layout import (
    LayoutResult,
    analyze,
    page_anchor_multiset,
    page_geometry_signature,
)
from formextract.pipeline import page_tabs_for
from formextract.resolve import ProjectionChunk, build_prompt, project_chunks


@dataclass
class Profile:
    elements: list
    layout: LayoutResult
    page_tabs: dict[int, str]
    chunks: list[ProjectionChunk]
    timings: dict[str, float] = field(default_factory=dict)


def profile(path: str | Path, *, include_hidden: bool = False) -> Profile:
    """Ingest and project one file the way ``Pipeline.run`` chunks it.

    Hidden sheets are dropped (elements and tab list) unless ``include_hidden``,
    and chunk names come from :func:`formextract.pipeline.page_tabs_for`.
    """
    started = time.perf_counter()
    ing = ingest(path)
    ingest_seconds = time.perf_counter() - started

    hidden_names = {
        name for name, state in ing.sheet_state.items() if state != "visible"
    }
    elements = ing.elements
    if ing.sheet_state and not include_hidden and hidden_names:
        elements = [e for e in elements if e.sheet not in hidden_names]
    if ing.sheet_state:
        tabs = (
            list(ing.sheet_names)
            if include_hidden
            else [n for n in ing.sheet_names if n not in hidden_names]
        )
    else:
        tabs = ing.sheet_names or [
            f"page {i}" for i in range(ing.page_count or 0)
        ]
    page_tabs = page_tabs_for(ing)

    started = time.perf_counter()
    layout = analyze(elements)
    analyze_seconds = time.perf_counter() - started

    started = time.perf_counter()
    chunks = project_chunks(layout, elements, tabs, page_tabs=page_tabs)
    project_seconds = time.perf_counter() - started

    return Profile(
        elements=elements,
        layout=layout,
        page_tabs=page_tabs,
        chunks=chunks,
        timings={
            "ingest": ingest_seconds,
            "analyze": analyze_seconds,
            "project": project_seconds,
        },
    )


def reuse_misses(prof: Profile) -> list[tuple[int, int, int, float]]:
    """Per tab pair: (page_a, page_b, symmetric-difference anchors, jaccard).

    Only pairs whose geometry signatures are EQUAL but whose anchor multisets
    differ are reported. Counts come from the multisets; no anchor text is read
    out. The reuse rule itself is unchanged (strict equality on both parts).
    """
    by_id = {e.element_id: e for e in prof.elements}
    pages = [
        chunk.page if chunk.page is not None else idx
        for idx, chunk in enumerate(prof.chunks)
    ]
    keys = {
        page: (
            page_geometry_signature(prof.layout, by_id, page),
            page_anchor_multiset(prof.layout, page),
        )
        for page in pages
    }
    misses: list[tuple[int, int, int, float]] = []
    for pos, page_a in enumerate(pages):
        sig_a, multiset_a = keys[page_a]
        counts_a = Counter(multiset_a)
        for page_b in pages[pos + 1 :]:
            sig_b, multiset_b = keys[page_b]
            if sig_a != sig_b or multiset_a == multiset_b:
                continue
            counts_b = Counter(multiset_b)
            symmetric_difference = sum((counts_a - counts_b).values()) + sum(
                (counts_b - counts_a).values()
            )
            union = sum((counts_a | counts_b).values())
            intersection = sum((counts_a & counts_b).values())
            jaccard = intersection / union if union else 1.0
            misses.append((page_a, page_b, symmetric_difference, jaccard))
    return misses


def near_miss_pairs(prof: Profile) -> list[tuple[int, int, int, int, int, int]]:
    """Per equal-geometry tab pair: (page_a, page_b, intersection, union, total,
    symmetric-difference).

    Every pair of tabs whose quantised geometry signatures are equal is
    reported, including identical-anchor pairs (difference 0). The multisets
    come from :func:`page_anchor_multiset` (normalised labels only); no anchor
    text leaves this function, so the caller prints integers alone.
    """
    by_id = {e.element_id: e for e in prof.elements}
    pages = [
        chunk.page if chunk.page is not None else idx
        for idx, chunk in enumerate(prof.chunks)
    ]
    keys = {
        page: (
            page_geometry_signature(prof.layout, by_id, page),
            page_anchor_multiset(prof.layout, page),
        )
        for page in pages
    }
    pairs: list[tuple[int, int, int, int, int, int]] = []
    for pos, page_a in enumerate(pages):
        sig_a, multiset_a = keys[page_a]
        counts_a = Counter(multiset_a)
        for page_b in pages[pos + 1 :]:
            sig_b, multiset_b = keys[page_b]
            if sig_a != sig_b:
                continue
            counts_b = Counter(multiset_b)
            intersection = sum((counts_a & counts_b).values())
            union = sum((counts_a | counts_b).values())
            total = len(multiset_a) + len(multiset_b)
            symmetric_difference = sum((counts_a - counts_b).values()) + sum(
                (counts_b - counts_a).values()
            )
            pairs.append(
                (page_a, page_b, intersection, union, total, symmetric_difference)
            )
    return pairs


def closest_pair_diff(pairs: list[tuple[int, int, int, int, int, int]]) -> int:
    """Smallest anchor symmetric difference among equal-geometry pairs (0 if none)."""
    return min((pair[5] for pair in pairs), default=0)


def pairs_by_diff(
    pairs: list[tuple[int, int, int, int, int, int]],
) -> dict[int, int]:
    """Histogram of anchor symmetric-difference value -> number of pairs."""
    return dict(sorted(Counter(pair[5] for pair in pairs).items()))


def _label(page: int, page_tabs: dict[int, str], no_names: bool) -> str:
    if no_names:
        return f"page {page}"
    return page_tabs.get(page, f"page {page}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="profile hidden sheets too (off by default, as in Pipeline.run)",
    )
    parser.add_argument(
        "--no-names",
        action="store_true",
        help="print page numbers instead of sheet names",
    )
    args = parser.parse_args()

    prof = profile(args.path, include_hidden=args.include_hidden)
    print(f"ingest: {prof.timings['ingest']:.3f}s")
    print(f"analyze: {prof.timings['analyze']:.3f}s")
    print(f"project: {prof.timings['project']:.3f}s")

    print(f"chunks authored: {len(prof.chunks)}")
    for idx, chunk in enumerate(prof.chunks):
        page = chunk.page if chunk.page is not None else idx
        label = _label(page, prof.page_tabs, args.no_names)
        off = len(build_prompt(chunk, include_address=False))
        on = len(build_prompt(chunk, include_address=True))
        print(
            f"  {label}: prompt_chars include_address=False={off} "
            f"include_address=True={on}"
        )

    by_id = {e.element_id: e for e in prof.elements}
    keys = []
    for idx, chunk in enumerate(prof.chunks):
        page = chunk.page if chunk.page is not None else idx
        keys.append(
            (
                page_geometry_signature(prof.layout, by_id, page),
                page_anchor_multiset(prof.layout, page),
            )
        )
    exemplar_tabs = len(set(keys))
    replay_candidate_tabs = len(keys) - exemplar_tabs
    print(
        f"reuse structural: exemplar_tabs={exemplar_tabs} "
        f"replay_candidate_tabs={replay_candidate_tabs}"
    )

    misses = reuse_misses(prof)
    print(f"reuse misses (equal geometry, differing anchors): {len(misses)}")
    for page_a, page_b, symmetric_difference, jaccard in misses:
        label_a = _label(page_a, prof.page_tabs, args.no_names)
        label_b = _label(page_b, prof.page_tabs, args.no_names)
        print(
            f"  {label_a} vs {label_b}: "
            f"anchors_symmetric_difference={symmetric_difference} "
            f"jaccard={jaccard:.3f}"
        )

    pairs = near_miss_pairs(prof)
    print(f"near-miss pairs (equal geometry): {len(pairs)}")
    for page_a, page_b, intersection, union, total, symmetric_difference in pairs:
        label_a = _label(page_a, prof.page_tabs, args.no_names)
        label_b = _label(page_b, prof.page_tabs, args.no_names)
        print(
            f"  {label_a} vs {label_b}: "
            f"anchors_intersection={intersection} anchors_union={union} "
            f"anchors_total={total} "
            f"anchors_symmetric_difference={symmetric_difference}"
        )
    print(f"closest_pair_diff: {closest_pair_diff(pairs)}")
    histogram = pairs_by_diff(pairs)
    print("pairs_by_diff: " + " ".join(f"{d}={c}" for d, c in histogram.items()))


if __name__ == "__main__":
    main()
