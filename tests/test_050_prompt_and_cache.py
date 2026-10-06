from __future__ import annotations

import json

from formextract.model import InstanceStatus
from formextract.pipeline import Pipeline, PipelineConfig, compute_cache_key
from formextract.resolve import (
    BINDING_OUTPUT_CONTRACT,
    LLMResponse,
    ProjectionChunk,
    author_drafts,
    build_prompt,
)
from formextract.store import Store


def test_include_address_changes_prompt_text():
    chunk = ProjectionChunk(key="tab", text="content")
    off = build_prompt(chunk)
    on = build_prompt(chunk, include_address=True)

    assert BINDING_OUTPUT_CONTRACT in on
    assert BINDING_OUTPUT_CONTRACT not in off
    assert '"address"' not in off
    assert "text blocks right of the anchor" in on
    assert "text blocks right of the anchor" not in off


def test_include_address_changes_cache_key():
    base = dict(
        content_hash="c",
        pipeline_version="3",
        schema_version="2",
        prompt_version="3",
        model="m",
        params={"temperature": 0},
    )
    off = compute_cache_key(include_address=False, **base)
    on = compute_cache_key(include_address=True, **base)
    assert off != on


def _address_response():
    return json.dumps(
        {
            "fields": [
                {
                    "label": "L",
                    "control_type": "text",
                    "options": [],
                    "answer": ["v"],
                    "annotations": [],
                    "address": {
                        "anchor_text": "anchor",
                        "column": 1,
                        "row_band": 2,
                        "offset_right": 3,
                    },
                    "confidence": 0.5,
                }
            ]
        }
    )


def test_ignored_address_when_include_address_false(store):
    class Client:
        def complete(self, prompt, *, model, params):
            return LLMResponse(text=_address_response(), model=model, params=params)

    drafts, _, _ = author_drafts(
        [ProjectionChunk(key="tab", text="content")],
        Client(),
        model="m",
        params={"temperature": 0},
        store=store,
        include_address=False,
    )
    assert drafts[0].address is None

    drafts, _, _ = author_drafts(
        [ProjectionChunk(key="tab", text="content")],
        Client(),
        model="m",
        params={"temperature": 0},
        store=store,
        include_address=True,
    )
    assert drafts[0].address is not None
    assert drafts[0].address.anchor_id == "anchor"


def test_archive_llm_call_index_false_not_lookup(store):
    call = store.archive_llm_call(
        purpose="cold_binding",
        model="m",
        params={"temperature": 0},
        prompt="PROMPT",
        response="RESPONSE",
        index=False,
    )
    assert (store.root / call.prompt_ref).exists()
    assert (store.root / call.response_ref).exists()
    assert (
        store.find_llm_call(
            purpose="cold_binding",
            model="m",
            params={"temperature": 0},
            prompt="PROMPT",
        )
        is None
    )


def test_transport_failure_not_archived(store):
    chunk = ProjectionChunk(key="tab", text="content")

    class Client:
        def complete(self, prompt, *, model, params):
            raise RuntimeError("down")

    author_drafts(
        [chunk],
        Client(),
        model="m",
        params={"temperature": 0},
        store=store,
        backoff_seconds=0,
    )
    prompt = build_prompt(chunk)
    assert (
        store.find_llm_call(
            purpose="cold_binding",
            model="m",
            params={"temperature": 0},
            prompt=prompt,
        )
        is None
    )


def test_partial_rerun_resends_only_failed_chunk(tmp_path):
    from openpyxl import Workbook

    path = tmp_path / "two.xlsx"
    wb = Workbook()
    good = wb.active
    good.title = "Good"
    good["A1"] = "Field Good"
    bad = wb.create_sheet("Bad")
    bad["A1"] = "Field Bad"
    wb.save(path)
    wb.close()

    store = Store(tmp_path / "store")

    class Client:
        def __init__(self):
            self.prompts = []

        def complete(self, prompt, *, model, params):
            self.prompts.append(prompt)
            if "Layout projection for `Good`" in prompt:
                return LLMResponse(
                    text=json.dumps(
                        {
                            "fields": [
                                {
                                    "label": "Good",
                                    "control_type": "text",
                                    "answer": ["v"],
                                    "annotations": [],
                                }
                            ]
                        }
                    ),
                    model=model,
                    params=params,
                )
            return LLMResponse(text="not json", model=model, params=params)

    first = Client()
    first_record = Pipeline(store, first, PipelineConfig()).run(path)
    assert first_record.status is InstanceStatus.PARTIAL

    second = Client()
    second_record = Pipeline(store, second, PipelineConfig()).run(path)
    assert second_record.status is InstanceStatus.PARTIAL
    assert second_record.instance_id != first_record.instance_id

    good_prompts = [p for p in second.prompts if "Layout projection for `Good`" in p]
    bad_prompts = [p for p in second.prompts if "Layout projection for `Bad`" in p]
    assert good_prompts == []
    assert len(bad_prompts) == 2  # original + repair for the failed chunk only
