from __future__ import annotations

import json
import re
from pathlib import Path

from formextract.model import InstanceStatus
from formextract.pipeline import Pipeline, PipelineConfig
from formextract.resolve import (
    LLMResponse,
    ProjectionChunk,
    author_drafts,
    build_prompt,
)
from formextract.store import Store


def _clean_field(label: str) -> dict:
    return {
        "label": label,
        "control_type": "text",
        "options": [],
        "answer": ["v"],
        "annotations": [],
    }


def _bad_field(label: str) -> dict:
    return {
        "label": label,
        "control_type": "no_such_control",
        "options": [],
        "answer": [],
        "annotations": [],
    }


def _two_tab_workbook(path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    good = wb.active
    good.title = "Good"
    good["A1"] = "Field Good"
    mixed = wb.create_sheet("Mixed")
    mixed["A1"] = "Field Mixed OK"
    mixed["A2"] = "Field Mixed Bad"
    wb.save(path)
    wb.close()
    return path


class _PerTabClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, model: str, params: dict) -> LLMResponse:
        self.prompts.append(prompt)
        tab = re.search(r"Layout projection for `([^`]+)`", prompt).group(1)
        if tab == "Good":
            fields = [_clean_field("Good")]
        else:
            fields = [_clean_field("Mixed OK"), _bad_field("Mixed Bad")]
        return LLMResponse(
            text=json.dumps({"fields": fields}),
            model=model,
            params=params,
            tokens=1,
            latency_ms=0,
        )


def _find(store: Store, prompt: str):
    return store.find_llm_call(
        purpose="cold_binding",
        model="gpt-4o-mini",
        params={"temperature": 0},
        prompt=prompt,
    )


def test_field_error_response_not_indexed_and_resent(tmp_path: Path):
    path = _two_tab_workbook(tmp_path / "two.xlsx")
    store = Store(tmp_path / "store")

    first = _PerTabClient()
    first_record = Pipeline(store, first, PipelineConfig()).run(path)

    assert first_record.status is InstanceStatus.PARTIAL
    assert [f.label_text for f in first_record.fields] == ["Good", "Mixed OK"]
    assert any("field 1" in err for err in first_record.errors), first_record.errors
    assert len(first_record.llm_calls) == 2
    for call in first_record.llm_calls:
        assert (store.root / call.prompt_ref).exists()
        assert (store.root / call.response_ref).exists()

    prompts = {call.prompt_ref: (store.root / call.prompt_ref).read_text(encoding="utf-8")
               for call in first_record.llm_calls}
    good_prompt = next(p for p in prompts.values() if "Layout projection for `Good`" in p)
    mixed_prompt = next(p for p in prompts.values() if "Layout projection for `Mixed`" in p)
    assert _find(store, good_prompt) is not None
    assert _find(store, mixed_prompt) is None

    second = _PerTabClient()
    second_record = Pipeline(store, second, PipelineConfig()).run(path)

    assert second_record.instance_id != first_record.instance_id
    assert second_record.status is InstanceStatus.PARTIAL
    assert len(second.prompts) == 1
    assert "Layout projection for `Mixed`" in second.prompts[0]
    assert [f.label_text for f in second_record.fields] == ["Good", "Mixed OK"]


def test_partial_instance_never_served_from_find_instance(tmp_path: Path):
    path = _two_tab_workbook(tmp_path / "two.xlsx")
    store = Store(tmp_path / "store")

    record = Pipeline(store, _PerTabClient(), PipelineConfig()).run(path)

    assert record.status is InstanceStatus.PARTIAL
    assert store.find_instance(record.idempotency_key) is None


def test_repair_response_with_field_errors_not_indexed(tmp_path: Path):
    store = Store(tmp_path / "store")
    chunk = ProjectionChunk(key="tab", text="content")
    params = {"temperature": 0}

    class Client:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete(self, prompt: str, *, model: str, params: dict) -> LLMResponse:
            self.prompts.append(prompt)
            if "not valid JSON" in prompt:
                text = json.dumps(
                    {"fields": [_clean_field("OK"), _bad_field("Bad")]}
                )
            else:
                text = "not json"
            return LLMResponse(
                text=text, model=model, params=params, tokens=1, latency_ms=0
            )

    first = Client()
    drafts, calls, errors = author_drafts(
        [chunk],
        first,
        model="m",
        params=params,
        store=store,
        backoff_seconds=0,
    )

    assert len(first.prompts) == 2
    assert [d.label for d in drafts] == ["OK"]
    assert any("field 1" in err.reason for err in errors), errors
    assert len(calls) == 2  # original and repair both archived
    for call in calls:
        assert (store.root / call.response_ref).exists()
    assert store.find_llm_call(
        purpose="cold_binding", model="m", params=params, prompt=first.prompts[0]
    ) is None
    assert store.find_llm_call(
        purpose="cold_binding", model="m", params=params, prompt=first.prompts[1]
    ) is None

    second = Client()
    author_drafts(
        [chunk],
        second,
        model="m",
        params=params,
        store=store,
        backoff_seconds=0,
    )
    assert len(second.prompts) == 2
    assert second.prompts[0] == first.prompts[0]
    assert second.prompts[1] == first.prompts[1]


def test_clean_response_is_indexed_and_served(store: Store):
    chunk = ProjectionChunk(key="tab", text="content")
    params = {"temperature": 0}

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, prompt: str, *, model: str, params: dict) -> LLMResponse:
            self.calls += 1
            return LLMResponse(
                text=json.dumps({"fields": [_clean_field("Clean")]}),
                model=model,
                params=params,
                tokens=1,
                latency_ms=0,
            )

    first = Client()
    drafts, calls, errors = author_drafts(
        [chunk], first, model="m", params=params, store=store
    )
    assert first.calls == 1
    assert [d.label for d in drafts] == ["Clean"]
    assert errors == []
    assert (
        store.find_llm_call(
            purpose="cold_binding",
            model="m",
            params=params,
            prompt=build_prompt(chunk),
        )
        is not None
    )

    second = Client()
    drafts, calls, errors = author_drafts(
        [chunk], second, model="m", params=params, store=store
    )
    assert second.calls == 0
    assert [d.label for d in drafts] == ["Clean"]
    assert errors == []
    assert len(calls) == 1
