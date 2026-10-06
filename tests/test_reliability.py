from __future__ import annotations

import json

from formextract.evals.canned import client_for, spec_fragment_response
from formextract.model import (
    PIPELINE_VERSION,
    InstanceRecord,
    InstanceStatus,
    SourceInfo,
)
from formextract.pipeline import Pipeline, PipelineConfig, compute_cache_key
from formextract.resolve import (
    LLMResponse,
    ProjectionChunk,
    author_drafts,
    build_prompt,
    parse_drafts_with_errors,
)
from formextract.store import Store, content_hash


def test_failed_run_not_replayed(tmp_path, store):
    bad = tmp_path / "form.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    pipe = Pipeline(store, None)
    first = pipe.run(bad)
    second = pipe.run(bad)

    assert first.status is InstanceStatus.FAILED
    assert second.status is InstanceStatus.FAILED
    assert first.instance_id != second.instance_id


def test_partial_run_not_replayed(spec_xlsx):
    store = Store(spec_xlsx.parent / "store")
    pipe = Pipeline(store, None)
    first = pipe.run(spec_xlsx)
    second = pipe.run(spec_xlsx)

    assert first.status is InstanceStatus.PARTIAL
    assert second.status is InstanceStatus.PARTIAL
    assert first.instance_id != second.instance_id


def test_perception_only_then_model(spec_xlsx):
    store = Store(spec_xlsx.parent / "store")
    perception = Pipeline(store, None).run(spec_xlsx)
    assert perception.status is InstanceStatus.PARTIAL

    model_run = Pipeline(
        store, client_for("spec_fragment_xlsx"), PipelineConfig()
    ).run(spec_xlsx)
    assert model_run.status is InstanceStatus.COMPLETE
    assert model_run.fields
    assert model_run.instance_id != perception.instance_id


def test_key_varies_with_model_prompt_params_schema():
    base = {
        "content_hash": "c",
        "pipeline_version": "1",
        "schema_version": "1",
        "prompt_version": "1",
        "model": "m",
        "params": {"temperature": 0},
    }
    keys = {
        compute_cache_key(**base),
        compute_cache_key(**{**base, "model": "other"}),
        compute_cache_key(**{**base, "model": "none"}),
        compute_cache_key(**{**base, "params": {"temperature": 1}}),
        compute_cache_key(**{**base, "params": {"temperature": 0, "top_p": 1}}),
        compute_cache_key(**{**base, "schema_version": "2"}),
        compute_cache_key(**{**base, "prompt_version": "2"}),
        compute_cache_key(**{**base, "content_hash": "d"}),
        compute_cache_key(**{**base, "include_hidden_sheets": True}),
    }
    assert len(keys) == 9


def test_key_stable_under_params_reorder():
    assert compute_cache_key(
        content_hash="c",
        pipeline_version="1",
        schema_version="1",
        prompt_version="1",
        model="m",
        params={"temperature": 0, "top_p": 1},
    ) == compute_cache_key(
        content_hash="c",
        pipeline_version="1",
        schema_version="1",
        prompt_version="1",
        model="m",
        params={"top_p": 1, "temperature": 0},
    )


def test_force_bypasses_complete_hit(spec_pdf, store):
    pipe = Pipeline(store, client_for("spec_fragment_pdf"), PipelineConfig())
    first = pipe.run(spec_pdf)
    second = pipe.run(spec_pdf, force=True)

    assert first.instance_id != second.instance_id
    assert store.find_instance(second.idempotency_key).instance_id == second.instance_id


def test_stored_v1_instance_never_served_for_v2_key(spec_pdf, store):
    chash = content_hash(spec_pdf)
    old = InstanceRecord(
        instance_id="old-instance",
        schema_version="1",
        pipeline_version=PIPELINE_VERSION,
        run_id="old-run",
        created_at="2024-01-01T00:00:00Z",
        source=SourceInfo(
            content_hash=chash, original_filename=spec_pdf.name, mime="pdf", size=1
        ),
        status=InstanceStatus.COMPLETE,
        idempotency_key=f"{chash}:{PIPELINE_VERSION}",
    )
    store.save_instance(old)

    record = Pipeline(store, client_for("spec_fragment_pdf"), PipelineConfig()).run(
        spec_pdf
    )
    assert record.instance_id != "old-instance"


def test_index_contains_only_complete(spec_pdf, spec_xlsx, tmp_path):
    store = Store(tmp_path / "store")
    complete = Pipeline(store, client_for("spec_fragment_pdf"), PipelineConfig()).run(
        spec_pdf
    )
    index = json.loads((store.instances_dir / "index.json").read_text(encoding="utf-8"))
    assert set(index.values()) == {complete.instance_id}

    partial_store = Store(tmp_path / "partial-store")
    partial = Pipeline(partial_store, None).run(spec_xlsx)
    assert partial.status is InstanceStatus.PARTIAL
    assert partial_store.find_instance(partial.idempotency_key) is None
    assert (partial_store.runs_dir / partial.source.content_hash / f"{partial.run_id}.json").exists()


