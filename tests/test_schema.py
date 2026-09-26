from __future__ import annotations

import pytest

from formextract.model import ControlType, GlyphKind, RegionType
from formextract.schema import canonical_field, canonical_name, normalize, normalize_label


def test_normalize_label():
    assert normalize_label("  Non-Admitted,   AGO ") == "non admitted ago"
    assert normalize_label("CARRIER (US)") == "carrier us"


@pytest.mark.parametrize(
    "normalizer,value,expected",
    [
        ("yes_no", "Yes", "true"),
        ("yes_no", "no", "false"),
        ("bool_check", "x", "true"),
        ("bool_check", None, None),
        ("text", "  hello   world ", "hello world"),
        ("none", "Non-Admitted", "Non-Admitted"),
        (None, "anything", "anything"),
    ],
)
def test_normalizers(normalizer, value, expected):
    assert normalize(normalizer, value, []) == expected


def test_yes_no_prefers_single_selected_option():
    from formextract.model import Option

    opts = [Option(text="No", selected=True)]
    assert normalize("yes_no", "Yes", opts) == "false"
    opts = [Option(text="All", selected=True)]
    assert normalize("state_list", "GA", opts) == "ALL"


def test_state_list_sorts_and_uppercases():
    assert normalize("state_list", "ga tx al", []) == "AL, GA, TX"


def test_unknown_normalizer_raises():
    with pytest.raises(KeyError):
        normalize("nope", "x", [])


def test_canonical_aliases():
    assert canonical_name("Which US states do they write in") == "us_states_written"
    assert canonical_name("do they write in canada?") == "writes_in_canada"
    assert canonical_name("Loss Runs") == "loss_runs_supported"
    assert canonical_name("Some Novel Question") is None
    cf = canonical_field("us_states_written")
    assert cf is not None
    assert cf.control_type is ControlType.MULTI_SELECT
    assert cf.normalizer_id == "state_list"
