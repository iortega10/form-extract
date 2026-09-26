from __future__ import annotations

import pytest

from formextract.ingest import ingest
from formextract.layout import analyze
from formextract.model import ControlType, RelationalAddress
from formextract.resolve import (
    BINDING_OUTPUT_CONTRACT,
    author_drafts,
    build_prompt,
    drafts_to_fields,
    parse_drafts,
    project_chunks,
)
from formextract.evals.canned import spec_fragment_response


def _chunks(path):
    ing = ingest(path)
    layout = analyze(ing.elements)
    tabs = ing.sheet_names or ["page 0"]
    return layout, project_chunks(layout, ing.elements, tabs)


def test_projection_contains_regions_and_bands(spec_pdf):
    layout, chunks = _chunks(spec_pdf)
    assert len(chunks) == 1
    text = chunks[0].text
    assert "### p0:c0:r0 [header]" in text
    assert "### p0:c1:r3 [grid]" in text
    assert "band " in text


def test_build_prompt_carries_contract_and_rules(spec_pdf):
    _, chunks = _chunks(spec_pdf)
    prompt = build_prompt(chunks[0])
    assert BINDING_OUTPUT_CONTRACT in prompt
    assert "annotation" in prompt
    assert "multi_select" in prompt
    assert chunks[0].key in prompt


def test_parse_drafts_from_canned_response():
    drafts = parse_drafts(spec_fragment_response())
    assert len(drafts) == 4
    labels = [d.label for d in drafts]
    assert "Carrier License" in labels
    states = drafts[0]
    assert states.control_type is ControlType.MULTI_SELECT
    assert states.options[0].selected is True
    assert drafts[3].control_type is ControlType.BOOL
    assert drafts[1].address is None


def test_parse_drafts_reads_address():
    text = (
        '{"fields": [{"label": "L", "control_type": "text", "options": [], '
        '"answer": ["v"], "annotations": [], "address": '
        '{"anchor_text": "anchor", "column": 1, "row_band": 2, "offset_right": 3}, '
        '"confidence": 0.5}]}'
    )
    draft = parse_drafts(text)[0]
    assert isinstance(draft.address, RelationalAddress)
    assert draft.address.anchor_id == "anchor"
    assert draft.address.column == 1
    assert draft.address.row_band == 2
    assert draft.address.offset_right == 3


def test_parse_drafts_rejects_garbage():
    with pytest.raises(ValueError):
        parse_drafts("no json here")


def test_drafts_to_fields_canonicalizes_and_normalizes():
    drafts = parse_drafts(spec_fragment_response())
    fields = drafts_to_fields(drafts)
    by_canon = {f.canonical_name: f for f in fields}
    assert set(by_canon) == {
        "us_states_written",
        "writes_in_canada",
        "carrier_license",
        "loss_runs_supported",
    }
    assert by_canon["us_states_written"].value_normalized == "ALL"
    assert by_canon["writes_in_canada"].value_normalized == "false"
    assert by_canon["loss_runs_supported"].value_normalized == "true"
    assert by_canon["carrier_license"].value_normalized == "Non-Admitted"
    assert by_canon["loss_runs_supported"].control_type is ControlType.BOOL


def test_author_drafts_archives_calls(spec_pdf, store):
    layout, chunks = _chunks(spec_pdf)

    class Client:
        def complete(self, prompt, *, model, params):
            from formextract.resolve import LLMResponse

            return LLMResponse(
                text=spec_fragment_response(), model=model, params=params, tokens=42
            )

    drafts, calls = author_drafts(
        chunks,
        Client(),
        model="m",
        params={"temperature": 0},
        store=store,
        regions=layout.regions,
    )
    assert len(drafts) == 4
    assert len(calls) == 1
    assert (store.root / calls[0].prompt_ref).exists()
    assert (store.root / calls[0].response_ref).exists()
    assert all(d.provenance.llm_call_ref == calls[0].call_id for d in drafts)


def test_author_drafts_fills_region_bbox_on_fields(spec_pdf, store):
    import json

    from formextract.model import BBox, RegionType
    from formextract.resolve import LLMResponse

    layout, chunks = _chunks(spec_pdf)
    region = next(r for r in layout.regions if r.type is RegionType.FIELD_ROW)
    response = json.dumps(
        {
            "fields": [
                {
                    "label": "Loss Runs",
                    "control_type": "bool",
                    "options": [{"text": "Support Loss Runs", "selected": True}],
                    "answer": ["true"],
                    "annotations": [],
                    "region_id": region.region_id,
                    "confidence": 0.9,
                }
            ]
        }
    )

    class Client:
        def complete(self, prompt, *, model, params):
            return LLMResponse(text=response, model=model, params=params, tokens=10)

    drafts, _ = author_drafts(
        chunks,
        Client(),
        model="m",
        params={"temperature": 0},
        store=store,
        regions=layout.regions,
    )
    assert drafts[0].region_id == region.region_id
    assert drafts[0].bbox == region.bbox

    fields = drafts_to_fields(drafts)
    assert fields[0].region_ref == region.region_id
    assert fields[0].bbox == region.bbox
    assert fields[0].bbox != BBox()  # no parse-time placeholder zero-bbox
