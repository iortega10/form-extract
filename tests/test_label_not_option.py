from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from formextract.evals.canned import CannedLLMClient
from formextract.evals.structure_probe import run_probe
from formextract.ingest import ingest
from formextract.layout import analyze
from formextract.model import (
    BBox,
    BindingDraft,
    ControlType,
    ElementRef,
    InstanceRecord,
    InstanceStatus,
    MarkerClass,
    Option,
    PIPELINE_VERSION,
    ReviewReason,
    SourceInfo,
)
from formextract.pipeline import Pipeline, PipelineConfig, compute_cache_key
from formextract.resolve import PROMPT_VERSION, drafts_to_fields
from formextract.schema import SCHEMA_VERSION
from formextract.store import Store


def _make_workbook(path: Path, rows) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Checklist"
    for r, cells in enumerate(rows, start=1):
        for c, text in enumerate(cells, start=1):
            ws.cell(row=r, column=c, value=text)
    wb.save(path)
    wb.close()
    return path


def _analyze(path: Path):
    elements = ingest(path).elements
    return analyze(elements), elements


def _marker_class(path: Path) -> "object":
    layout, _ = _analyze(path)
    assert len(layout.marker_classes) == 1
    return layout.marker_classes[0]


def _resolve_field(path: Path, field_label: str, *, model_options=None, model_selected=None):
    layout, elements = _analyze(path)
    by_id = {e.element_id: e for e in elements}
    label_el = next(e for e in elements if e.text == field_label)
    row_cells = sorted(
        (e for e in elements if e.bbox.y0 == label_el.bbox.y0),
        key=lambda e: e.bbox.x0,
    )

    def ref_for(text: str) -> ElementRef:
        target = next(e for e in elements if e.text == text)
        for key, bands in layout.bands.items():
            col = key % 1000
            for band_id, band in enumerate(bands):
                for seg, eid in enumerate(band):
                    if eid == target.element_id:
                        region = next(
                            r
                            for r in layout.regions
                            if r.column == col and band_id in r.band_ids
                        )
                        return ElementRef(
                            region_id=region.region_id,
                            band_id=band_id,
                            segment_index=seg,
                        )
        raise AssertionError(f"no layout ref for {text!r}")

    if model_options is None:
        model_options = [
            e.text
            for e in row_cells
            if e.text != field_label and e.text not in ("X", "✓")
        ]
    options = [Option(text=t, selected=(t == model_selected)) for t in model_options]

    draft = BindingDraft(
        draft_id="d1",
        label=field_label,
        control_type=ControlType.SINGLE_SELECT,
        bbox=BBox(),
        options=options,
        answers=[],
        region_id=ref_for(field_label).region_id,
        source_refs=[ref_for(e.text) for e in row_cells],
        confidence=0.95,
    )
    fields = drafts_to_fields(
        [draft], layout=layout, elements_by_id=by_id, tabs=["Checklist"]
    )
    return fields[0]


def _single_row_response() -> str:
    return json.dumps(
        {
            "fields": [
                {
                    "label": "Label",
                    "control_type": "single_select",
                    "options": [{"text": "Option", "selected": False}],
                    "answer": [],
                    "annotations": [],
                    "region_id": "p0:c0:field-row:label_x_option",
                    "source_elements": [
                        {
                            "region_id": "p0:c0:field-row:label_x_option",
                            "band_id": 0,
                            "segment_index": 0,
                        },
                        {
                            "region_id": "p0:c0:field-row:label_x_option",
                            "band_id": 0,
                            "segment_index": 1,
                        },
                        {
                            "region_id": "p0:c0:field-row:label_x_option",
                            "band_id": 0,
                            "segment_index": 2,
                        },
                    ],
                    "confidence": 0.9,
                }
            ]
        }
    )


def test_label_x_option_is_right_only(tmp_path):
    path = _make_workbook(tmp_path / "single.xlsx", [["Label", "X", "Option"]])

    mc = _marker_class(path)
    assert mc.marker_class is MarkerClass.RIGHT_ONLY
    assert mc.left_candidate_element_ids == []
    assert len(mc.right_candidate_element_ids) == 1

    client = CannedLLMClient(_single_row_response())
    record = Pipeline(Store(tmp_path / "store"), client, PipelineConfig()).run(path)
    assert record.status is InstanceStatus.COMPLETE
    assert len(record.fields) == 1
    field = record.fields[0]
    assert field.value_normalized == "Option"
    assert [o.selected for o in field.options] == [True]
    assert field.provenance.review_flag is False
    assert field.ambiguity is None


