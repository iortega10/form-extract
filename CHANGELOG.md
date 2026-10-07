# Changelog

All notable changes to this project are documented in this file. The project
follows a plain release-numbering scheme (`0.1.0`, `0.2.0`, ...) and keeps a
single schema version bump per output-changing release.

## Unreleased

### Added

- **Row lattice, a derived view (`formextract/rows.py`).** A page-global lattice
  over the existing layout bands, the coordinate the 0.6.0 line contract will
  cite cells by (`row.seg`). `build_rows(layout, elements_by_id, *, page)` groups
  the page's **short** bands (height <= `SHORT_BAND_FACTOR` (1.5) x the median
  band height) into rows by their top edge, then assigns every tall band by its
  top edge — a band above/below every row makes a leading/trailing row, a top
  edge in a gap goes to the smaller row index — so a tall merged label lands on
  its top row without fusing two rows. Each row exposes its `(column, band_id)`
  entries (column x-order) and its element ids in segment order, plus
  `row_of_element`, `segment` and `ref_for` (a segment translated back to today's
  `(region_id, band_id, segment_index)`). It is a frozen view: never attached to
  a `LayoutResult`, never added to an `InstanceRecord`, never serialised. layout,
  the projection, the prompt and every existing record byte are unchanged;
  `layout.py` is untouched.
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
- **Synthetic gold set and exact scorer (`tools/make_gold.py`,
  `tools/score_gold.py`).** A grouping-quality measure that is independent of the
  contract and of any model. `make_gold.py` (openpyxl only, never imports the
  package) writes deterministic `.xlsx` tabs whose correct fields are known by
  construction, plus a gold JSON and manifest keyed by the xlsx element ids the
  ingest emits (`Sheet!row:col`); the dev set is 16 tabs (~245 fields): twelve
  homogeneous tabs and four **mixed** tabs (60-100 rows, 9+ structure kinds each,
  a 50-option grid and a 12-row yes/no run in every one) whose fields also carry
  the `mixed_tab` tag, and the held-out set is a different seed and vocabulary.
  Each tab records the checkbox convention its generator used, in the exact shape
  `PipelineConfig.checkbox_conventions` takes, and `--print-conventions
  GOLD.json` prints that list. `score_gold.py` (stdlib only) compares a record
  JSON dump against gold and prints strict field precision/recall (no half
  credit), `merge_count`/`split_count`/`missed`/`spurious`,
  `stray_ref_count`/`unresolved_ref_count`, the matched-pair
  `label_ok`/`options_ok`/`answer_ok`/`selected_ok`,
  `selected_ok_under_convention`/`selected_ambiguous_expected`/
  `unexpected_ambiguous`, and `addressed` (perception), overall and per tag —
  integers, percentages and tag names only, never a label, cell text or tab name,
  so it is safe on private gold.
- **`tools/live_probe.py --gold DIR`.** A whole-set live run: every tab of a
  `make_gold.py --out` directory is run through the real `Pipeline` (the gold's
  own conventions declared, a throwaway store in a temp dir, `--workers` as
  `chunk_workers`) and one JSON line of numbers is printed — the scorer's
  headline counters plus per-tag `matched`/`gold` and the probe's usual cost
  keys. `--record-out PATH` writes the combined record dump only when asked, and
  a directory whose tabs no longer match the manifest sha256 is refused (exit 2).
  Key safety is unchanged.
- **Line-contract parser (`formextract/lines.py`) and
  `LLMResponse.finish_reason`.** The response text of the planned 0.6.0 `lines`
  output contract now has a pure, total parser (`parse_lines`) that turns it into
  line records (`kind L= O= A= N=`, refs `row.seg`, join `+`, span `-`, quoted
  literals) and never raises. It is lenient (case-insensitive kinds and keys,
  three-letter kinds, `:` for `=`, an optional `r` before a row, whitespace around
  commas), degrades an unknown kind to a review-flagged field, and records an
  unparsable line between records as `line N: ...`, while text before the first
  record or after `end` is counted `noise_lines`. A ref that cannot exist (a
  cross-row span, a malformed ref) becomes a sentinel ref for the resolver to
  reject, never a line error. The section-3.5 truncation rule is applied: a
  recognised-cut finish reason, or an `unknown` reason with no `end`, marks the
  parse `truncated` and drops the trailing line, with the error
  `response truncated after N lines`. `LLMResponse` gains an optional, defaulted
  `finish_reason` the caller fills (normalised by `lines.normalize_finish_reason`);
  the probe's client now sets it. The default output contract is still `json`;
  this turn touches no resolver.
