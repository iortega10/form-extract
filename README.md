# form-extract

Extract structured fields from non-uniform form documents (checklists, compliance
forms, questionnaires) as PDF or XLSX. The pipeline is
**ingest → layout perception → (optional) LLM binding → instance record**:

- **ingest** (`formextract/ingest/`) — format-adaptive parsing (pymupdf word
  spans for PDF, openpyxl cells for XLSX) producing a stream of `Element`s.
- **layout** (`formextract/layout.py`) — banding, column/region segmentation,
  glyph classification (checkmarks, `X` marks), anchor detection, and
  ambiguous-control hypotheses (e.g. `Yes/No → single_select | multi_select`).
- **resolve** (`formextract/resolve.py`) — projects layout chunks into an LLM
  prompt, parses field drafts, canonicalizes labels, and normalizes values.
- **store** (`formextract/store.py`) — content-addressed archive of raw
  inputs, instance records, and LLM calls; instance runs are idempotent by
  content hash.
- **evals** (`formextract/evals/`) — golden manifest, synthetic fixtures, a
  canned (deterministic) LLM, and a harness that scores region types, anchors,
  and field bindings.

Release notes live in [`CHANGELOG.md`](CHANGELOG.md).

## Install

```bash
pip install -e ".[dev]"
```

Base install (XLSX only) requires Python ≥ 3.11 and `openpyxl`. PDF text-layer
support is an optional extra (PyMuPDF is AGPL-3.0 and no longer installed by
default):

```bash
pip install -e ".[dev,pdf]"      # development + PDF
# or, as a plain dependency:
pip install "form-extract[pdf]"
```

## Run the test suite

```bash
python -m pytest tests -q
```

100+ tests covering ingest, layout, schema normalization, store idempotency,
resolve parsing, provenance/field identity, hidden sheets, the optional PDF
backend, checkbox marker classification, non-answer columns, pipeline
end-to-end, and eval-harness scoring.

## Run the golden eval (against the bundled fixture)

The golden manifest `formextract/evals/manifests/spec_fragment.json` builds a
synthetic checklist (as PDF and XLSX), extracts it with a deterministic canned
LLM, and scores the output:

```bash
python -m formextract.evals formextract/evals/manifests/spec_fragment.json \
    --store .formextract-store --out report.json
# equivalent: formextract-eval formextract/evals/manifests/spec_fragment.json
```

Flags: `--llm {canned,none}` (default `canned`), `--model`, `--store`,
`--out`. Current scores: region-type accuracy **1.0**, anchors F1 **1.0**,
binding F1 **1.0** (8/8 fields), budgets ok.

## Test against your own file

### 1. Perception only (no LLM, works out of the box)

Run the pipeline with `llm_client=None` to get regions, anchors, and
unconsumed text residuals for any `.pdf` (with a text layer) or `.xlsx`:

```python
from formextract.pipeline import Pipeline
from formextract.store import Store

record = Pipeline(Store(".formextract-store"), llm_client=None).run("path/to/form.pdf")

print(record.status)                     # partial when no fields were resolved
for region in record.regions:
    print(region.region_id, region.type.value)  # e.g. p0:c1:r3 grid / header / field-row
for anchor in record.anchors:
    print(anchor.normalized_text)        # normalized field-label strings
for span in record.residuals:
    print(span.text)                     # text not consumed by any field
```

For layout detail without a stored record, use ingest + layout directly:

```python
from formextract.ingest import ingest
from formextract.layout import analyze

layout = analyze(ingest("path/to/form.xlsx").elements)
print(layout.regions, layout.anchors, layout.glyphs, layout.hypotheses)
```

### 2. Full extraction with an LLM

`Pipeline` accepts any object implementing the `LLMClient` protocol —
`complete(prompt, *, model, params) -> LLMResponse`. The prompt already
contains the JSON output contract; your client must return the model's raw
text response:

