"""L0 row lattice: fixture matrix, invariants, gold report, cost.

Every workbook here is built in-test with openpyxl (or by the gold generator);
element ids are the ingest's ``<sheet>!<row>:<col>`` and geometry is controlled
with declared column widths so label and control columns split (a wide spacer
next to a small within-row gap). Nothing here touches layout, resolve or the
persisted record.
"""
from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from openpyxl import Workbook

from formextract.ingest import ingest
from formextract.layout import analyze
from formextract.model import (
    BBox,
    Element,
    LayoutResult,
    to_dict,
    to_json,
)
from formextract.rows import (
    SHORT_BAND_FACTOR,
    RowLattice,
    build_all_rows,
    build_rows,
)

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "tools") not in sys.path:
    sys.path.insert(0, str(REPO / "tools"))
import make_gold  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _save(wb: Workbook, root: Path, name: str) -> Path:
    path = root / f"{name}.xlsx"
    wb.save(path)
    return path


def _load(path: Path):
    """Return ``(name, elements_by_id, layout, lattice)`` for one tab."""
    result = ingest(path)
    by_id = {e.element_id: e for e in result.elements}
    layout = analyze(result.elements)
    lattice = build_rows(layout, by_id, page=0)
    return by_id, layout, lattice


# --------------------------------------------------------------------------
# Fixture matrix builders
# --------------------------------------------------------------------------


