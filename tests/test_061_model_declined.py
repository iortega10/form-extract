"""0.6.1-C: the per-tab coverage record tells "the model labelled every row
non-field" (``model_declined``) apart from "anchors present but unbound".

Hand-derived (a tab of six anchor rows ``Label Row NN | X``; the lines reply is
scripted, no model):

| reply | fields | dispositions (rows) | model_declined | status |
|---|---|---|---|---|
| ``hdr L=0.0`` + ``skip L=1.0`` | 0 | 2 | True | PARTIAL (no-fields rule, not C) |
| ``bool L=0.0 O=0.5`` | 1 | 0 | False | COMPLETE |
| ``end`` only | 0 | 0 | False | PARTIAL (no-fields rule, not C) |
| json contract | - | 0 | None | - |

Part C changes no ``InstanceStatus``, no ``min_coverage`` and adds no error.
"""
from __future__ import annotations

import json

from openpyxl import Workbook

from formextract.model import InstanceStatus, TabCoverage, from_json, to_json
from formextract.pipeline import Pipeline, PipelineConfig
from formextract.resolve import LLMResponse
from formextract.store import Store


class _Client:
    def __init__(self, text, *, finish_reason="stop"):
        self.text = text
        self.finish_reason = finish_reason
        self.prompts: list[str] = []

    def complete(self, prompt, *, model, params):
        self.prompts.append(prompt)
        return LLMResponse(
            text=self.text,
            model=model,
            params=params,
            tokens=1,
            latency_ms=0,
            finish_reason=self.finish_reason,
        )


def _workbook(tmp_path, rows=6):
    path = tmp_path / "model-declined.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Checklist"
    for i in range(rows):
        ws.cell(row=i + 1, column=1, value=f"Label Row {i:02d}")
        ws.cell(row=i + 1, column=6, value="X")
    wb.save(path)
    wb.close()
    return path


def _run(tmp_path, path, text, *, config=None, name="store"):
    store = Store(tmp_path / name)
    record = Pipeline(store, _Client(text), config or PipelineConfig()).run(path)
    return record


def _lines_config():
    return PipelineConfig(model="canned", output_contract="lines")


def test_all_dispositions_true_and_status_unchanged(tmp_path):
    path = _workbook(tmp_path)
    record = _run(
        tmp_path, path, "hdr L=0.0\nskip L=1.0\nend", config=_lines_config()
    )
    (block,) = record.coverage
    assert block.anchors == 6
    assert block.dispositions == 2
    assert block.model_declined is True
    # C changes no status: the PARTIAL comes from the pre-existing no-fields rule.
    assert record.status is InstanceStatus.PARTIAL
    assert record.errors == ["no fields resolved despite candidate hypotheses"]


def test_one_field_is_not_declined(tmp_path):
    path = _workbook(tmp_path)
    record = _run(
        tmp_path, path, "bool L=0.0 O=0.5\nend", config=_lines_config()
    )
    (block,) = record.coverage
    assert block.dispositions == 0
    assert block.model_declined is False
    assert record.status is InstanceStatus.COMPLETE


def test_anchors_present_but_unbound_is_not_declined(tmp_path):
    path = _workbook(tmp_path)
    # A field whose ref does not resolve: there IS a field, nothing is consumed.
    record = _run(
        tmp_path,
        path,
        "bool L=0.0 O=99.9\nend",
        config=_lines_config(),
    )
    (block,) = record.coverage
    assert block.dispositions == 0
    assert block.model_declined is False
    assert block.unbound  # anchors present but unbound


def test_empty_reply_is_not_declined(tmp_path):
    path = _workbook(tmp_path)
    record = _run(tmp_path, path, "end", config=_lines_config())
    (block,) = record.coverage
    assert block.dispositions == 0
    # An empty reply is a different failure: the model returned no disposition.
    assert block.model_declined is False
    assert record.status is InstanceStatus.PARTIAL


def test_json_contract_has_no_disposition_information(tmp_path):
    path = _workbook(tmp_path)
    body = json.dumps({"fields": []})
    record = _run(tmp_path, path, body, config=PipelineConfig(model="canned"))
    (block,) = record.coverage
    assert block.dispositions == 0
    assert block.model_declined is None


#: The TabCoverage members that existed before 0.6.1 (the 0.6.0 shape).
_PRE_0_6_1_TABCOVERAGE_FIELDS = (
    "tab",
    "units_total",
    "units_consumed",
    "ratio",
    "anchors",
    "grid_regions",
    "chunked",
    "max_anchors_per_tab",
    "unbound",
    "unbound_truncated",
)


def test_json_coverage_serialises_to_the_pre_0_6_1_bytes(tmp_path):
    """Part C: a json run's serialised TabCoverage is 0.6.0's byte for byte.

    The two new members are ``omit_if_default``, so on a json run (where the
    information does not exist) they stay at their defaults and the dump omits
    them. Comparing to the same record's 0.6.0 field set proves it: if either
    member were serialised unconditionally the two would differ, and
    ``SCHEMA_VERSION`` would have to move.
    """
    path = _workbook(tmp_path)
    body = json.dumps({"fields": []})
    record = _run(tmp_path, path, body, config=PipelineConfig(model="canned"))
    (block,) = record.coverage

    # The members are present on the object...
    assert block.dispositions == 0
    assert block.model_declined is None

    # ...but absent from the serialised shape: exactly the 0.6.0 keys.
    dump = json.loads(to_json(block))
    assert sorted(dump) == sorted(_PRE_0_6_1_TABCOVERAGE_FIELDS)
    assert "dispositions" not in dump
    assert "model_declined" not in dump

    # The bytes equal a 0.6.0-shaped dump built from only the 0.6.0 members.
    before = {name: getattr(block, name) for name in _PRE_0_6_1_TABCOVERAGE_FIELDS}
    assert to_json(block) == to_json(before)

    # And the whole record's coverage block carries no new key either.
    record_block = json.loads(to_json(record))["coverage"][0]
    assert "dispositions" not in record_block
    assert "model_declined" not in record_block


def test_fields_are_serialised_and_round_trip(tmp_path):
    path = _workbook(tmp_path)
    record = _run(
        tmp_path, path, "hdr L=0.0\nskip L=1.0\nend", config=_lines_config()
    )
    dump = json.loads(to_json(record))
    block = dump["coverage"][0]
    assert block["dispositions"] == 2
    assert block["model_declined"] is True
    back = from_json(type(record), to_json(record))
    assert isinstance(back.coverage[0], TabCoverage)
    assert back.coverage[0].model_declined is True
    assert back.coverage[0].dispositions == 2


def test_no_default_error_from_model_declined(tmp_path):
    path = _workbook(tmp_path)
    for text in ("hdr L=0.0\nskip L=1.0\nend", "end", "bool L=0.0 O=0.5\nend"):
        record = _run(
            tmp_path, path, text, config=_lines_config(), name=f"store-{abs(hash(text))}"
        )
        for error in record.errors:
            assert "declin" not in error.lower()
            assert "coverage" not in error.lower()
