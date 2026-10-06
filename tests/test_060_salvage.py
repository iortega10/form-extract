"""0.6.0 T2: truncation salvage (parser-only, one call, unindexed)."""
from __future__ import annotations

import json
import random
from pathlib import Path

from formextract.resolve import (
    LLMResponse,
    ProjectionChunk,
    author_drafts,
    build_prompt,
    parse_drafts_with_errors,
)
from formextract.store import Store

PARAMS = {"temperature": 0}


def _field(label: str, *, answer=None) -> dict:
    return {
        "label": label,
        "control_type": "text",
        "options": [],
        "answer": ["v"] if answer is None else answer,
        "annotations": [],
    }


def _response(fields: list[dict]) -> str:
    return json.dumps({"fields": fields})


def _reasons(errors) -> list[str]:
    return [e if isinstance(e, str) else e.reason for e in errors]


# --- shape corpus -----------------------------------------------------------


def test_truncation_mid_string_keeps_prior_fields():
    text = '{"fields": [' + json.dumps(_field("First")) + ', {"label": "Sec'
    drafts, errors = parse_drafts_with_errors(text)
    assert [d.label for d in drafts] == ["First"]
    assert errors == ["response truncated after 1 fields"]


def test_truncation_mid_key_keeps_prior_fields():
    text = '{"fields": [' + json.dumps(_field("First")) + ', {"lab'
    drafts, errors = parse_drafts_with_errors(text)
    assert [d.label for d in drafts] == ["First"]
    assert errors == ["response truncated after 1 fields"]


def test_truncation_mid_number_keeps_no_partial_field():
    text = '{"fields": [{"label": "A", "control_type": "text", "answer": [12'
    drafts, errors = parse_drafts_with_errors(text)
    assert drafts == []
    assert errors == ["response truncated after 0 fields"]


def test_truncation_mid_object_keeps_no_partial_field():
    text = '{"fields": [{"label": "A", "control_type": "text", "options": []'
    drafts, errors = parse_drafts_with_errors(text)
    assert drafts == []
    assert errors == ["response truncated after 0 fields"]


def test_truncation_after_a_comma_keeps_the_closed_field():
    text = '{"fields": [' + json.dumps(_field("First")) + ","
    drafts, errors = parse_drafts_with_errors(text)
    assert [d.label for d in drafts] == ["First"]
    assert errors == ["response truncated after 1 fields"]


def test_truncation_inside_a_nested_options_array():
    text = (
        '{"fields": [{"label": "A", "control_type": "multi_select", '
        '"options": [{"text": "x"'
    )
    drafts, errors = parse_drafts_with_errors(text)
    assert drafts == []
    assert errors == ["response truncated after 0 fields"]


def test_truncation_at_depth_one_with_zero_complete_fields():
    drafts, errors = parse_drafts_with_errors('{"fields": [')
    assert drafts == []
    assert errors == ["response truncated after 0 fields"]


# --- string / escape awareness ---------------------------------------------


def test_escaped_quote_and_brace_inside_a_string_do_not_derail_the_scan():
    tricky = {
        "label": 'a"b}c',
        "control_type": "text",
        "options": [],
        "answer": ["x{y"],
        "annotations": [],
    }
    second = _field("Second")
    text = _response([tricky, second])
    cut = text.rindex('"Second"')
    drafts, errors = parse_drafts_with_errors(text[:cut])
    assert [d.label for d in drafts] == ['a"b}c']
    assert drafts[0].answers == ["x{y"]
    assert errors == ["response truncated after 1 fields"]


def test_a_fence_is_ignored_when_scanning_for_the_cut():
    body = '{"fields": [' + json.dumps(_field("First")) + ", "
    drafts, errors = parse_drafts_with_errors("```json\n" + body)
    assert [d.label for d in drafts] == ["First"]
    assert errors == ["response truncated after 1 fields"]


# --- salvaged fields equal the untouched prefix -----------------------------


def test_salvaged_fields_equal_the_first_n_of_the_full_response():
    fields = [_field(f"F{i}", answer=[f"v{i}"]) for i in range(5)]
    full = _response(fields)
    # Cut immediately after the third field object closes (before its comma).
    cut = full.index(json.dumps(fields[3])) - 2
    prefix = full[:cut]
    assert prefix.endswith("}")

    full_drafts, full_errors = parse_drafts_with_errors(full)
    part_drafts, part_errors = parse_drafts_with_errors(prefix)

    assert full_errors == []
    assert [d.label for d in full_drafts] == [f["label"] for f in fields]
    assert [d.label for d in part_drafts] == [f["label"] for f in fields[:3]]
    assert [d.answers for d in part_drafts] == [d.answers for d in full_drafts[:3]]
    assert part_errors == ["response truncated after 3 fields"]


# --- balanced-but-invalid is unchanged --------------------------------------


def test_balanced_invalid_response_still_repairs(store: Store):
    chunk = ProjectionChunk(key="tab", text="content")

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, prompt, *, model, params):
            self.calls += 1
            if "not valid JSON" in prompt:
                return LLMResponse(
                    text=_response([_field("Fixed")]), model=model, params=params
                )
            return LLMResponse(
                text='{"fields": [{"label": "A" "control_type": "text"}]}',
                model=model,
                params=params,
            )

    client = Client()
    drafts, calls, errors = author_drafts(
        [chunk], client, model="m", params=PARAMS, store=store, backoff_seconds=0
    )
    assert client.calls == 2  # original (balanced but invalid) + repair
    assert [d.label for d in drafts] == ["Fixed"]
    assert [e for e in errors if "truncated" in e.reason] == []


