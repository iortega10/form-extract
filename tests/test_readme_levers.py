"""T1: the README levers table cannot silently lose a row."""
from __future__ import annotations

from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"

MARKER = "**Levers, most effective first.**"
END_MARKER = "**Where to look when it is slow.**"

LEVERS = [
    "thinking",
    "non-reasoning model",
    "max_tokens",
    "chunk_workers",
    "reuse_layout_bindings",
    "non_answer_columns",
    "include_address",
    "include_hidden_sheets",
]


def _levers_block() -> str:
    text = README.read_text(encoding="utf-8")
    assert MARKER in text, "README lost the levers table header"
    after = text.split(MARKER, 1)[1]
    end = after.find(END_MARKER)
    assert end != -1, "README lost the paragraph after the levers table"
    return after[:end]


def test_levers_table_lists_every_lever():
    block = _levers_block()
    assert "| lever | cost note |" in block, "README levers table is not a table"
    for lever in LEVERS:
        assert lever in block, f"README levers table lost the {lever!r} row"


def test_levers_table_is_ranked_and_notes_the_two_biggest():
    block = _levers_block()
    # thinking off is the first row, ahead of chunk_workers.
    assert block.index("thinking") < block.index("chunk_workers")
    assert "workbook-dependent" in block, "reuse row lost its cost caveat"


def test_provider_family_params_table_and_default_params_sentence():
    block = _levers_block()
    assert '{"temperature": 0}' in block
    assert "thinkingBudget" in block
    assert "reasoning" in block
    assert "must pass their own `params`" in block


def test_client_error_text_is_not_swallowed():
    text = README.read_text(encoding="utf-8")
    assert "transport failed" in text
