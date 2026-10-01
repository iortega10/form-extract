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

Requires Python ≥ 3.11, `openpyxl`, `pymupdf`.

## Run the test suite

```bash
python -m pytest tests -q
```

43 tests covering ingest, layout, schema normalization, store idempotency,
resolve parsing, pipeline end-to-end, and eval-harness scoring.

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
- `.xlsx` and text-layer `.pdf` are supported. Scanned/image-only PDFs raise
  `NotImplementedError` at ingest (Phase 5 vision tier) and the record comes
  back `failed` with the reason in `record.errors`.
- Every LLM call is archived under the store root (`llm/`), and the resulting
  instance JSON is written to `<store>/instances/<instance_id>.json`.

### 3. Test against the synthetic fixture directly

```python
from pathlib import Path
from formextract.evals.synthetic import build

pdf = build("spec_fragment_pdf", Path("fixtures"))   # also: spec_fragment_xlsx
```

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
tests/          # pytest suite (43 tests)
docs/design/    # design notes
```

## License

Licensed under the **Apache License, Version 2.0**; see [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE). Copyright 2026 Ivan Ortega.

Note that the PDF ingester depends on **PyMuPDF**, which is licensed under the GNU AGPL-3.0
(or a commercial license from Artifex). It is installed separately and not bundled, but check its
terms before distributing a product that includes it.