def _fx_plain(root: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Plain"
    for r in range(1, 4):
        ws.cell(row=r, column=1, value=f"Question {r}?")
        ws.cell(row=r, column=3, value="Yes")
        ws.cell(row=r, column=4, value="No")
    return [("plain", _save(wb, root, "plain"))]


def _fx_tall_merged(root: Path):
    out = []
    for end in (3, 2):  # odd span and even span (the y-centre trap)
        wb = Workbook()
        ws = wb.active
        ws.title = f"Tall{end}"
        ws.column_dimensions["B"].width = 20.0
        ws.merge_cells(start_row=1, start_column=1, end_row=end, end_column=1)
        ws["A1"] = "Merged tall label"
        for i in range(3):
            r = 1 + i
            ws.cell(row=r, column=3, value=f"Ctrl{i}")
            ws.cell(row=r, column=5, value="X")
        out.append((f"tall_merged_{end}", _save(wb, root, f"tall{end}")))
    return out


def _fx_side_by_side(root: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Side"
    ws.column_dimensions["B"].width = 20.0
    ws.column_dimensions["F"].width = 20.0
    for r in range(1, 3):
        ws.cell(row=r, column=1, value=f"Left {r}")
        ws.cell(row=r, column=3, value="Yes")
        ws.cell(row=r, column=5, value="No")
        ws.cell(row=r, column=7, value=f"Right {r}")
        ws.cell(row=r, column=9, value="On")
        ws.cell(row=r, column=11, value="Off")
    return [("side_by_side", _save(wb, root, "side"))]


def _fx_stacked_label(root: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "StackedLabel"
    ws["A1"] = "First part"
    ws["A2"] = "second part"
    ws["C1"] = "answer text"
    return [("stacked_label", _save(wb, root, "stacklabel"))]


def _fx_grid(root: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Grid"
    ws.cell(row=1, column=1, value="Which states do they ship to?")
    for i in range(5):
        for j in range(10):
            ws.cell(row=2 + i, column=2 + j, value=f"s{i * 10 + j}")
    return [("grid", _save(wb, root, "grid"))]


def _fx_gutter(root: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Gutter"
    ws.column_dimensions["B"].width = 20.0  # empty gutter between label and controls
    for r in range(1, 4):
        ws.cell(row=r, column=1, value=f"Label {r}")
        ws.cell(row=r, column=3, value="Yes")
        ws.cell(row=r, column=5, value="No")
    return [("gutter", _save(wb, root, "gutter"))]


def _fx_banner(root: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Banner"
    ws.merge_cells("A1:D1")
    ws["A1"] = "SECTION BANNER"
    for r in range(2, 5):
        ws.cell(row=r, column=1, value=f"Label {r}")
        ws.cell(row=r, column=3, value="Yes")
        ws.cell(row=r, column=4, value="No")
    return [("banner", _save(wb, root, "banner"))]


def _fx_stacked_multi(root: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "StackedMulti"
    ws.cell(row=1, column=1, value="Pick many")
    for i in range(3):
        ws.cell(row=2 + i, column=1, value=f"Option {i}")
    return [("stacked_multi", _save(wb, root, "stackmulti"))]


def _fx_empty_and_one_cell(root: Path):
    wb = Workbook()
    wb.active.title = "Empty"
    empty = _save(wb, root, "empty")
    wb = Workbook()
    ws = wb.active
    ws.title = "OneCell"
    ws["A1"] = "Only cell"
    return [("empty", empty), ("one_cell", _save(wb, root, "onecell"))]


def _fx_varied_heights(root: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Heights"
    ws.column_dimensions["B"].width = 20.0
    ws.merge_cells("A1:A3")
    ws["A1"] = "A tall merged label occupying three rows of height"
    for r in range(1, 5):
        ws.cell(row=r, column=3, value=f"Ctrl{r}")
        ws.cell(row=r, column=5, value="X")
    return [("varied_heights", _save(wb, root, "heights"))]


_FIXTURE_BUILDERS = (
    _fx_plain,
    _fx_tall_merged,
    _fx_side_by_side,
    _fx_stacked_label,
    _fx_grid,
    _fx_gutter,
    _fx_banner,
    _fx_stacked_multi,
    _fx_empty_and_one_cell,
    _fx_varied_heights,
)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("rows")
    fixtures: dict[str, tuple] = {}
    for builder in _FIXTURE_BUILDERS:
        for name, path in builder(root):
            fixtures[name] = _load(path)
    golds: dict[str, tuple] = {}
    for set_name in ("dev", "heldout"):
        gold, _manifest, _ = make_gold.build_set(
            set_name, make_gold.DEFAULT_SEEDS[set_name], root / set_name
        )
        tabs = {
            tab: _load(root / set_name / f"{tab}.xlsx") for tab in gold["tabs"]
        }
        golds[set_name] = (gold, tabs)
    return {"fixtures": fixtures, "golds": golds}


@pytest.fixture(scope="module")
def cases(built):
    """Every fixture workbook and every gold tab, as labelled cases."""
    out = [("fx:" + name, *value) for name, value in built["fixtures"].items()]
    for set_name, (_gold, tabs) in built["golds"].items():
        out.extend((f"{set_name}:{tab}", *value) for tab, value in tabs.items())
    return out


# --------------------------------------------------------------------------
# Invariants (over the fixture matrix AND the gold tabs)
# --------------------------------------------------------------------------


def test_declared_constants_and_types(cases):
    assert SHORT_BAND_FACTOR == 1.5
    for _name, _by_id, _layout, lattice in cases:
        assert isinstance(lattice, RowLattice)
        assert lattice.short_band_factor == SHORT_BAND_FACTOR
        assert lattice.tolerance > 0
        for row in lattice.rows:
            assert isinstance(row.entries, tuple)
            assert isinstance(row.elements, tuple)


def test_every_element_placed_exactly_once(cases):
    for name, by_id, layout, lattice in cases:
        page = lattice.page
        band_elements = {
            eid
            for key, bands in layout.bands.items()
            if key // 1000 == page
            for band in bands
            for eid in band
        }
        placed = [eid for row in lattice.rows for eid in row.elements]
        # each element appears exactly once in the lattice...
        assert len(placed) == len(set(placed)), name
        # ...and every element that belongs to a band is placed.
        assert set(placed) == band_elements, name
        # row_of_element agrees with a linear scan.
        for row in lattice.rows:
            for eid in row.elements:
                assert lattice.row_of_element(eid) == row.index, (name, eid)


def test_row_indices_contiguous_and_ordered_by_top_edge(cases):
    for name, by_id, layout, lattice in cases:
        assert [row.index for row in lattice.rows] == list(range(len(lattice.rows)))
        tops = []
        for row in lattice.rows:
            tops.append(min(by_id[eid].bbox.y0 for eid in row.elements))
        assert tops == sorted(tops), name
        assert len(set(tops)) == len(tops), name  # strictly orderable


def test_gold_and_fixtures_present(cases):
    names = {name for name, *_ in cases}
    assert "fx:plain" in names
    assert "dev:Dev01" in names and "heldout:Hold01" in names
    assert len(cases) >= 30


def test_stable_under_element_insertion_order(cases):
    for name, by_id, layout, lattice in cases:
        shuffled = dict(reversed(list(by_id.items())))
        assert build_rows(layout, shuffled, page=lattice.page) == lattice, name
        # Also when the element stream fed to analyze is reordered.
        reordered = analyze(list(reversed(list(by_id.values()))))
        assert build_rows(reordered, by_id, page=lattice.page) == lattice, name


def test_page_global_rows_stable_under_region_reorder(cases):
    for name, by_id, layout, lattice in cases:
        flipped = replace(layout, regions=list(reversed(layout.regions)))
        assert build_rows(flipped, by_id, page=lattice.page) == lattice, name


def test_deterministic_repeat_build(cases):
    for name, by_id, layout, lattice in cases:
        assert build_rows(layout, by_id, page=lattice.page) == lattice, name
        all_rows = build_all_rows(layout, by_id)
        if lattice.rows:  # a page with no bands is simply absent
            assert all_rows.get(lattice.page) == lattice, name


def test_ref_for_round_trips_through_bands(cases):
    for name, by_id, layout, lattice in cases:
        region_by_id = {r.region_id: r for r in layout.regions}
        for row in lattice.rows:
            for k, eid in enumerate(row.elements):
                ref = lattice.ref_for(row, k)
                assert ref is not None, (name, eid)
                region_id, band_id, segment_index = ref
                region = region_by_id[region_id]
                band_key = region.bbox.page * 1000 + region.column
                assert layout.bands[band_key][band_id][segment_index] == eid, (name, eid)
        # segment() reads the same order and out-of-range returns None.
        for row in lattice.rows:
            assert lattice.segment(row, 0) == row.elements[0]
            assert lattice.segment(row, len(row.elements)) is None
            assert lattice.ref_for(row, len(row.elements)) is None


# --------------------------------------------------------------------------
# Fixture-matrix structure checks
# --------------------------------------------------------------------------


def _rows_of(lattice, ids):
    return sorted({lattice.row_of_element(i) for i in ids})


def test_plain_rows_label_and_options_same_row(built):
    _by_id, _layout, lattice = built["fixtures"]["plain"]
    assert len(lattice.rows) == 3
    for r in range(1, 4):
        rows = _rows_of(
            lattice, [f"Plain!{r}:1", f"Plain!{r}:3", f"Plain!{r}:4"]
        )
        assert rows == [r - 1]


def test_merged_tall_label_top_row_no_fusion(built):
    for name in ("tall_merged_3", "tall_merged_2"):
        sheet = "Tall3" if name.endswith("3") else "Tall2"
        _by_id, _layout, lattice = built["fixtures"][name]
        label_row = lattice.row_of_element(f"{sheet}!1:1")
        control_rows = [lattice.row_of_element(f"{sheet}!{i + 1}:3") for i in range(3)]
        assert label_row == 0, name
        # the three controls occupy three distinct rows 0,1,2; the tall label
        # does NOT fuse rows 1 and 2.
        assert control_rows == [0, 1, 2], name
        assert len(set(control_rows)) == 3, name


def test_two_side_by_side_share_a_row(built):
    _by_id, _layout, lattice = built["fixtures"]["side_by_side"]
    assert len(lattice.rows) == 2
    for r in range(1, 3):
        left = _rows_of(lattice, [f"Side!{r}:1", f"Side!{r}:3", f"Side!{r}:5"])
        right = _rows_of(lattice, [f"Side!{r}:7", f"Side!{r}:9", f"Side!{r}:11"])
        assert left == right == [r - 1]


def test_two_cell_stacked_label_stays_two_rows(built):
    _by_id, _layout, lattice = built["fixtures"]["stacked_label"]
    first = lattice.row_of_element("StackedLabel!1:1")
    second = lattice.row_of_element("StackedLabel!2:1")
    answer = lattice.row_of_element("StackedLabel!1:3")
    assert first != second
    assert first == 0 and second == 1
    assert answer == first


def test_grid_five_rows_with_instruction_row_above(built):
    _by_id, _layout, lattice = built["fixtures"]["grid"]
    option_rows = sorted(
        {lattice.row_of_element(f"Grid!{2 + i}:{2 + j}") for i in range(5) for j in range(10)}
    )
    assert option_rows == list(range(option_rows[0], option_rows[0] + 5))
    label_row = lattice.row_of_element("Grid!1:1")
    assert label_row < option_rows[0]


def test_gutter_label_and_controls_share_a_row(built):
    _by_id, _layout, lattice = built["fixtures"]["gutter"]
    # the gutter really splits label and control columns
    assert {key % 1000 for key in _layout.columns} == {0, 1}
    for r in range(1, 4):
        rows = _rows_of(lattice, [f"Gutter!{r}:1", f"Gutter!{r}:3", f"Gutter!{r}:5"])
        assert rows == [r - 1]


def test_merged_banner_own_top_row(built):
    _by_id, _layout, lattice = built["fixtures"]["banner"]
    banner_row = lattice.row_of_element("Banner!1:1")
    assert banner_row == 0
    assert lattice.rows[0].elements == ("Banner!1:1",)
    assert len(lattice.rows) == 4


def test_stacked_multi_options_consecutive_label_first(built):
    _by_id, _layout, lattice = built["fixtures"]["stacked_multi"]
    option_rows = [lattice.row_of_element(f"StackedMulti!{2 + i}:1") for i in range(3)]
    assert option_rows == [1, 2, 3]
    assert lattice.row_of_element("StackedMulti!1:1") == 0


def test_varied_row_heights_median_small(built):
    _by_id, _layout, lattice = built["fixtures"]["varied_heights"]
    # the median band height is small, so the tall merged label is TALL and the
    # short control rows are not fused by it.
    label_row = lattice.row_of_element("Heights!1:1")
    control_rows = [lattice.row_of_element(f"Heights!{r}:3") for r in range(1, 5)]
    assert label_row == 0
    assert control_rows == [0, 1, 2, 3]


def test_empty_tab_and_one_cell_tab(built):
    _by_id, _layout, empty = built["fixtures"]["empty"]
    assert empty.rows == ()
    assert empty.row_of_element("Empty!1:1") is None
    by_id, _layout2, one = built["fixtures"]["one_cell"]
    assert len(one.rows) == 1
    assert one.rows[0].elements == ("OneCell!1:1",)
    assert one.row_of_element("OneCell!1:1") == 0
    assert set(by_id) == {"OneCell!1:1"}


# --------------------------------------------------------------------------
# Cost: linear in bands, iterative
# --------------------------------------------------------------------------


def _synthetic(n: int):
    elements: dict[str, Element] = {}
    bands: list[list[str]] = []
    columns: list[str] = []
    for i in range(n):
        eid = f"e{i}"
        elements[eid] = Element(
            element_id=eid,
            text=str(i),
            bbox=BBox(page=0, x0=0.0, y0=float(i), x1=1.0, y1=float(i) + 1.0),
        )
        bands.append([eid])
        columns.append(eid)
    layout = LayoutResult(
        page_count=1,
        columns={0: columns},
        regions=[],
        bands={0: bands},
        glyphs=[],
        hypotheses=[],
        anchors=[],
    )
    return layout, elements


def test_cost_linear_in_bands():
    steps_4k: list[int] = [0]
    steps_8k: list[int] = [0]
    layout_4k, elements_4k = _synthetic(4000)
    layout_8k, elements_8k = _synthetic(8000)

    start = time.perf_counter()
    lattice_4k = build_rows(layout_4k, elements_4k, page=0, steps=steps_4k)
    build_rows(layout_8k, elements_8k, page=0, steps=steps_8k)
    elapsed = time.perf_counter() - start

    assert len(lattice_4k.rows) == 4000  # one short band per row
    ratio = steps_8k[0] / steps_4k[0]
    assert ratio < 3.0, ratio  # ~2.0 for a linear pass, not 4.0
    assert steps_8k[0] < 8000 * 10
    assert elapsed < 5.0  # never seconds


# --------------------------------------------------------------------------
# The lattice is a view: never persisted
# --------------------------------------------------------------------------


def test_lattice_is_a_view_never_persisted(tmp_path):
    from formextract.evals.canned import client_for
    from formextract.evals.synthetic import build
    from formextract.pipeline import Pipeline, PipelineConfig
    from formextract.store import Store

    path = build("spec_fragment_xlsx", tmp_path / "fx")
    record = Pipeline(
        Store(tmp_path / "store"), client_for("spec_fragment_xlsx"), PipelineConfig()
    ).run(path)
    before = to_json(record)
    assert "rows" not in before

    result = ingest(path)
    by_id = {e.element_id: e for e in result.elements}
    layout = analyze(result.elements)
    assert not hasattr(layout, "rows")
    assert "rows" not in to_dict(layout)
    lattices = build_all_rows(layout, by_id)
    assert lattices
    # building the lattice leaves every record byte where it was
    assert to_json(record) == before
    assert "rows" not in to_dict(layout)

    import dataclasses

    lattice = next(iter(lattices.values()))
    with pytest.raises(dataclasses.FrozenInstanceError):
        lattice.page = 99  # type: ignore[misc]


# --------------------------------------------------------------------------
# Gold report
# --------------------------------------------------------------------------

_SINGLE_ROW_TAGS = ("yes_no_row", "dense_multi", "text_field", "typed_value")


def _gold_fields(built, tag):
    for set_name, (gold, tabs) in built["golds"].items():
        for field in gold["fields"]:
            if tag in field["tags"] and field["tab"] in tabs:
                yield set_name, field, tabs[field["tab"]][2]


def test_gold_single_row_structures_map_to_one_row(built, capsys):
    for tag in _SINGLE_ROW_TAGS:
        checked = ok = 0
        for _set_name, field, lattice in _gold_fields(built, tag):
            checked += 1
            rows = set()
            for cell in field["label_cells"] + field["option_cells"] + field["answer_cells"]:
                rows.add(lattice.row_of_element(cell))
            if len(rows) == 1:
                ok += 1
        print(f"{tag}: checked={checked} ok={ok}")
        assert checked > 0
        assert ok == checked


def _cell_row(cell_id: str) -> int:
    _tab, rc = cell_id.split("!", 1)
    return int(rc.split(":", 1)[0])


def test_gold_side_by_side_share_a_row(built, capsys):
    checked = ok = 0
    shared: dict[tuple, set] = {}
    for set_name, field, lattice in _gold_fields(built, "side_by_side"):
        checked += 1
        rows = {
            lattice.row_of_element(c)
            for c in field["label_cells"] + field["option_cells"]
        }
        if len(rows) == 1:
            ok += 1
        key = (set_name, field["tab"], _cell_row(field["label_cells"][0]))
        shared.setdefault(key, set()).update(rows)
    print(f"side_by_side: checked={checked} ok={ok}")
    assert checked > 0 and ok == checked
    # the two fields declared on one xlsx row land on ONE lattice row
    for key, rows in shared.items():
        assert len(rows) == 1, key


def test_gold_stacked_multi_options_consecutive(built, capsys):
    checked = ok = 0
    for _set_name, field, lattice in _gold_fields(built, "stacked_multi"):
        checked += 1
        option_rows = [lattice.row_of_element(c) for c in field["option_cells"]]
        label_rows = {lattice.row_of_element(c) for c in field["label_cells"]}
        consecutive = option_rows == list(range(min(option_rows), max(option_rows) + 1))
        label_above = len(label_rows) == 1 and min(label_rows) < min(option_rows)
        if consecutive and label_above:
            ok += 1
    print(f"stacked_multi: checked={checked} ok={ok}")
    assert checked > 0 and ok == checked


def _structural_ok(field, lattice):
    tags = set(field["tags"])
    labels = {lattice.row_of_element(c) for c in field["label_cells"]}
    opts = [lattice.row_of_element(c) for c in field["option_cells"]]
    answers = {lattice.row_of_element(c) for c in field["answer_cells"]}
    if "label_two_rows" in tags:
        return len(labels) == 2
    if "label_two_cells" in tags:
        return len(labels) == 1
    if "merged_tall" in tags:
        return len(labels) == 1
    if "grid_50" in tags:
        return len(set(opts)) == 5 and len(labels) == 1 and min(labels) < min(opts)
    if "matrix" in tags:
        return len(labels | answers) == 1 and len(opts) == 1 and opts[0] < min(labels)
    if {"gutter_column", "non_answer_column", "no_glyph_answer"} & tags:
        return len(labels | set(opts) | answers) == 1
    return True


def test_gold_structural_tags_map_as_expected(built, capsys):
    extra = (
        "label_two_rows",
        "label_two_cells",
        "merged_tall",
        "grid_50",
        "matrix",
        "gutter_column",
        "non_answer_column",
        "no_glyph_answer",
    )
    for tag in extra:
        checked = ok = 0
        for _set_name, field, lattice in _gold_fields(built, tag):
            checked += 1
            if _structural_ok(field, lattice):
                ok += 1
        print(f"{tag}: checked={checked} ok={ok}")
        assert checked > 0 and ok == checked
