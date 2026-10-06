"""Canned LLM bindings for the spec-fragment golden items (hermetic eval runs)."""
from __future__ import annotations

import json
from typing import Any

from ..resolve import LLMResponse

_LABEL_REGION = "p0:c0:field-row:which_us_states_do_they_ship_to"
_X_ALL_REGION = "p0:c1:field-row:x_all"
_YES_NO_REGION = "p0:c1:field-row:yes_x_no"


SPEC_FRAGMENT_FIELDS: list[dict[str, Any]] = [
    {
        "label": "Which US states do they ship to",
        "control_type": "multi_select",
        "options": [{"text": "All", "selected": True}],
        "answer": ["ALL"],
        "annotations": ["if not all please check those that apply"],
        "region_id": _LABEL_REGION,
        "source_elements": [
            {"region_id": _LABEL_REGION, "band_id": 1, "segment_index": 0},
            {"region_id": _X_ALL_REGION, "band_id": 0, "segment_index": 0},
            {"region_id": _X_ALL_REGION, "band_id": 0, "segment_index": 1},
        ],
        "confidence": 0.93,
    },
    {
        "label": "Do they ship to Canada?",
        "control_type": "single_select",
        "options": [{"text": "Yes", "selected": False}, {"text": "No", "selected": True}],
        "answer": ["No"],
        "annotations": [],
        "region_id": _LABEL_REGION,
        "source_elements": [
            {"region_id": _LABEL_REGION, "band_id": 3, "segment_index": 0},
            {"region_id": _YES_NO_REGION, "band_id": 3, "segment_index": 0},
            {"region_id": _YES_NO_REGION, "band_id": 3, "segment_index": 1},
            {"region_id": _YES_NO_REGION, "band_id": 3, "segment_index": 2},
        ],
        "confidence": 0.97,
    },
    {
        "label": "Vendor Status",
        "control_type": "single_select",
        "options": [
            {"text": "Approved", "selected": False},
            {"text": "Provisional", "selected": True},
        ],
        "answer": ["Provisional"],
        "annotations": ["Confirm on vendor portal"],
        "region_id": _LABEL_REGION,
        "source_elements": [
            {"region_id": _LABEL_REGION, "band_id": 4, "segment_index": 0},
            {"region_id": _YES_NO_REGION, "band_id": 4, "segment_index": 0},
            {"region_id": _YES_NO_REGION, "band_id": 4, "segment_index": 1},
            {"region_id": _YES_NO_REGION, "band_id": 4, "segment_index": 2},
        ],
        "confidence": 0.95,
    },
    {
        "label": "Order Log",
        "control_type": "bool",
        "options": [{"text": "Support Order Log", "selected": True}],
        "answer": ["true"],
        "annotations": ["Check if the vendor is exporting its order log directly"],
        "region_id": _LABEL_REGION,
        "source_elements": [
            {"region_id": _LABEL_REGION, "band_id": 5, "segment_index": 0},
            {"region_id": _YES_NO_REGION, "band_id": 5, "segment_index": 0},
        ],
        "confidence": 0.9,
    },
]


def spec_fragment_response() -> str:
    return json.dumps({"fields": SPEC_FRAGMENT_FIELDS})


_CONVENTION_LABEL_REGION = "p0:c0:field-row:priority"
_CONVENTION_OPTIONS_REGION = "p0:c1:field-row:low_x_high"


def spec_fragment_convention_xlsx_response() -> str:
    return json.dumps(
        {
            "fields": [
                {
                    "label": "Priority",
                    "control_type": "single_select",
                    "options": [
                        {"text": "Low", "selected": False},
                        {"text": "High", "selected": False},
                    ],
                    "answer": [],
                    "annotations": [],
                    "region_id": _CONVENTION_LABEL_REGION,
                    "source_elements": [
                        {
                            "region_id": _CONVENTION_LABEL_REGION,
                            "band_id": 0,
                            "segment_index": 0,
                        },
                        {
                            "region_id": _CONVENTION_OPTIONS_REGION,
                            "band_id": 0,
                            "segment_index": 0,
                        },
                        {
                            "region_id": _CONVENTION_OPTIONS_REGION,
                            "band_id": 0,
                            "segment_index": 1,
                        },
                        {
                            "region_id": _CONVENTION_OPTIONS_REGION,
                            "band_id": 0,
                            "segment_index": 2,
                        },
                    ],
                    "confidence": 0.9,
                }
            ]
        }
    )


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
    "spec_fragment_convention_xlsx": spec_fragment_convention_xlsx_response,
}


def client_for(item_id: str) -> CannedLLMClient | None:
    factory = _CANNED.get(item_id)
    return CannedLLMClient(factory()) if factory else None
