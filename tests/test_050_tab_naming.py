from __future__ import annotations

import json
import re
from pathlib import Path

from formextract.ingest import ingest
from formextract.layout import analyze
from formextract.model import (
    BBox,
    BindingDraft,
    ControlType,
    Element,
    ElementRef,
    InstanceStatus,
    Option,
)
from formextract.pipeline import Pipeline, PipelineConfig
from formextract.resolve import (
    DECLARED_CONVENTION_TOKEN,
    LLMResponse,
    drafts_to_fields,
)
from formextract.store import Store


def _hidden_first_workbook(path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    hidden = wb.active
    hidden.title = "HiddenFirst"
    hidden["A1"] = "Order Log"
    hidden["F1"] = "X Support Order Log"
    hidden.sheet_state = "hidden"

    visible = wb.create_sheet("VisibleSecond")
    visible["A1"] = "Order Log"
    visible["F1"] = "X Support Order Log"
    wb.save(path)
    wb.close()
    return path


def test_hidden_first_sheet_uses_true_tab_name_in_chunk_and_field(tmp_path: Path):
    path = _hidden_first_workbook(tmp_path / "hidden-first.xlsx")
    store = Store(tmp_path / "store")

    class Client:
        def complete(self, prompt, *, model, params):
            tab = re.search(r"Layout projection for `([^`]+)`", prompt).group(1)
            region = re.search(r"### (p\d+:c\d+:field-row:[^\s]+)", prompt).group(1)
            return LLMResponse(
                text=json.dumps(
                    {
                        "fields": [
                            {
                                "label": f"Field on {tab}",
                                "control_type": "text",
                                "answer": [tab],
                                "annotations": [],
                                "region_id": region,
                            }
                        ]
                    }
                ),
                model=model,
                params=params,
            )

    record = Pipeline(store, Client(), PipelineConfig()).run(path)

    assert record.status is InstanceStatus.COMPLETE
    assert record.tabs == ["VisibleSecond"]
    assert record.hidden_sheets == ["HiddenFirst"]
    assert len(record.fields) == 1
    assert record.fields[0].tab == "VisibleSecond"
    assert record.fields[0].label_text == "Field on VisibleSecond"

    prompt = (store.root / record.llm_calls[0].prompt_ref).read_text(encoding="utf-8")
    assert "Layout projection for `VisibleSecond`" in prompt
    assert "Layout projection for `HiddenFirst`" not in prompt


def _between_marker_elements() -> list[Element]:
    elements = [
        Element(
            element_id="label",
            text="Do they ship to Canada?",
            bbox=BBox(page=1, x0=0, y0=0, x1=60, y1=10),
            sheet="VisibleSecond",
        ),
        Element(
            element_id="yes",
            text="Yes",
            bbox=BBox(page=1, x0=70, y0=0, x1=80, y1=10),
            sheet="VisibleSecond",
        ),
        Element(
            element_id="mark",
            text="X",
            bbox=BBox(page=1, x0=90, y0=0, x1=100, y1=10),
            sheet="VisibleSecond",
        ),
        Element(
            element_id="no",
            text="No",
            bbox=BBox(page=1, x0=110, y0=0, x1=120, y1=10),
            sheet="VisibleSecond",
        ),
    ]
    return elements


def test_hidden_first_sheet_checkbox_convention_matches_true_tab():
    from formextract.model import CHECKBOX_MARK_FOLLOWS_OPTION, CheckboxConvention

    elements = _between_marker_elements()
    layout = analyze(elements)
    by_id = {e.element_id: e for e in elements}

    def ref_for(element_id):
        for key, bands in layout.bands.items():
            column = key % 1000
            for band_id, band in enumerate(bands):
                for segment_index, eid in enumerate(band):
                    if eid == element_id:
                        region = next(
                            r
                            for r in layout.regions
                            if r.column == column and band_id in r.band_ids
                        )
                        return ElementRef(
                            region_id=region.region_id,
                            band_id=band_id,
                            segment_index=segment_index,
                        )
        raise AssertionError(f"no ref for {element_id!r}")

    draft = BindingDraft(
        draft_id="d1",
        label="Do they ship to Canada?",
        control_type=ControlType.SINGLE_SELECT,
        bbox=BBox(),
        options=[Option(text="Yes"), Option(text="No")],
        region_id=ref_for("label").region_id,
        source_refs=[ref_for("label"), ref_for("yes"), ref_for("mark"), ref_for("no")],
        confidence=0.9,
    )

    fields = drafts_to_fields(
        [draft],
        layout=layout,
        elements_by_id=by_id,
        tabs=["VisibleSecond"],
        page_tabs={1: "VisibleSecond"},
        checkbox_conventions=[
            CheckboxConvention(
                tab="VisibleSecond",
                anchor_pattern="ship to canada",
                convention=CHECKBOX_MARK_FOLLOWS_OPTION,
            )
        ],
    )
    (field,) = fields
    assert field.tab == "VisibleSecond"
    assert DECLARED_CONVENTION_TOKEN in field.provenance.heuristic_agreement
    assert field.value_normalized == "true"  # Yes selected, yes_no normalizer
