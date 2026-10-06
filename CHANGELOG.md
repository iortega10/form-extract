# Changelog

All notable changes to this project are documented in this file. The project
follows a plain release-numbering scheme (`0.1.0`, `0.2.0`, ...) and keeps a
single schema version bump per output-changing release.

## Unreleased

### Added

- **Live provider probe (`tools/live_probe.py`).** A stdlib-only,
  out-of-package tool that measures one provider call per projection chunk on a
  real workbook (OpenAI-compatible or Gemini) and prints one JSON line of
  numbers only: input/output/reasoning token counts, `seconds`,
  tokens/second, finish-reason class, fields, parse errors, truncation,
  unresolved refs, anchor and grid counts, and status/HTTP-error classes. The
  key is read only from an environment variable, is never printed, stored or
  placed in a URL (Gemini authenticates by header), a response body is never
  printed, and prompt/response text reaches disk only under an explicit
  `--dump`. `--tiny` measures the per-call floor. Covered by a hermetic
  `http.server` test.
- **Profiler near-miss data (`tools/profile_workbook.py`).** For every pair of
  tabs with an equal geometry signature the profiler now prints the anchor
  intersection, union, total and symmetric difference (integers only), plus
  `closest_pair_diff` and a `pairs_by_diff` histogram, so near-match reuse can
  be sized up without reading any label text.
- **Row coverage (`formextract/coverage.py`, `InstanceRecord.coverage`).** Every
  run with a client now records, per tab, `units_total` / `units_consumed` /
  `ratio` over two model-free unit types — anchor rows and GRID regions — using
  the model's *resolved* element refs as the numerator and ids only, never text.
  `unbound` lists the units no field used (capped at 50, `unbound_truncated`
  marks the cut); `record.coverage_min_ratio` is the minimum over eligible tabs.
  `ratio: null` (no eligible units) is ineligible, never `1.0`. The block is
  additive and defaulted, so `SCHEMA_VERSION` stays `"2"`. The live probe
  (`tools/live_probe.py`) now reports the same numerator and denominator
  (`units_consumed` / `units_total`, with `anchors` + `grid_units` splitting the
  denominator) instead of its own ad-hoc count.
- **`PipelineConfig.min_coverage`.** Opt-in, no default. When set, every
  eligible tab with `ratio` below it appends one error
  (`tab <name>: row coverage 0.05 < 0.50`) and the run becomes `partial`. A
  rerun costs a recompute, not tokens: the `partial` record is not served from
  the instance cache, but each chunk whose response parsed without field errors
  is served from the call cache. `min_coverage` is part of
  `compute_cache_key`.
- **Truncation salvage (parser-only).** A length-truncated response — one that
  ends with a brace/bracket still open or inside a string — no longer raises out
  of `parse_drafts_with_errors` and no longer triggers the repair call (which
  re-sent the same prompt and truncated the same way, doubling the spend). A
  single linear, string/escape-aware scan keeps every complete field object
  before the cut and appends one error, `response truncated after N fields`
  (N = the complete fields kept, `0` included). The salvaged fields go through
  the usual per-field parse (a bad field is still a field error), the record
  reads `partial`, and the response is archived unindexed so a rerun re-sends
  it. A balanced-but-invalid response (a syntax error with balanced delimiters,
  trailing prose, a bad escape) still goes to the repair call, unchanged.
- **Cost levers table in the README.** The integrator guide now ranks the
  cost/latency levers (thinking off, a fast non-reasoning model, an explicit
  `max_tokens`, `chunk_workers`, `reuse_layout_bindings`) with a cost note per
  row, plus a per-provider-family `params` table.

### Changed

- **Deterministic order for drafts tied on an anchor band.** `drafts_to_fields`
  now breaks a tie on the anchor band with the smallest `(band, segment)` the
  draft cites, then the normalised label, then the draft's original position,
  instead of leaving the model's response order (a stable-sort artefact) to
  decide the `field_id` ordinals. This can change ordinals only for drafts tied
  on an anchor band, and it re-orders region-less drafts (which share the
  sentinel band) by label.
- **Cache-key note.** `min_coverage` is appended to `compute_cache_key` only
  when it is set, so every default-config key is byte-identical to 0.5.0's and
  an instance cached by 0.5.0 is still served under the default config — it
  simply carries `coverage=[]` until the cache is cleared or `force=True` is
  used. That is the cost of not invalidating: the field is additive, so
  `SCHEMA_VERSION` does not move.

### Fixed

