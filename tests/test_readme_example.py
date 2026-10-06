from __future__ import annotations

import importlib
import json
from pathlib import Path

from formextract.pipeline import Pipeline, PipelineConfig
from formextract.resolve import (
    LLMResponse,
    ProjectionChunk,
    _binding_contract,
    parse_drafts_with_errors,
)
from formextract.store import Store

README = Path(__file__).resolve().parents[1] / "README.md"
MODEL_OUTPUT_MARKER = "<!-- example:model-output -->"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _code_only(block: str) -> str:
    lines = block.splitlines()
    if lines and lines[0].strip() in {"python", "py", "json", "bash", "sh"}:
        lines = lines[1:]
    return "\n".join(lines).strip()


def _fenced_blocks(text: str) -> list[str]:
    return [_code_only(part) for part in text.split("```")[1::2]]


def _model_output_example() -> str:
    text = _readme()
    assert MODEL_OUTPUT_MARKER in text
    after = text.split(MODEL_OUTPUT_MARKER, 1)[1]
    blocks = _fenced_blocks(after)
    assert blocks, "no fenced block after the model-output marker"
    return blocks[0]


def _latency_snippet() -> str:
    for block in _fenced_blocks(_readme()):
        if "store.load_instance(" in block:
            return block
    raise AssertionError("README has no snippet loading an InstanceRecord")


def _keys(node) -> list[str]:
    keys: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            keys.append(key)
            keys.extend(_keys(value))
    elif isinstance(node, list):
        for item in node:
            keys.extend(_keys(item))
    return keys


def test_readme_model_output_example_parses_with_zero_field_errors():
    text = _model_output_example()

    drafts, errors = parse_drafts_with_errors(text, include_address=False)

    assert errors == []
    assert [d.control_type.value for d in drafts] == [
        "single_select",
        "multi_select",
        "bool",
        "text",
    ]
    assert drafts[0].label == "Ship to Canada?"
    assert [o.text for o in drafts[0].options] == ["Yes", "No"]
    assert drafts[0].options[0].selected is True
    assert drafts[3].answers == ["J. Rivera, 2026-01-05"]
    assert drafts[0].address is None


def test_readme_model_output_example_uses_only_contract_keys():
    contract = _binding_contract(include_address=False)

    assert '"address"' not in contract
    used = _keys(json.loads(_model_output_example()))
    assert "fields" in used
    for key in used:
        assert (
            f'"{key}"' in contract
        ), f"{key!r} is not in the include_address=False contract"


class _TinyClient:
    def complete(self, prompt: str, *, model: str, params: dict) -> LLMResponse:
        return LLMResponse(
            text=json.dumps(
                {
                    "fields": [
                        {
                            "label": "Reviewed by",
                            "control_type": "text",
                            "options": [],
                            "answer": ["J. Rivera"],
                            "annotations": [],
                            "confidence": 0.5,
                        }
                    ]
                }
            ),
            model=model,
            params=params,
            tokens=321,
            latency_ms=7,
        )


def _tiny_workbook(path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Tiny"
    ws["A1"] = "Reviewed by"
    wb.save(path)
    wb.close()
    return path


def test_readme_latency_snippet_prints_stored_latency_and_tokens(tmp_path, capsys):
    workbook = _tiny_workbook(tmp_path / "tiny.xlsx")
    store = Store(tmp_path / "store")
    record = Pipeline(store, _TinyClient(), PipelineConfig()).run(workbook)
    assert record.llm_calls
    assert store.load_instance(record.instance_id) is not None

    snippet = _latency_snippet()
    assert "latency_ms" in snippet
    assert "tokens" in snippet
    source = snippet.replace(
        '".formextract-store"', json.dumps(str(store.root))
    ).replace('"INSTANCE_ID"', json.dumps(record.instance_id))

    exec(compile(source, str(README), "exec"), {})

    printed = capsys.readouterr().out.strip().splitlines()
    assert printed == [
        f"{call.call_id} {call.latency_ms} {call.tokens}" for call in record.llm_calls
    ]
    assert printed[0].endswith("7 321")