```python
from formextract.pipeline import Pipeline, PipelineConfig
from formextract.resolve import LLMResponse
from formextract.store import Store


class MyClient:
    def complete(self, prompt, *, model, params):
        text, finish_reason = call_my_llm(prompt)  # your provider call
        return LLMResponse(
            text=text,
            model=model,
            params=params,
            tokens=123,
            finish_reason=finish_reason,  # OpenAI "length", Gemini "MAX_TOKENS", ...
        )


pipeline = Pipeline(
    Store(".formextract-store"),
    llm_client=MyClient(),
    config=PipelineConfig(model="gpt-4o-mini", params={"temperature": 0}),
)
record = pipeline.run("path/to/form.pdf")

for f in record.fields:
    print(f.canonical_name or f.label_text, "=", f.value_normalized, f.control_type)
```

Notes:

- Re-running the same bytes returns the cached instance (content-hash
  idempotency); change `PIPELINE_VERSION` or clear the store to force a rerun.
- `.xlsx` and text-layer `.pdf` are supported. PDF text-layer support needs
  the optional `[pdf]` extra; without it a `.pdf` raises `PdfBackendUnavailable`
  (and the eval harness skips PDF items with a reported reason). Scanned/
  image-only PDFs raise `NotImplementedError` at ingest (Phase 5 vision tier).
- Every LLM call is archived under the store root (`llm/`), and the resulting
  instance JSON is written to `<store>/instances/<instance_id>.json`.
- Each resolved `Field` carries a content-derived `field_id` (stable for the
  same file + pipeline version, independent of tab names), plus `tab`,
  `section_path` and `source_elements`. Hidden workbook sheets are captured in
  `SourceInfo.sheet_state`, excluded from default output, and included only
  with `PipelineConfig(include_hidden_sheets=True)`.

### Integrating from an agent or service

For callers wrapping the pipeline in an agent, service, or job runner. The
prompt-size and concurrency knobs it refers to are in
[Prompt size and concurrency](#prompt-size-and-concurrency).

**Cost model.** One model call per authored tab, plus at most one repair call
for a tab whose response was not valid JSON (or whose fields all failed to
parse). Only tabs with layout content are authored, so hidden sheets are
skipped by default. Input runs roughly 0.6k–2.7k tokens per tab on a dense
sheet (the projection lists one band line per label/option/mark); output is one
JSON object per field found on that tab, so it grows with the field count.

**Model and client.** Return the model's raw text. Do not wrap the call in
another agent loop and do not issue per-tab sub-calls yourself: the pipeline
already chunks per tab, and `chunk_workers` runs those chunks in parallel. A
slow client call is the first thing to check when a run feels slow — every
archived call carries `latency_ms`. Raise on a transport failure (never retry
internally), and raise `NonRetryable` — or an exception carrying a 4xx
`status_code`/`code` — for a request that can never succeed: a 4xx costs exactly
one call. Do not swallow the provider's error text: it ends up in the record's
`errors` as `transport failed: {exc}`. Set `LLMResponse.finish_reason` to the
provider's stop reason (OpenAI `length`, Anthropic `max_tokens`, Gemini
`MAX_TOKENS`, ...): the lines output contract reads it to tell a length cut from
a clean stop. It is optional and defaults to `None` (`unknown`).

**Levers, most effective first.**

| lever | cost note |
|---|---|
| thinking / reasoning off | On a reasoning model the reasoning tokens dominate both the wall clock and the bill; switching thinking off is the single biggest lever. |
| a fast non-reasoning model | The generation rate is roughly fixed per model, so latency tracks `output tokens / rate`; a small non-reasoning model writes the same JSON faster. |
| an explicit `max_tokens` (`max_completion_tokens` on OpenAI, `maxOutputTokens` on Gemini) | Size it to the expected output: a length-truncated JSON response is a reported condition (a `response truncated after N fields` error and a `partial` status), not a crash. |
| `chunk_workers=N` | Authors N tabs concurrently, so wall clock drops without changing per-call cost; the client must be thread-safe. |
| `reuse_layout_bindings=True` | workbook-dependent: it only fires when two tabs have the same geometry **and** the same anchor labels, so run `tools/profile_workbook.py` first. |
| `non_answer_columns=[...]` | Keeps reference/tag columns out of the projection, so prompts stay smaller and those columns are never read as answers. |
| `include_address=False` | The default; the per-field `address` object grew every prompt and every response. |
| `include_hidden_sheets` | Left at its default `False`; hidden sheets cost nothing because they are never authored. |