def test_one_bad_chunk_keeps_others(store):
    class Client:
        def complete(self, prompt, *, model, params):
            if "bad chunk" in prompt:
                return LLMResponse(text="not json", model=model, params=params)
            return LLMResponse(
                text=json.dumps(
                    {
                        "fields": [
                            {
                                "label": "F",
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

    chunks = [
        ProjectionChunk(
            key=f"tab{i}",
            text=("bad chunk" if i == 2 else f"good chunk {i}"),
        )
        for i in range(5)
    ]
    drafts, calls, errors = author_drafts(
        chunks,
        Client(),
        model="m",
        params={"temperature": 0},
        store=store,
        backoff_seconds=0,
    )

    assert len(drafts) == 4
    assert len(calls) == 6  # 4 good + initial/repair for the bad chunk
    assert len(errors) == 1
    assert errors[0].chunk_id == "2"
    assert errors[0].tab == "tab2"


def test_bad_field_keeps_chunk():
    text = json.dumps(
        {
            "fields": [
                {"label": "Good", "control_type": "text", "answer": ["v"], "annotations": []},
                {"label": "Bad", "control_type": "not_a_type", "answer": [], "annotations": []},
            ]
        }
    )
    drafts, errors = parse_drafts_with_errors(text)
    assert len(drafts) == 1
    assert drafts[0].label == "Good"
    assert len(errors) == 1
    assert "field 1" in errors[0]


def test_json_retry_then_success(store):
    class Client:
        def __init__(self):
            self.calls = 0

        def complete(self, prompt, *, model, params):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(text="not json", model=model, params=params)
            return LLMResponse(text=spec_fragment_response(), model=model, params=params)

    chunks = [ProjectionChunk(key="tab", text="content")]
    client = Client()
    drafts, calls, errors = author_drafts(
        chunks,
        client,
        model="m",
        params={"temperature": 0},
        store=store,
        backoff_seconds=0,
    )

    assert client.calls == 2
    assert len(drafts) == 4
    assert len(calls) == 2
    assert errors == []


def test_retry_budget_enforced(store):
    class Client:
        def __init__(self):
            self.calls = 0

        def complete(self, prompt, *, model, params):
            self.calls += 1
            return LLMResponse(text="not json", model=model, params=params)

    chunks = [ProjectionChunk(key="tab", text="content")]
    client = Client()
    drafts, calls, errors = author_drafts(
        chunks,
        client,
        model="m",
        params={"temperature": 0},
        store=store,
        backoff_seconds=0,
        call_budget=2,
    )

    assert client.calls == 2
    assert drafts == []
    assert len(calls) == 2
    assert len(errors) == 1
    assert "parse failed after repair" in errors[0].reason


def test_rerun_after_partial_reuses_archived_calls(store):
    class Client:
        def __init__(self):
            self.calls = 0
            self.prompts = []

        def complete(self, prompt, *, model, params):
            self.calls += 1
            self.prompts.append(prompt)
            if "good chunk" in prompt:
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

    chunks = [
        ProjectionChunk(key="good", text="good chunk"),
        ProjectionChunk(key="bad", text="bad chunk"),
    ]
    first = Client()
    author_drafts(
        chunks,
        first,
        model="m",
        params={"temperature": 0},
        store=store,
        backoff_seconds=0,
    )
    assert first.calls == 3  # good + bad initial + bad repair

    second = Client()
    drafts, calls, errors = author_drafts(
        chunks,
        second,
        model="m",
        params={"temperature": 0},
        store=store,
        backoff_seconds=0,
    )
    # The good chunk is served from the call cache; the failed chunk's original
    # and repair prompts were not indexed, so only those two prompts are re-sent.
    assert second.calls == 2
    assert len(drafts) == 1
    assert len(calls) == 3  # good cached + bad original + bad repair
    assert len(errors) == 1
    assert errors[0].chunk_id == "1"
    assert sum("good chunk" in p for p in second.prompts) == 0
    assert sum("bad chunk" in p for p in second.prompts) == 2


def test_cached_bad_original_from_040_is_a_cache_miss(store):
    chunk = ProjectionChunk(key="tab", text="content")
    prompt = build_prompt(chunk)
    # Simulate a 0.4.0-era entry: archived under the old rule that indexed
    # every response, including unparseable ones.
    store.archive_llm_call(
        purpose="cold_binding",
        model="m",
        params={"temperature": 0},
        prompt=prompt,
        response="not json",
    )

    class Client:
        def __init__(self):
            self.prompts = []

        def complete(self, prompt, *, model, params):
            self.prompts.append(prompt)
            return LLMResponse(
                text=json.dumps(
                    {
                        "fields": [
                            {
                                "label": "F",
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

    client = Client()
    drafts, calls, errors = author_drafts(
        [chunk],
        client,
        model="m",
        params={"temperature": 0},
        store=store,
        backoff_seconds=0,
    )

    # The stale bad entry is ignored as a cache miss: the original prompt is
    # re-sent (not skipped straight to the repair prompt) and the fresh good
    # response replaces the stale lookup entry.
    assert len(client.prompts) == 1
    assert "Parse error" not in client.prompts[0]
    assert len(drafts) == 1
    assert errors == []
    assert len(calls) == 1  # fresh original only; stale entry was not served


def test_empty_sheet_complete_is_cached(tmp_path):
    from openpyxl import Workbook

    path = tmp_path / "empty.xlsx"
    wb = Workbook()
    wb.save(path)

    store = Store(tmp_path / "store")
    pipe = Pipeline(store, client_for("spec_fragment_xlsx"), PipelineConfig())
    first = pipe.run(path)
    second = pipe.run(path)

    assert first.status is InstanceStatus.COMPLETE
    assert first.instance_id == second.instance_id