def test_balanced_invalid_bad_escape_still_repairs():
    # Balanced delimiters, invalid escape: not a cut, so it must raise (repair).
    import pytest

    with pytest.raises(ValueError):
        parse_drafts_with_errors('{"fields": [{"label": "a\\qb"}]}')


def test_trailing_prose_that_breaks_the_slice_still_raises():
    # Extra text after the object, carrying its own brace, breaks the
    # `_extract_json` slice. Depth ends below zero, so it is not a cut and the
    # original ValueError is re-raised -> repair.
    import pytest

    with pytest.raises(ValueError):
        parse_drafts_with_errors('{"fields": []} notes follow }')


# --- one call, never cached -------------------------------------------------


def test_unterminated_costs_one_call_and_is_not_indexed(store: Store):
    chunk = ProjectionChunk(key="tab", text="content")
    truncated = '{"fields": [' + json.dumps(_field("Only"))

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, prompt, *, model, params):
            self.calls += 1
            return LLMResponse(text=truncated, model=model, params=params)

    first = Client()
    drafts, calls, errors = author_drafts(
        [chunk], first, model="m", params=PARAMS, store=store, backoff_seconds=0
    )
    assert first.calls == 1  # no repair call for a cut
    assert [d.label for d in drafts] == ["Only"]
    assert _reasons(errors) == ["response truncated after 1 fields"]
    assert len(calls) == 1
    assert (
        store.find_llm_call(
            purpose="cold_binding", model="m", params=PARAMS, prompt=build_prompt(chunk)
        )
        is None
    )

    second = Client()
    author_drafts(
        [chunk], second, model="m", params=PARAMS, store=store, backoff_seconds=0
    )
    assert second.calls == 1  # a cut is never served from the call cache


def test_zero_field_truncation_costs_one_call(store: Store):
    chunk = ProjectionChunk(key="tab", text="content")

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, prompt, *, model, params):
            self.calls += 1
            return LLMResponse(text='{"fields": [{"label": "', model=model, params=params)

    client = Client()
    drafts, calls, errors = author_drafts(
        [chunk], client, model="m", params=PARAMS, store=store, backoff_seconds=0
    )
    assert client.calls == 1
    assert drafts == []
    assert _reasons(errors) == ["response truncated after 0 fields"]


# --- concurrency ------------------------------------------------------------


def test_salvage_identical_serial_and_workers(tmp_path: Path):
    truncated = '{"fields": [' + json.dumps(_field("Only"))
    chunks = [ProjectionChunk(key=f"tab{i}", text=f"chunk {i}") for i in range(4)]

    class Client:
        def complete(self, prompt, *, model, params):
            return LLMResponse(text=truncated, model=model, params=params)

    serial_drafts, _, serial_errors = author_drafts(
        chunks,
        Client(),
        model="m",
        params=PARAMS,
        store=Store(tmp_path / "serial"),
        max_workers=1,
        backoff_seconds=0,
    )
    parallel_drafts, _, parallel_errors = author_drafts(
        chunks,
        Client(),
        model="m",
        params=PARAMS,
        store=Store(tmp_path / "parallel"),
        max_workers=4,
        backoff_seconds=0,
    )
    assert [d.label for d in serial_drafts] == ["Only"] * 4
    assert [d.label for d in parallel_drafts] == [d.label for d in serial_drafts]
    assert _reasons(serial_errors) == _reasons(parallel_errors)
    assert all(r == "response truncated after 1 fields" for r in _reasons(parallel_errors))


# --- linear time (operation counts, never seconds) --------------------------


class _CountingStr(str):
    """A str that counts every character indexed through ``__getitem__``."""

    def __new__(cls, value: str, box: dict) -> "_CountingStr":
        obj = super().__new__(cls, value)
        obj._box = box
        return obj

    def __getitem__(self, key):
        self._box["ops"] += 1
        return super().__getitem__(key)


def _huge_prefix(n: int, box: dict) -> _CountingStr:
    head = '{"fields": [{"label": "'
    return _CountingStr(head + "a" * (n - len(head)), box)


def test_million_character_unterminated_response_is_linear():
    box = {"ops": 0}
    text = _huge_prefix(1_000_000, box)
    assert len(text) == 1_000_000

    drafts, errors = parse_drafts_with_errors(text)

    assert drafts == []
    assert errors == ["response truncated after 0 fields"]
    assert box["ops"] >= len(text)  # every character is visited
    assert box["ops"] <= 4 * len(text)  # a constant amount of work per character


def test_scan_operation_count_doubles_with_input_size():
    def ops_for(n: int) -> int:
        box = {"ops": 0}
        parse_drafts_with_errors(_huge_prefix(n, box))
        return box["ops"]

    small = ops_for(200_000)
    large = ops_for(400_000)
    assert large < 3 * small


# --- seeded fuzz ------------------------------------------------------------


def test_seeded_fuzz_of_truncations_never_raises():
    rng = random.Random(20261006)
    fields = [_field(f"F{i}", answer=[f"v{i}"]) for i in range(6)]
    full = _response(fields)
    labels = [f["label"] for f in fields]

    for _ in range(500):
        cut = rng.randint(1, len(full))
        text = full[:cut]
        drafts, _errors = parse_drafts_with_errors(text)  # must not raise
        salvaged = [d.label for d in drafts]
        # Every salvaged field is a complete object, so it is a prefix of the
        # full field list - never a torn fragment or a reordered field.
        assert salvaged == labels[: len(salvaged)]