The package's default `params` stays `{"temperature": 0}`. A reasoning-model
integrator must pass their own `params` — those models reject `temperature`
(HTTP 400):

| provider family | params |
|---|---|
| non-reasoning (default) | `{"temperature": 0}` |
| Gemini 2.5 family | `{"temperature": 0, "thinkingBudget": 0}` (`thinkingBudget` maps to `generationConfig.thinkingConfig`) |
| OpenAI reasoning models | omit `temperature`; set a low reasoning effort |

**Where to look when it is slow.** Each stored call under
`<store>/llm/calls/*.json` carries `tokens` and `latency_ms`. `Pipeline.run`
returns the record, so this shows where the time went:

```python
from formextract.store import Store

store = Store(".formextract-store")
record = store.load_instance("INSTANCE_ID")
for call in record.llm_calls:
    print(call.call_id, call.latency_ms, call.tokens)
```

**What the model should return.** The prompt carries the full output contract;
a response is one JSON object with exactly those keys and no `address` key
unless `include_address=True`. A tiny tab with one single-select, one
multi-select, one bool and one text field:

<!-- example:model-output -->
```json
{
  "fields": [
    {
      "label": "Ship to Canada?",
      "control_type": "single_select",
      "options": [{"text": "Yes", "selected": true}, {"text": "No", "selected": false}],
      "answer": ["Yes"],
      "annotations": [],
      "region_id": "p0:c1:field-row:ship-to-canada",
      "source_elements": [{"region_id": "p0:c1:field-row:ship-to-canada", "band_id": 1, "segment_index": 0}],
      "confidence": 0.9
    },
    {
      "label": "Regions covered",
      "control_type": "multi_select",
      "options": [{"text": "EMEA", "selected": true}, {"text": "APAC", "selected": false}],
      "answer": ["EMEA"],
      "annotations": [],
      "region_id": "p0:c1:field-row:regions-covered",
      "source_elements": [{"region_id": "p0:c1:field-row:regions-covered", "band_id": 2, "segment_index": 0}],
      "confidence": 0.8
    },
    {
      "label": "Priority order",
      "control_type": "bool",
      "options": [],
      "answer": ["true"],
      "annotations": [],
      "region_id": "p0:c1:field-row:priority-order",
      "source_elements": [{"region_id": "p0:c1:field-row:priority-order", "band_id": 3, "segment_index": 0}],
      "confidence": 0.7
    },
    {
      "label": "Reviewed by",
      "control_type": "text",
      "options": [],
      "answer": ["J. Rivera, 2026-01-05"],
      "annotations": [],
      "region_id": "p0:c1:field-row:reviewed-by",
      "source_elements": [{"region_id": "p0:c1:field-row:reviewed-by", "band_id": 4, "segment_index": 0}],
      "confidence": 0.6
    }
  ]
}
```

**Reading a result.** `record.status` is `complete` or `partial`, and
`record.errors` lists what went wrong per chunk (parse failure, transport
failure, or a field-level error such as `field 2: ...`). A single field can
carry `provenance.review_flag` with a `provenance.review_reason` (for example
`ambiguous_mark` or `replay_mismatch`), a `Field.ambiguity` record, or
`unresolved_source_refs`. A `partial` run is re-attempted on the next `run()`
of the same bytes: chunks whose response parsed with no field errors are served
from the call cache, while chunks with parse or field errors are re-sent.

**Row coverage.** Every record carries a `coverage` block: one entry per tab
with `units_total`, `units_consumed` and `ratio`. A unit is one anchor row or
one GRID region, and a unit counts as consumed when any element of its band
lands in a field's resolved `source_elements`. So `ratio` is the share of the
tab's anchor rows and grid regions that some field used — it is **not a
quality score**, and by default it changes nothing: a run that consumed 5 of
98 anchor rows still reads `complete`. A tab with no eligible units (for
example one whose only column is listed in `non_answer_columns`) has
`ratio: null`, which is ineligible — never `1.0`, never a failure. The
`unbound` list gives the ids of the units no field used (capped at 50, with
`unbound_truncated` marking the cut), so you can inspect what was missed
without reading any label text.