- **A 4xx is no longer retried.** `resolve._complete_with_retries` made three
  attempts with backoff for *any* exception. A new `resolve.NonRetryable`
  marker (raise it, or wrap the provider's error in it) plus a total attribute
  sniff (`status_code`/`code`/`response.status_code`, accepted as an int, a
  digit string or an `IntEnum`) makes a 4xx cost exactly one call, while 408,
  429, 5xx, timeouts and unknown exceptions retry as before. The provider's own
  reason still reaches the record unchanged as `transport failed: {exc}`.
- **`force=True` now re-spends, as documented.** `Pipeline.run(force=True)`
  previously bypassed only the instance cache; each chunk was still served from
  the per-prompt call cache, so an unchanged prompt made zero model calls. It
  now also skips the call-cache lookup (`author_drafts(..., force=True)` threaded
  through the serial, `chunk_workers` and reuse paths), calls the model once per
  chunk, and indexes the fresh response exactly as a non-forced run would. The
  call budget still applies, and `force=False` is byte-identical to before.

No cache key, `PROMPT_VERSION`, `PIPELINE_VERSION`, `SCHEMA_VERSION` or default
`params` changed in these entries.

## 0.5.0 (2026-10-06)

### Changed

- **Smaller default prompts.** `PipelineConfig.include_address` now defaults to
  `False`, so `build_prompt()` omits the `address` key from the output contract
  and the bullet that explains it. A model that returns `address` anyway has it
  ignored (`draft.address` stays `None`). `PROMPT_VERSION` and
  `PIPELINE_VERSION` are now `3`, so cached 0.4.0 records are not served.
- **Failures are not cached.** A response is indexed in the LLM lookup cache
  only when it parses **and** every field in it parses
  (`parse_drafts_with_errors` returns no field errors). A response that parses
  but carries a per-field error is still archived (prompt/response files and the
  run's `llm_calls` record), just not indexed, so the same partial answer is not
  served on a rerun. The rule covers the original response and a repair
  response. A 0.4.0-era cached unparseable entry is now a cache miss (the
  original prompt is re-sent) rather than a shortcut to the repair prompt.
- **Optional concurrent chunk resolution.** `PipelineConfig.chunk_workers`
  (1..32, default 1) authors chunks with a thread pool; results stay in chunk
  order and the call budget is enforced under a lock. Only helpful when the
  `LLMClient` is thread-safe.

### Fixed

- **Tab naming after hidden sheets.** Pages are numbered across all workbook
  sheets (hidden ones included) but `tabs` held only visible names. Chunk keys
  and `Field.tab` now use a page-to-tab map built from each page's element
  `sheet`, so a workbook whose first sheet is hidden no longer shifts every
  later tab name. This changes output for such workbooks.

## 0.4.0 (2026-10-06)

This release also carries the 0.3.1 fix below: 0.3.1 was never tagged or published on its
own.

### Added

- **`checkbox_conventions`.** Callers can declare, per tab and anchor pattern,
  which way a marker between two options points (`mark_precedes_option` /
  `mark_follows_option`); the package then selects deterministically. A
  declaration applies only to markers between two options, and there is no
  default because real forms mix conventions. Without a declaration a
  between-marker stays ambiguous.
- **Structure probe (integers-only).** `python -m
  formextract.evals.structure_probe` prints a deterministic, integers-only
  census of a workbook for safe inspection of a real document.
- **Opt-in layout-signature reuse (in-memory).** With
  `PipelineConfig(reuse_layout_bindings=True)`, tabs in one workbook that share
  the same quantised geometry signature and normalised anchor-label multiset are
  authored once; later tabs replay the binding against their own elements
  (`ProvenanceSource.REPLAY`) and skip the LLM call. The flag defaults to
  `False`, which keeps the 0.3.1 single-call authoring path byte-for-byte (one
  call per tab, `REPLAY` never emitted). Reuse authorises only the binding
  shape, behind a four-step ladder: equal geometry signatures, equal normalised
  anchor-label multisets, every reused binding's `(column, band, segment)`
  address resolving to a live element in the target tab (any miss refuses the
  whole tab and falls back to a fresh call), and a successful value read before
  a clean `REPLAY` without a review flag. Values are never copied: checkbox/bool
  selections are re-derived from the target tab's own markers, and free-text
  answers are read from the target tab's own value elements (an unreadable text
  value is null with `ReviewReason.REPLAY_MISMATCH`). The exemplar map is per
  run and in memory only, never persisted.

## 0.3.1 (not released separately; included in 0.4.0)

### Fixed

- A field's own label is no longer counted as an option next to a marker, so
  `Label | X | Option` rows are right-only and auto-select instead of being
  ambiguous.

### Changed

- `PIPELINE_VERSION` is now `2`, so records produced by 0.3.0 are not served
  from the cache.

## 0.3.0 (2026-10-05)

This release also carries the 0.2.0 reliability work below: 0.2.0 was never tagged or
published on its own.

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
  spreadsheet columns (e.g. `"Z"` or `{"tab": "Checklist*", "column": "Z"}`) whose
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

Expected visible effect on a real checklist: roughly 18 of 32 markers on the
reference sheet are genuinely ambiguous between two options and stay null until
a convention is declared; the other single-checkbox rows were over-nulled in
0.3.0 and are fixed in 0.3.1.

## 0.2.0 (not released separately; included in 0.3.0)

### Added

- Reliable runs: hashed cache key (content, pipeline/schema/prompt versions,
  model, params), COMPLETE-only cache hits, non-COMPLETE runs routed to a run
  log, per-chunk isolation, transport/parse repair retries with a call budget,
  and a per-call archive read path for incremental reuse.

## 0.1.0

- Initial release: XLSX and text-layer PDF ingestion, deterministic layout
  perception, cold-path LLM binding, canonical field schema, content-addressed
  store, and the golden-set eval harness.
