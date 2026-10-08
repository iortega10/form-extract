"""Bridge from the gold's cell-id-keyed canned lines to the real line contract.

``tools/make_gold.py`` writes the perfect and mutated responses for the 0.6.0
line contract in **cell-id form** (``{Sheet!r:c}`` where a response would name
``row.seg``): the generator never imports ``formextract``, so it cannot know the
coordinate the row lattice assigns a cell. This module is the bridge. It ingests
each gold tab, builds the page-global lattice, and substitutes every placeholder
with ``row.seg``, so the artifact stays ground truth (a property of the workbook
cells) and the test still drives the real projection coordinate.

Test-only code, so it may import the package and ``tools/make_gold.py``.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _root in (REPO, REPO / "tools"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

import make_gold  # noqa: E402
import score_gold  # noqa: E402

from formextract.ingest import ingest  # noqa: E402
from formextract.layout import analyze  # noqa: E402
from formextract.model import to_json  # noqa: E402
from formextract.pipeline import Pipeline, PipelineConfig  # noqa: E402
from formextract.resolve import LLMResponse  # noqa: E402
from formextract.rows import build_all_rows  # noqa: E402
from formextract.store import Store  # noqa: E402

#: ``{Sheet!r:c}`` -> the lattice coordinate. Unknown placeholders are left as
#: the bare cell id, which the parser reads as a malformed ref and turns into a
#: sentinel, so a deliberately unresolvable ref never reaches the bridge.
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_TAB_IN_PROMPT = re.compile(r"Rows of `([^`]+)`")


class TabGeometry:
    """One gold tab's ingest, layout and lattice, plus its cell -> ``row.seg`` map."""

    def __init__(self, path: Path):
        self.path = Path(path)
        ing = ingest(self.path)
        self.by_id = {e.element_id: e for e in ing.elements}
        self.layout = analyze(ing.elements)
        self.lattice = build_all_rows(self.layout, self.by_id)[0]
        self.refs: dict[str, str] = {}
        for element_id in self.by_id:
            row = self.lattice.row_of_element(element_id)
            if row is None:
                continue
            self.refs[element_id] = (
                f"{row}.{self.lattice.rows[row].elements.index(element_id)}"
            )

    def ref_object(self, cell_id: str) -> dict | None:
        resolved = self.lattice.refs_by_element.get(cell_id)
        if resolved is None:
            return None
        return {
            "region_id": resolved[0],
            "band_id": resolved[1],
            "segment_index": resolved[2],
        }

    def region_of(self, cell_id: str) -> str | None:
        resolved = self.lattice.refs_by_element.get(cell_id)
        return resolved[0] if resolved else None


def render(doc_tab: dict, refs: dict, *, end: bool | None = None) -> str:
    """Render one tab's cell-id lines as a real response text.

    ``doc_tab`` is one entry of a ``make_gold`` lines document: ``{"lines": [...],
    "end": bool}``. Each ``{cell}`` becomes the lattice's ``row.seg``; ``end``
    overrides the artifact's terminator flag (the section-3.5 cut case).
    """
    lines = [_PLACEHOLDER.sub(lambda m: refs.get(m.group(1), m.group(1)), line)
             for line in doc_tab["lines"]]
    text = "\n".join(lines)
    if doc_tab["end"] if end is None else end:
        text += "\nend"
    return text


class LinesClient:
    """A canned lines client: the tab named in the prompt -> its response text.

    Fills ``finish_reason`` from the text itself: a response that ends with the
    terminator is a clean stop, one that does not is reported cut, so the
    section-3.5 rule has something to act on.
    """

    def __init__(self, texts_by_tab: dict[str, str]):
        self.texts = dict(texts_by_tab)
        self.prompts: list[str] = []

    def complete(self, prompt, *, model, params):
        self.prompts.append(prompt)
        match = _TAB_IN_PROMPT.search(prompt)
        if match is None:
            raise AssertionError("the lines prompt names no tab")
        text = self.texts[match.group(1)]
        return LLMResponse(
            text=text,
            model=model,
            params=params,
            tokens=1,
            latency_ms=0,
            finish_reason="stop" if text.endswith("\nend") else "length",
        )


class GoldLines:
    """Canned line responses for one gold set, rendered onto real refs."""

    def __init__(self, gold_dir: Path, gold: dict):
        self.gold_dir = Path(gold_dir)
        self.gold = gold
        self.geometry = {
            tab: TabGeometry(self.gold_dir / f"{tab}.xlsx") for tab in gold["tabs"]
        }

    # -- documents ---------------------------------------------------------

    def _render_all(self, doc: dict) -> dict[str, str]:
        return {
            tab: render(doc[tab], self.geometry[tab].refs) for tab in self.gold["tabs"]
        }

    def perfect(self) -> dict[str, str]:
        return self._render_all(make_gold.perfect_lines(self.gold))

    def mutation(self, name: str) -> dict[str, str]:
        return self._render_all(make_gold.line_mutations(self.gold)[name])

    def document(self, tab: str) -> str:
        return self.perfect()[tab]

    # -- running -----------------------------------------------------------

    def run(
        self,
        texts_by_tab: dict[str, str],
        store_dir: Path,
        *,
        contract: str = "lines",
        conventions: bool = True,
        reuse: bool = False,
        kind_rule: bool = True,
    ) -> list[dict]:
        """Run every tab through the real ``Pipeline``; return the fields.

        One throwaway ``Store`` for the set, the gold's own conventions declared
        by default, reuse off (a fresh store has nothing to replay).
        """
        config = PipelineConfig(
            model="canned",
            output_contract=contract,
            checkbox_conventions=(
                list(self.gold.get("checkbox_conventions") or []) if conventions else []
            ),
            reuse_layout_bindings=reuse,
            kind_rule=kind_rule,
        )
        client = LinesClient(texts_by_tab)
        store = Store(Path(store_dir))
        fields: list[dict] = []
        for tab in self.gold["tabs"]:
            record = Pipeline(store, client, config).run(self.gold_dir / f"{tab}.xlsx")
            fields.extend(json.loads(to_json(record))["fields"])
        return fields


def score(gold_doc: dict, fields: list[dict]) -> dict:
    """Score a fields list against the gold with the stdlib scorer (numbers only)."""
    gold = score_gold.Gold(gold_doc)
    predicted, stray, unresolved = score_gold._predicted_fields(gold, fields)
    return score_gold.score_document(gold, predicted, stray, unresolved, None)