`PipelineConfig.min_coverage` is opt-in and has **no default** — nothing in a
single workbook defends a number, so you pick one. Setting it makes such a run
`partial` and adds one error per below-threshold tab (`tab <name>: row coverage
0.05 < 0.50`); `ratio: null` tabs are ineligible and never fail.
`record.coverage_min_ratio` is the minimum `ratio` over eligible tabs (`null`
when none is eligible). A rerun after a `min_coverage` failure recomputes but
spends almost nothing: a `partial` record is never served from the instance
cache, yet each chunk whose response parsed without field errors is served from
the call cache. Only a cached response that carried field-level errors is
re-sent (it is archived unindexed).

```python
config = PipelineConfig(min_coverage=0.5)  # an example threshold, not a recommendation
record = Pipeline(store, client, config).run("form.xlsx")
for block in record.coverage:
    print(block.tab, block.ratio, len(block.unbound))
```

**One-option `single` kind rule.** `PipelineConfig.kind_rule` (default `True`)
re-derives a draft the model states as `single_select` with exactly one option,
no answer and no other field on its lattice row — a typed value cited as an
option, not a single choice: the cited cell becomes the answer and the kind
becomes `text`, or `bool` when the row holds a marker the layout classified. A
field that already states an answer is left alone. Only a re-derived field's
`provenance` gains `stated_control_type` (the model's kind) and `kind_rule`
(`one_option_single`), so every other record serialises byte-identically;
`SCHEMA_VERSION` stays `"2"`, `PIPELINE_VERSION` is `"6"`. Set `kind_rule=False`
to leave the model's kind as stated (it still moves the instance cache key). A lone
checkbox reads as `bool`, whether spelled with the unicode glyphs `□`/`☐` or
with an ASCII box (`[ ]`, `[x]`, `( )`) at the start of a cell.

**Pitfalls.**

- `force=True` re-sends every chunk: it skips both the instance cache and the
  per-prompt call cache, so an unchanged prompt still makes one model call per
  chunk (the call budget still applies). `force=False` (the default) serves an
  unchanged prompt from the call cache and spends nothing.
- A changed model, `params`, prompt, `PROMPT_VERSION`/`PIPELINE_VERSION`, or
  `include_address` is a different cache key, so changing any of them re-spends
  the run.
- One process per `Store` directory: two processes writing the same store are
  not supported.

#### Measuring a provider before you commit to it

`tools/live_probe.py` measures what one call to your provider actually costs on
a real workbook, without touching the store: it ingests and projects the file
with the package's own functions, calls the endpoint directly, and prints one
JSON object per line of **numbers only** (input/output/reasoning token counts,
`seconds`, tokens/second, finish-reason class, fields, parse errors,
truncation, unresolved refs, anchors and status/HTTP-error classes). It never
prints a label, an answer or a response body by construction.

```console
python tools/live_probe.py path/to/form.xlsx --provider openai --model gpt-4o-mini
python tools/live_probe.py path/to/form.xlsx --provider gemini --model gemini-2.5-flash \
    --tab 0 --param thinkingBudget=0
```

The key is read only from the environment variable named by `--env-var`
(`OPENAI_API_KEY` / `GEMINI_API_KEY` by default); it is never printed, stored or
placed in a URL, and an empty `--env-var ""` sends no auth header (Gemini
authenticates by the `x-goog-api-key` header). `--base-url` points the OpenAI
provider at any OpenAI-compatible endpoint. Other flags: `--param k=v`
(repeatable; `thinkingBudget=N` maps to Gemini's
`generationConfig.thinkingConfig`), `--max-tokens N`, `--tab N` (an index among
the chunks, never a name), `--repeat K` (prints min/median/max, not K rows),
`--workers N`, `--no-temperature` (omit `temperature` for reasoning models) and
`--dump PATH` — the one path prompt/response text may be written to, only when
explicitly requested.

Recommended first run: `--tiny` sends a fixed ~10-token prompt and reports its
seconds as `floor_seconds` (the per-call floor `F`), then one real tab measures
the generation rate on top of it — start with thinking off
(`--param thinkingBudget=0` on Gemini, or a non-reasoning model).

#### Measuring grouping quality (the gold set)

A faster output contract can quietly regroup fields while the latency numbers
look good, so grouping quality is measured separately against a synthetic gold
set whose correct fields are known by construction.

```console
python tools/make_gold.py --set dev --seed 20260101 --out gold --canned gold/canned
python tools/score_gold.py --gold gold/gold_dev.json --record record.json
```

`tools/make_gold.py` (openpyxl only, never imports the package) writes one
`.xlsx` per tab plus a gold JSON keyed by the xlsx element ids the ingest emits
(`Sheet!row:col`), so the ground truth is independent of bands, regions and any
model. The dev set is 16 tabs (~245 fields) covering a closed vocabulary of
structure tags: twelve homogeneous tabs (one structure each — yes/no rows, dense
and stacked multi-select, side-by-side, two-row and two-cell labels, text,
typed values, no-glyph answers, a 50-option grid, a matrix, merged tall cells, a
gutter column, a non-answer column) and four **mixed** tabs of 60-100 rows that
place 9+ structure kinds in one tab (the two long-form shapes, a 50-option grid
and a 12-row yes/no run, in every mixed tab), separated by header/note/skip rows.
A mixed tab's fields carry `mixed_tab` **in addition to** their structure tag, so
each structure's score includes its mixed instances and `mixed_tab` reports the
mixed set as a whole. `--set heldout` builds a second set (two mixed tabs, a
different seed and a different vocabulary, never used for tuning). Appending the
mixed tabs leaves every homogeneous tab's bytes unchanged (a committed sha256
table guards it). Regeneration is deterministic: the same `--seed` and `--set`
produce byte-identical files.