def test_opt_x_opt_with_a_label_stays_between(tmp_path):
    path = _make_workbook(
        tmp_path / "between.xlsx", [["Label", "Opt1", "X", "Opt2"]]
    )

    mc = _marker_class(path)
    assert mc.marker_class is MarkerClass.BETWEEN
    assert len(mc.left_candidate_element_ids) == 1
    assert len(mc.right_candidate_element_ids) == 1

    field = _resolve_field(path, "Label")
    assert field.ambiguity is not None
    assert field.ambiguity.reason == "between_options"
    assert field.provenance.review_flag is True
    assert field.provenance.review_reason is ReviewReason.AMBIGUOUS_MARK
    assert all(o.selected is None for o in field.options)
    assert field.value_normalized is None


def test_label_x_alone_is_unattached(tmp_path):
    path = _make_workbook(tmp_path / "unattached.xlsx", [["Label", "X"]])

    mc = _marker_class(path)
    assert mc.marker_class is MarkerClass.UNATTACHED

    field = _resolve_field(path, "Label")
    assert all(o.selected is None for o in field.options)
    assert field.provenance.review_flag is True
    assert field.value_normalized is None


def test_label_x_two_options_right_is_ambiguous(tmp_path):
    path = _make_workbook(
        tmp_path / "competitor.xlsx", [["Label", "X", "Opt1", "Opt2"]]
    )

    mc = _marker_class(path)
    assert mc.marker_class is MarkerClass.RIGHT_ONLY
    assert mc.left_candidate_element_ids == []
    assert len(mc.right_candidate_element_ids) == 2

    field = _resolve_field(path, "Label")
    assert field.ambiguity is not None
    assert field.ambiguity.reason == "competing_options"
    assert field.provenance.review_flag is True
    assert all(o.selected is None for o in field.options)
    assert field.value_normalized is None


def test_second_text_cell_that_is_not_the_label_is_still_a_candidate(tmp_path):
    # The label is the anchor column's first cell; "Note text" is a *second*
    # text cell, not the label, so it must stay an option-like left candidate.
    path = _make_workbook(
        tmp_path / "note.xlsx", [["Label", "Note text", "X", "Option"]]
    )

    mc = _marker_class(path)
    assert mc.marker_class is MarkerClass.BETWEEN
    assert len(mc.left_candidate_element_ids) == 1
    assert len(mc.right_candidate_element_ids) == 1

    field = _resolve_field(path, "Label")
    assert field.ambiguity is not None
    assert field.ambiguity.reason == "between_options"
    assert field.value_normalized is None


def test_pdf_marker_behaviour_unchanged(spec_pdf):
    layout = analyze(ingest(spec_pdf).elements)
    counts = Counter(m.marker_class for m in layout.marker_classes)
    assert counts[MarkerClass.RIGHT_ONLY] == 2
    assert counts[MarkerClass.BETWEEN] == 2
    assert counts[MarkerClass.LEFT_ONLY] == 0
    assert counts[MarkerClass.UNATTACHED] == 0


def test_pipeline_version_is_in_the_cache_key(tmp_path):
    assert SCHEMA_VERSION == "2"
    assert PROMPT_VERSION == "4"
    # 0.6.0-K1: the one-option kind rule changes a default run's fields.
    # 0.6.1: A/B/D change a default run's output; C's coverage members are
    # omit_if_default, so the record shape does not move and SCHEMA_VERSION
    # stays "2".
    assert PIPELINE_VERSION == "6"

    base = dict(
        content_hash="abc123",
        schema_version=SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        model="canned",
        params={"temperature": 0},
    )
    old = compute_cache_key(pipeline_version="1", **base)
    new = compute_cache_key(pipeline_version=PIPELINE_VERSION, **base)
    assert old != new

    store = Store(tmp_path / "store")
    store.save_instance(
        InstanceRecord(
            instance_id="old-instance",
            schema_version=SCHEMA_VERSION,
            pipeline_version="1",
            run_id="old-run",
            created_at="2024-01-01T00:00:00Z",
            source=SourceInfo(
                content_hash="abc123",
                original_filename="f.xlsx",
                mime="xlsx",
                size=0,
            ),
            status=InstanceStatus.COMPLETE,
            idempotency_key=old,
        )
    )
    assert store.find_instance(new) is None
    assert store.find_instance(old).instance_id == "old-instance"


def test_structure_reproduction_counts(tmp_path):
    rows = []
    for i in range(18):
        rows.append([f"Between {i}", "Opt1", "X", "Opt2"])
    for i in range(9):
        rows.append([f"Single {i}", "X", "Option"])
    for i in range(5):
        rows.append(["X", "Option"])
    path = _make_workbook(tmp_path / "structure.xlsx", rows)

    result = run_probe(path)
    assert result["markers_between"] == 18
    assert result["markers_right_only"] == 14
    assert result["markers_left_only"] == 0
    assert result["markers_unattached"] == 0
    assert result["controls_auto_selected"] == 14
    assert result["controls_ambiguous"] == 18
    assert result["markers_total_equals_bucket_sum"] is True