- **Line-contract parser, totality and bounds (L1a-fix).** The parser was
  hardened against inputs a model can emit: every number is validated *before*
  conversion (ASCII digits only, at most `MAX_DIGITS` (9) of them - a longer or
  non-ASCII run, Arabic-Indic and fullwidth included, is a sentinel, never an
  `int()` and never a `ValueError`); `_digits_to_int` is the module's only `int()`
  call and `re`'s `\d` is not used. A span wider than `MAX_SPAN_WIDTH` (10,000),
  reversed or cross-row is a sentinel and stays unexpanded. A literal is allowed
  under `O`/`N` only - under `L`/`A` it is a sentinel item - and a record is
  `review=True` when it holds a sentinel, when a duplicate `O` occurrence of an
  `L` element is dropped, or when its kind is unknown, so the resolver can flag
  the field without re-scanning. `LineStats` gains `lines_total`, `error_lines`,
  `refs_total`, `refs_bad`, `truncated`, `dropped_tail`, `unknown_kind`,
  `no_label` and `empty_response` beside `records`/`noise_lines`/
  `literal_items`/`end_seen`/`end_missing`. An empty or noise-only response is no
  longer reported as a truncation - nothing was cut - but as one `response empty`
  error with `stats.empty_response` (the pipeline still marks the chunk PARTIAL).
  The trailing segment is now decided *before* it is parsed, so a dropped tail
  costs one step, not one per item (a 10 MB single line: 9.1 s fully parsed,
  0.011 s when dropped), asserted through the repo's `steps` counter.
- **The lines output contract, behind `PipelineConfig.output_contract="lines"`
  (L1b).** The default stays `"json"` (the 0.5.0 contract, byte-identical). In
  lines mode the projection is one line per page-global lattice row
  (`project_lines_chunks`, a new function; `project_chunks` is untouched), the
  prompt is `build_lines_prompt` (the section 3.2-3.4 grammar), and the response
  is read by `parse_lines_response`, which resolves every `row.seg` ref through
  the lattice, expands spans, and derives one `BindingDraft` per field line
  exactly as section 4 says: the label is the `L` cells' `strip_marks` text in
  reading order (never model text), options are one per `O` element (`+` joins
  into one, literals as written, an empty `O` cell is a mark), annotations are
  the `N` cells, the `A` cells are the text answer for a text field and a
  second-opinion selection otherwise, `region_id` and the anchor band come from
  the reading-order-first `L` cell, and `hdr`/`note`/`skip` lines produce no
  field but are returned as dispositions. Every marker classification the
  field's `L`/`O`/`A` cells touch is injected into the field's glyph set, so
  `_geometric_mark_decision` sees a mark the model did not cite (a
  BETWEEN/COMPETING marker shared by two side-by-side fields over-flags both
  with `AMBIGUOUS_MARK`). A ref that resolves to nothing becomes a sentinel that
  fails the resolver's bounds check into `unresolved_source_refs`, so a bad
  response can never raise. There is no repair call on this path (a parser has
  nothing to repair) and a response is indexed in the call cache only when it
  parsed with no line errors, was not cut and was not empty; a bad response is
  listed as an `LLMCall` either way. `BindingDraft` gains a defaulted
  `answer_refs` (the `A` refs of a text field, empty on the json path) and a
  defaulted `review_reason`; `reuse.apply_binding` re-reads a replayed text
  answer from the TARGET tab's `answer_refs`, which fixes the one-region defect
  the "drop the refs in the label region" rule has — for the lines path only,
  since the json path leaves `answer_refs` empty and keeps its rule unchanged.
  `drafts_to_fields` now also cross-checks a `declared_convention` decision
  against the stated selection (it had no check before).
- **The 0.6.0 offline harness for the lines contract.** `tools/make_gold.py
  --canned-lines DIR` writes `lines_<set>.json`, the perfect response per tab
  plus five line-level mutations (`drop_line`, `wrong_kind`, `merge_two`,
  `split_one`, `cut_before_end`) in cell-id form (`hdr L={Hold01!1:1}`): the
  generator never imports `formextract`, so it cannot know the `row.seg` a
  lattice assigns a cell, and `tests/gold_lines.py` substitutes each `{cell}`
  with that coordinate. `tests/test_060_lines_gold.py` drives the real
  `Pipeline` with those responses and scores them with the stdlib scorer:
  perfect lines reach strict 1.0 on dev (244/244) and held-out (117/117), the
  mutation table moves exactly the named counters, and the failure modes (a bad
  ref into `unresolved_source_refs`, an unknown kind into `AMBIGUOUS_ROLE`, a
  cut into a dropped tail and a PARTIAL run, an empty response) are pinned.
  `tools/live_probe.py --gold DIR --contract {json,lines}` (default `json`)
  lets the reviewer measure both contracts on one gold set under one version.

### Known issues