`--gold-version {2,3,4}` picks the gold schema. **3** (the default for a new set)
makes a field's **kind readable from the sheet**: a question label alone leaves
`single` vs `multi` and `bool` vs `text` a coin flip, so every label carries a
kind cue — multi-select and grid fields end `(select all that apply)`, the other
single-selects (side-by-side, matrix, no-glyph) end `(choose one)`, the affirm/
dissent yes/no rows keep the plain question, text fields are rewritten as
imperatives (`Enter the …`) and the typed-value fields as noun phrases
(`… of the …`). The cue is recorded per field as `kind_cue` (a closed vocabulary:
`plain_question`, `select_all_that_apply`, `choose_one`, `imperative`,
`noun_phrase`). **2** is frozen: it reproduces the earlier sets, tabs and gold
JSON, byte for byte, and omits `kind_cue`. A v3 set has the same fields, cells
and selections as its v2 twin — only the label wording changes — so the two
versions score identically on a perfect response and differ only in what a model
can read off the sheet.

**4** is opt-in (`--gold-version 4`; the default stays 3) and rebuilds only the
`matrix` tag as an ordinary mark grid: a header row of three option cells drawn
from the set's own option pool and, per row, a label in column 1 plus one
`marker` cell under exactly one header. Every matrix field is `kind=single` with
the three headers as its option cells, and carries `expected_ambiguous` — a mark
directly under a column header has no `mark_precedes_option` neighbour (the
layout classifies it `UNATTACHED`, with no candidates) so the resolver cannot
decide it. v2 and v3 are untouched, byte for byte; v4 differs from v3 only in the
tabs that hold a matrix (`Dev11`, the mixed `Dev13`, `Hold05`), because each tab
draws from its own seed-keyed RNG stream.

*2026-10-07 (G3b)* — the typed-value fields (gold kind `bool`) keep the plain
question of v2 rather than the noun phrase described above: a noun phrase under a
typed answer reads as a text field, so the generator would manufacture the kind
confusion the cue exists to avoid. `noun_phrase` stays in the declared vocabulary
but no structure tag maps to it.

Every tab records the checkbox convention its generator used, in a
`checkbox_conventions` list written in exactly the shape
`PipelineConfig.checkbox_conventions` takes (here: one selector per tab,
`mark_precedes_option`, because the generator always writes the mark before its
option). Print it to paste into a run:

```console
python tools/make_gold.py --print-conventions gold/gold_dev.json
```

A gold run **must** declare those conventions: a between-marker is `AMBIGUOUS`
without one. The generator also marks a deliberate subset `expected_ambiguous`
(the side-by-side tab's shared-marker row, and a two-field shared-marker row in
every mixed tab), where the ambiguity is the designed outcome, so the scorer does
not read it as a regression.

