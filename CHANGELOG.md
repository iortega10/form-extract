# Changelog

All notable changes to this project are documented in this file. The project
follows a plain release-numbering scheme (`0.1.0`, `0.2.0`, ...) and keeps a
single schema version bump per output-changing release.

## Unreleased (0.3.0)

### Added

- **Provenance.** Every `Field` now carries a content-derived `field_id`, plus
  `tab`, `section_path` (enclosing header regions) and `source_elements`
  (resolved element ids from the model's `(region_id, band_id, segment_index)`
  pointers). `field_id` is stable for the same file + pipeline version and
  independent of tab names; editing a label retires only that field's id.
- **Content-derived region ids.** Region ids are now
  `p{page}:c{column}:{type}:{first_anchor_norm}` (disambiguated only on
  collision), no longer positional counters.
- **Hidden sheets.** Workbook `sheet_state` is captured on `SourceInfo`; hidden
  sheets are excluded from default output and can be included with
  `PipelineConfig(include_hidden_sheets=True)`.
- **Provenance coverage in the harness.** The eval report now includes
  `field_id_coverage`, `field_ids_unique`, and `items_skipped`.
- **Optional PDF backend.** `pymupdf` moved out of the base dependencies into
  the `[pdf]` extra. PDF ingest raises a typed `PdfBackendUnavailable` error
  with the install hint when the extra is absent, and the eval harness skips
  PDF items with a reported reason.
- **Right-only marker auto-select.** A unique marker with an option-like
  candidate only to its right is auto-selected geometrically; every other
  selection is either declared or flagged for review.
- **`non_answer_columns`.** `PipelineConfig(non_answer_columns=[...])` declares
  spreadsheet columns (e.g. `"Z"` or `{"tab": "CHC*", "column": "Z"}`) whose
  text is projected as annotations only and is never a marker or option.

### Changed

- `SCHEMA_VERSION` bumped to `2` (schema additions only; every new attribute
  has a default so stored 0.1/0.2 records still load).
- An unmarked option control is now null instead of `"false"` (the old value
  was a guess).
- A marker between two options with no declared convention is now null with
  `ambiguous_mark` and its candidates (previously a value that could differ
  between runs).
- `compute_cache_key` now includes `include_hidden_sheets` and
  `non_answer_columns`.
- A cached unparseable LLM original response now goes straight to the repair
  prompt instead of re-spending the original prompt.

Expected visible effect on a real checklist: roughly two thirds of populated
between-marked checkbox values become null until a convention is declared in
0.4.0.

## 0.2.0

### Added

- Reliable runs: hashed cache key (content, pipeline/schema/prompt versions,
  model, params), COMPLETE-only cache hits, non-COMPLETE runs routed to a run
  log, per-chunk isolation, transport/parse repair retries with a call budget,
  and a per-call archive read path for incremental reuse.

## 0.1.0

- Initial release: XLSX and text-layer PDF ingestion, deterministic layout
  perception, cold-path LLM binding, canonical field schema, content-addressed
  store, and the golden-set eval harness.
