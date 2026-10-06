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
        text = call_my_llm(prompt)  # your provider call
        return LLMResponse(text=text, model=model, params=params, tokens=123)


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

**Model and client.** Use a fast non-reasoning model, switch reasoning/thinking
off, set a modest `max_tokens`, keep `temperature` 0 (the default `params`), and
return the model's raw text. Do not wrap the call in another agent loop and do
not issue per-tab sub-calls yourself: the pipeline already chunks per tab, and
`chunk_workers` runs those chunks in parallel. A slow client call is the first
thing to check when a run feels slow — every archived call carries `latency_ms`.

**Levers, most effective first.**

1. `include_address=False` — the default; the per-field `address` object grows
   every prompt and every response.
2. `chunk_workers=N` — authors N tabs concurrently; the client must be
   thread-safe.
3. `reuse_layout_bindings=True` — skips authoring for a tab whose quantised
   geometry **and** normalised anchor labels both match an earlier tab. It can
   legitimately never fire: real forms rarely repeat exactly.
4. `non_answer_columns=[...]` — keeps reference/tag columns out of the
   projection, so prompts stay smaller and those columns are never read as
   answers.
5. `include_hidden_sheets` — left at its default `False`; hidden sheets cost
   nothing because they are never authored.

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

**Pitfalls.**

- `force=True` re-spends every call; it does not reuse the call cache.
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
