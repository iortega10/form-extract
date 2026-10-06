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