`tools/score_gold.py` (stdlib only) compares a record JSON dump — a full
`InstanceRecord` dump from `to_json`, or a bare `{"fields": [...]}` — against a
gold file and prints integers and percentages: strict field precision and recall
(no half credit), `merge_count` / `split_count` / `missed` / `spurious`,
`stray_ref_count` / `unresolved_ref_count`, and on matched pairs `label_ok`,
`options_ok`, `answer_ok`, `selected_ok` split into `selected_ok_under_convention`
(a field the resolver decided) plus `selected_ambiguous_expected` (a field the
gold expects to stay ambiguous), with `unexpected_ambiguous` counted per tag
(the signal that a convention was not declared or could not apply), plus
`addressed` (a **perception** coverage: the share of gold label/option cells any
predicted field cites). Every number is also reported per structure tag
(`small_n` marks a tag with fewer than five gold instances). The output contains
only integers, percentages and tag names — never a label, a cell text or a tab
name — so the same scorer can be run on your own workbook's gold: it prints
numbers only.

**Two scores, not one.** The strict `matched` count needs the field's element set
*and* its kind to agree. That conflates two different questions — did the model
**group** the cells into the right field, and did it **guess the kind** — so the
scorer reports both. `matched_ignoring_kind` matches on the element set alone (a
predicted field whose L∪O element ids equal a gold field's set, kind ignored; at
most one predicted per gold, in the same greedy order as the strict matcher) and
is never below `matched`. `kind_confusions` is the gap: gold fields matched
ignoring kind but not strictly, i.e. fields the model grouped correctly and typed
wrongly. The identity `matched_ignoring_kind == matched + kind_confusions` holds
by construction. `kind_confusion_pairs` is the direction of those mistakes, a
small closed-vocabulary count table keyed `"<gold kind>><predicted kind>"` (kinds
`single`/`multi`/`bool`/`text`) — never text from the sheet. Read `matched` as the
score when kind matters, `matched_ignoring_kind` as the ceiling a correct grouping
would reach, and `kind_confusions` as the part of the gap a better prompt (or a
better gold) could still close. `live_probe.py --gold` forwards the same three
counters, overall and per tag.

To score a whole gold set end to end, run every tab through the real pipeline and
print one line of numbers:

```console
python tools/live_probe.py --gold gold --provider gemini --model gemini-2.5-flash-lite \
    --param thinkingBudget=0
python tools/score_gold.py --gold gold/gold_dev.json --record record.json --json
```

On a rate-limited (free) key add `--min-interval 6`: calls start at least that many
seconds apart, and the output's `http_status_counts` / `incomplete_tabs` show
whether any call was refused (nothing is retried).

`live_probe.py --gold DIR` takes a directory made by `make_gold.py --out`, runs
each tab's `.xlsx` through the real `Pipeline` with the gold's own conventions
declared, a throwaway store and `--workers` as `chunk_workers`, refuses (exit 2) a
directory whose tabs no longer match the manifest sha256, and prints one JSON
line of numbers: the scorer's headline counters (plus per-tag `matched`/`gold`)
and the probe's usual cost keys. `--record-out PATH` writes the combined record
dump only when asked.

`--contract lines` measures the same gold set under the 0.6.0 row-lines contract
(`PipelineConfig.output_contract`, default `"json"`), and `--prompt-variant NAME`
picks that contract's prompt:

```console
python tools/live_probe.py --gold gold --provider gemini --model gemini-2.5-flash-lite \
    --contract lines --prompt-variant fix2
```

`base` is the default and the prompt 0.6.0-L1b shipped; `fix1`, `fix2`,
`fix2_notags` and `fix2_style` are experimental candidates written from the
failures a live dev run exposed, and they stay experimental until the owner reads
their numbers. `fix2_notags` is `fix2`'s text over a projection with the row tags
off, and `fix2_style` is `fix2`'s text plus one sentence defining the `[shaded]`
projection tag, which it is the only variant to turn on (see `resolve.py`). The
name is printed as `"prompt_variant"` in the output (the single-tab mode prints
it too, but measures the json prompt, which has no variants) and is part of the
run's cache key. The tuning rule: a variant is tuned on the dev gold only, a
frozen variant is run once on the held-out set and those numbers are never fed
back, and a revision is a new name rather than an edit to a variant that has
held-out numbers.

