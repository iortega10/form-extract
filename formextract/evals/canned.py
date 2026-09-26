"""Canned LLM bindings for the spec-fragment golden items (hermetic eval runs)."""
from __future__ import annotations

import json
from typing import Any

from ..resolve import LLMResponse

SPEC_FRAGMENT_FIELDS: list[dict[str, Any]] = [
    {
        "label": "Which US states do they write in",
        "control_type": "multi_select",
        "options": [{"text": "All", "selected": True}],
        "answer": ["ALL"],
        "annotations": ["if not all please check those that apply"],
        "confidence": 0.93,
    },
    {
        "label": "Do they write in Canada?",
        "control_type": "single_select",
        "options": [{"text": "Yes", "selected": False}, {"text": "No", "selected": True}],
        "answer": ["No"],
        "annotations": [],
        "confidence": 0.97,
    },
    {
        "label": "Carrier License",
        "control_type": "single_select",
        "options": [
            {"text": "Admitted", "selected": False},
            {"text": "Non-Admitted", "selected": True},
        ],
        "answer": ["Non-Admitted"],
        "annotations": ["Confirm on NAIC website"],
        "confidence": 0.95,
    },
    {
        "label": "Loss Runs",
        "control_type": "bool",
        "options": [{"text": "Support Loss Runs", "selected": True}],
        "answer": ["true"],
        "annotations": ["Check if MGU is printing Loss runs from IMS directly"],
        "confidence": 0.9,
    },
]


def spec_fragment_response() -> str:
    return json.dumps({"fields": SPEC_FRAGMENT_FIELDS})


class CannedLLMClient:
    def __init__(self, response_text: str, model: str = "canned", tokens: int = 500):
        self.response_text = response_text
        self.model = model
        self.tokens = tokens

    def complete(self, prompt: str, *, model: str, params: dict[str, Any]) -> LLMResponse:
        return LLMResponse(
            text=self.response_text,
            model=model,
            params=params,
            tokens=self.tokens,
            latency_ms=0,
        )


_CANNED = {
    "spec_fragment_pdf": spec_fragment_response,
    "spec_fragment_xlsx": spec_fragment_response,
}


def client_for(item_id: str) -> CannedLLMClient | None:
    factory = _CANNED.get(item_id)
    return CannedLLMClient(factory()) if factory else None
