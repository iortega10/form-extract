from __future__ import annotations

import pytest

from formextract.model import ControlType, GlyphKind, RegionType
from formextract.schema import canonical_field, canonical_name, normalize, normalize_label


def test_normalize_label():
    assert normalize_label("  Pre-Approved,   AGO ") == "pre approved ago"
    assert normalize_label("VENDOR (US)") == "vendor us"


@pytest.mark.parametrize(
    "normalizer,value,expected",
    [
        ("yes_no", "Yes", "true"),
        ("yes_no", "no", "false"),
        ("bool_check", "x", "true"),
        ("bool_check", None, None),
        ("text", "  hello   world ", "hello world"),
        ("none", "Provisional", "Provisional"),
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


def test_unmarked_option_is_none():
    from formextract.model import Option

    # A single_select with no mark and no explicit value is unanswered.
    assert normalize("yes_no", None, [Option(text="Yes"), Option(text="No")]) is None
    # A check control with no mark is unanswered, never a synthesised "false".
    assert normalize("bool_check", None, [Option(text="Support Order Log")]) is None
    # A free-text "No" typed by the filler IS an answer and stays "false".
    assert normalize("bool_check", "No", [Option(text="Support Order Log")]) == "false"
    assert normalize("bool_check", "false", []) == "false"


def test_canonical_aliases():
    assert canonical_name("Which US states do they ship to") == "us_states_shipped"
    assert canonical_name("do they ship to canada?") == "ships_to_canada"
    assert canonical_name("Order Log") == "order_log_supported"
    assert canonical_name("Some Novel Question") is None
    cf = canonical_field("us_states_shipped")
    assert cf is not None
    assert cf.control_type is ControlType.MULTI_SELECT
    assert cf.normalizer_id == "state_list"