```console
python tools/score_gold.py --gold gold/gold_dev.json --record record.json --json
```

### 3. Test against the synthetic fixture directly

```python
from pathlib import Path
from formextract.evals.synthetic import build

pdf = build("spec_fragment_pdf", Path("fixtures"))   # also: spec_fragment_xlsx
```

## Checkboxes

Markers (`X`, checkmarks, checked boxes) are decided by the resolver, not the
model. A control has three honest states:

- **selected** — a unique right-only marker (box before label) auto-selects its
  option.
- **unmarked / null** — no marker and no explicit value is unanswered, never
  `"false"`.
- **ambiguous** — a marker between two options (or a right-only marker with a
  competitor) is recorded as null with `provenance.review_flag=True`,
  `ReviewReason.AMBIGUOUS_MARK`, and `Field.ambiguity` naming the marker and its
  candidates. Between-markers stay null unless the caller declares a
  convention.

A field's own label is never an option candidate: a `Label | X | Option` row is
a right-only marker and auto-selects, not a between-marker.

Declare which way a between-marker points for a form family with
`PipelineConfig.checkbox_conventions`:

```python
PipelineConfig(checkbox_conventions=[
    {"tab": "Checklist", "anchor_pattern": r"^(yes|no|priority)$",
     "convention": "mark_follows_option"},
])
```

`tab` is an exact sheet name or an `fnmatch` glob; `anchor_pattern` is a regex
matched against the normalised label/anchor text of the control (`None` matches
every control on the tab). `mark_follows_option` means the marker belongs to the
option on its left (`Low X High` → `Low`); `mark_precedes_option` means it
belongs to the option on its right (`Low X High` → `High`). When several
conventions match one control the most specific wins: `anchor_pattern` beats
`None`, a literal tab beats a glob, and a longer tab beats a shorter. A
declaration the geometry cannot support (it requires an option on a side where
there is none) selects nothing and flags the control for review. The model
never supplies or overrides a convention.

Declare columns that hold reference/tag words (never answers) with
`PipelineConfig(non_answer_columns=["Z"])` or
`PipelineConfig(non_answer_columns=[{"tab": "Checklist*", "column": "Z"}])`. Text in
those columns is projected as annotations only and is never a marker or an
option candidate.

## `json` vs `lines`: choosing the output contract

`PipelineConfig.output_contract` picks what the model writes. Both contracts
resolve the same way; they differ in the response format and in who proposes a
checkbox mark.

- **`"json"` (the default)** — the 0.5.0 contract: one JSON object of fields per
  tab. The model states each field's `control_type`, its `options` (each with a
  `selected` flag) and its `answer`.
- **`"lines"`** — a compact row-indexed line grammar, one line per field
  (`kind L=.. O=.. A=..`). The model names *cells* and never states `selected`;
  the resolver decides every mark. One call per tab, no repair call.