- **The lines contract cannot derive a mark that shares its band with several
  options, so it loses selections the json contract keeps.** A mark in a
  spreadsheet row gets every option-like cell to its right as a candidate, so it
  classifies `COMPETING` (or `BETWEEN`); `_geometric_mark_decision`'s
  short-circuits stay ahead of the right-only union and `_apply_declared_convention`
  converts only `BETWEEN`, so a row holding several options in one band (a
  single-row Yes/No control, a dense multi-select, a 50-option grid) yields no
  mark at all where the model states none. Measured on the gold set:
  `selected_ok` `100/180` (dev) and `57/91` (held-out) for `lines`, against
  `180/180` for `json` with the same declared conventions; `stacked_multi`,
  `matrix` and `typed_value` derive every mark. This is the gap T8 left on
  purpose; the fix is a resolver semantics change (union the
  convention-eligible `competing`/`between` marks per field), out of L1b's stop
  line, and `tests/test_060_lines_gold.py::test_marker_rows_that_geometry_cannot_derive`
  pins the numbers so a fix must update the pin deliberately.

### Changed

- **`PROMPT_VERSION` and `PIPELINE_VERSION` are now `"4"`.** The lines output
  contract ships beside the JSON one, so every instance key moves once.
  `SCHEMA_VERSION` stays `"2"`: `BindingDraft.answer_refs`/`review_reason` are
  defaulted and additive. The default json prompt and projection are
  byte-identical to 0.5.0 and the call cache keys on the prompt text, so cached
  0.5.x calls are still served even though the instance key misses.
  `compute_cache_key` appends one canonical `contract=<name>` token whenever
  `output_contract` is not the json default, so a toggled contract can never
  alias a stale instance.

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
- **Gold schema v2 and the selection split.** `gold_version` moves to `2`: the
  gold JSON gains a per-tab `checkbox_conventions` list and the manifest gains
  `homogeneous_tabs`/`mixed_tabs` (and `mixed_tab` joins the tag vocabulary). The
  scorer's `selected_ok` is now the sum of `selected_ok_under_convention` (a field
  the resolver decided) and `selected_ambiguous_expected` (a gold
  `expected_ambiguous` field), with a new per-tag `unexpected_ambiguous` for a
  predicted ambiguity on a field the gold does not expect to be ambiguous — the
  signal that a convention was not declared or could not apply. Both tools stay
  stdlib/openpyxl only and no package version constant moves.

### Fixed

- **A vertically stacked multi-select keeps every marked option.** The geometric
  mark decision applied only the *first* right-only marker's candidate
  (`_geometric_mark_decision` returned `auto_select` for `right_only[0]`) and
  `drafts_to_fields` then set exactly one option's `selected`, overwriting the
  model's other selections — and flagged `AMBIGUOUS_MARK` only when the model had
  also set the others. A dense row never hit it (interior marks classify BETWEEN,
  the first sees several candidates: COMPETING), but a vertically stacked
  multi-select groups one `X Option` per band, so every marker is RIGHT_ONLY with
  a single candidate. The decision now carries the **union** of the field's
  right-only single-candidate markers (`_MarkDecision.option_texts`),
  de-duplicated by normalised option text, and `drafts_to_fields` marks every
  matched option (and the model cross-check compares the whole set). A marker
  whose candidate is missing or whose stripped text is empty declines for itself:
  it is recorded (`_MarkDecision.declined_marker_element_ids`), never cancels a
  selectable marker, and forces an `AMBIGUOUS_MARK` review flag; the BETWEEN and
  COMPETING short-circuits and declared conventions are unchanged. A field with
  exactly one right-only marker is byte-identical to before. `PROMPT_VERSION`,
  `PIPELINE_VERSION` and `SCHEMA_VERSION` do not move — this changes record
  *values* for stacked multi-selects only, so cached instances of such forms are
  stale until `PIPELINE_VERSION` moves with L1b.
- **`Field.tab` is populated for a region-less field.** `drafts_to_fields` set
  `tab` only when the model's `region_id` resolved, so a field whose region did
  not resolve carried `tab=None` — and `_matching_conventions` skips its tab
  filter for a null tab, so *every* declared `checkbox_conventions` selector
  matched and the longest/most specific one could win even when it belonged to a
  different tab. `BindingDraft` gains a defaulted `tab`, `author_drafts` sets it
  from the projection chunk's key, and `drafts_to_fields` falls back to it when
  the region is missing; when a region resolves the computed tab is unchanged and
  agrees with the carried one. `field_id` is unchanged (it never includes the
  tab). `SCHEMA_VERSION` and `PROMPT_VERSION` do not move; `PIPELINE_VERSION` is
  deliberately **not** bumped here even though a run's output changes (a
  region-less field now carries `tab`): 0.6.0 is unreleased and the planned
  `PIPELINE_VERSION` `"4"` move lands with the contract turn, which must also
  invalidate such cached entries.
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