**Who decides a checkbox mark.** Under `json` the model proposes the `selected`
flags and the resolver may override them (a declared `checkbox_conventions`
entry, or a unique right-only marker); under `lines` the resolver decides and
the model only names cells. The `X | label | Option` rules in
[Checkboxes](#checkboxes) apply to both.

**What `answers` and `options[].selected` mean (after 0.6.1).** In either
contract `options[].selected` is the resolver's decision. When the resolver
selects options, `answers` agrees with them — the selected options' texts in
option order — and the model's own differing claim is kept on
`provenance.model_answers`. So read `answers` for a `text` field's typed value,
and `options[].selected` (or `value_raw`) for a select/checkbox field's
selection.

**Speed.** Latency tracks output tokens, and the line grammar is smaller than
the JSON object, so `lines` is usually faster: one integrator measured 213 s
(json) vs 16 s (lines) on one workbook — a single observation, not a benchmark.
Measure your own tabs; each stored call carries `latency_ms` and `tokens`.

**Prefer `lines`** for a plain checklist when speed or cost matters; keep the
default `json` when you want the model's own `selected` flags as an independent
second opinion.

**Strict tabs.** `min_coverage=0.01` makes any eligible tab that binds nothing
`PARTIAL`, with one error naming the tab. It also flags tabs that are not forms
— a header strip, a lookup/data table, a small label/value block — so read the
error as "look here", not "this is broken". The per-tab coverage record also
carries `model_declined`: `True` when a tab has anchors, the model returned only
`hdr`/`note`/`skip` dispositions and no field — which separates "the model
declared this tab non-form" from "anchors present but unbound".

## Repeated tabs (opt-in reuse)

`PipelineConfig(reuse_layout_bindings=True)` turns on in-memory layout-signature
reuse. It is off by default; with the default `False` the pipeline is exactly
the single-call authoring path from 0.3.1 (one LLM call per tab, `REPLAY` never
appears).

When enabled, the first tab of a signature group (in workbook order) is the
exemplar and is authored normally. A later tab reuses it only when the whole
four-step ladder passes:

1. its quantised geometry signature (banding-scale buckets of column/region
   bboxes and band counts — no text, no region ids) equals the exemplar's;
2. its normalised anchor-label multiset equals the exemplar's;
3. every reused binding's `(column, band, segment)` address resolves to a live
   element in that tab — any miss refuses reuse for the whole tab and falls
   back to a fresh model call;
4. reused fields are emitted with `ProvenanceSource.REPLAY` and no review flag
   only when the ladder was clean and the value read succeeded.

Reuse skips authoring only, never value resolution. Checkbox/bool selections are
re-derived from the target tab's own markers; a reused tab's free-text answers
are **read from that tab's own value elements** (never copied from the exemplar)
and an unreadable text value is null with `ReviewReason.REPLAY_MISMATCH`. The
exemplar map is per run and in memory only — it is never written to the store.

The six-identical-tabs case drops from 6 model calls to 1 when the flag is on; a
mixed workbook of two layouts x three tabs drops from 6 calls to 2.

## Prompt size and concurrency

`PipelineConfig(include_address=True)` restores the pre-0.5.0 prompt, which
asked the model for a per-field `address` object. It defaults to `False`: the
prompt omits that contract key (and its explanation), so each tab costs a little
less output generation. If a model returns `address` anyway it is ignored and
`draft.address` stays `None`.

`PipelineConfig(chunk_workers=N)` (1..32, default 1) resolves projection chunks
with a thread pool. Results are returned in chunk order and the call budget is
shared under a lock, so raising it only helps when the `LLMClient` is
thread-safe. With `1` the path is identical to the serial 0.4.0 path.

`PROMPT_VERSION` and `PIPELINE_VERSION` are `3`; records produced by 0.4.0 are
not served from the cache.

## Checking a real workbook safely

`python -m formextract.evals.structure_probe <path.xlsx>
[--conventions <json>]` prints one JSON object of integers only (plus one
boolean check field): tab and hidden-tab counts, markers per geometric class
(`right_only`, `between`, `left_only`, `unattached`), markers inside declared
non-answer columns, controls ambiguous / auto-selected / selected by a declared
convention, review-flag count, and per-tab region counts in tab order. It runs
only the deterministic ingest and layout (no model, no network, no store
writes) and prints no label, answer, element text, address, bbox, sheet name or
file name, so the owner can commit the output without committing source data.

## Repository layout

```
formextract/
  ingest/       # PDF/XLSX → Element stream
  layout.py     # bands, regions, glyphs, anchors, hypotheses
  resolve.py    # projection, prompt, drafts → canonical fields
  schema.py     # label canonicalization + value normalizers
  model.py      # dataclasses (Element, Field, InstanceRecord, ...)
  store.py      # content-addressed archive + idempotency
  pipeline.py   # orchestration
  evals/        # manifest, synthetic fixtures, canned LLM, harness, scoring
tests/          # pytest suite (80 tests)
docs/design/    # design notes
```

## License

Licensed under the **Apache License, Version 2.0**; see [`LICENSE`](https://github.com/iortega10/form-extract/blob/master/LICENSE) and
[`NOTICE`](https://github.com/iortega10/form-extract/blob/master/NOTICE). Copyright 2026 Ivan Ortega.

Note that the PDF ingester depends on **PyMuPDF**, which is licensed under the GNU AGPL-3.0
(or a commercial license from Artifex). It is installed separately and not bundled, but check its
terms before distributing a product that includes it.
