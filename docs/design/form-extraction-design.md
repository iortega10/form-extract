# Generic form/document field extraction — design

Goal: a Python package that extracts structured data from non-uniform
document-based forms (canonical case: an Excel-workbook-based
compliance checklist, exported/shared as PDF), for batches where
some forms share an identical template and others are one-offs. This is a
design/build spec for hearth-cli, not a philosophy note — it should be
buildable module-by-module from the phase list below.

This doc is the outcome of a design discussion between Claude and hearth's
model (session `sac81a37a` in this project's `.hearth/sessions/`), not a
transcript of it. Where the discussion changed the starting plan, the
"why" is kept; the back-and-forth itself is not.

## Evidence the design is grounded in

A real sample PDF, inspected with `pdftotext -layout`, has a genuine text
layer (not scanned) and is a rendered export of a multi-tab Excel workbook
(it references several other tabs of the workbook by name, such as a
fees tab, a notes tab and a reference tab). A representative layout
fragment:

```
Which US states do they ship to    X All
                  if not all please check those that apply   AK AL AR AZ CA CO CT DE FL GA
                                                               HI IA ID IL IN KS KY LA MA MD
Do they ship to Canada?    Yes   X No
Vendor Status    Approved   X Provisional   Confirm on vendor portal
Order Log    X Support Order Log (Check if the vendor is exporting its order log directly)
```

This fragment single-handedly breaks the naive "row = field, nearest-token
= value" model and is why the deterministic/LLM boundary below looks the
way it does:

- `if not all please check those that apply` → a **sub-instruction**, not a
  label; its "value" is a **grid spanning two wrapped rows**.
- `Do they ship to Canada?` → the value is an **option set**
  (`{Yes, X No}`), not a single nearest token.
- `Vendor Status` → label + two options + a **trailing annotation**
  (`Confirm on vendor portal`) — "nearest" is meaningless with three
  candidates after the label.
- `Order Log` → the value is a **boolean checkbox** whose own label is
  "Support Order Log," with a parenthetical annotation on the option, not
  on the field.
- The same `X`-adjacent-to-short-token shape is **single-select** in
  `X No` and **multi-select** in the state grid — geometrically identical,
  semantically opposite. It cannot be disambiguated without group context
  (are there sibling options in the same region? is this a repeating grid?
  is there a select-all control?).

## Decided architecture

### 1. Ingestion (format-adaptive, unchanged from the original plan)

Prefer the most structured source available, in order:

1. Native `.xlsx` via `openpyxl` when the source workbook exists at all
   (grid coordinates, merged cells, fill color, multi-sheet — fully
   deterministic). Note: validate against real intake before weighting
   this as the primary path — vendor forms typically arrive as PDF, and
   xlsx may end up a minority backend rather than the default one.
2. PDF with a text layer via `pdfplumber`/`PyMuPDF` word-level bounding
   boxes — still deterministic, geometric instead of grid-based.
   `pdftotext -layout` output is itself a legitimate third PDF
   representation (whitespace-preserved 2D text), useful as a cheap,
   already-LLM-friendly view alongside word boxes, not just for manual
   inspection.
3. Page-image rendering + vision-LLM (not OCR — see Phase 5) for
   scanned/flattened PDFs with no text layer. Last resort per document,
   not the default.

All backends normalize into a common `Element` stream (see data contracts,
below) — this part of the original plan was not contested.

### 2. The deterministic/LLM boundary — the main design change

The original plan's cut point ("deterministic layer emits candidate
label/value pairs, LLM resolves ambiguity") was rejected in this specific
form. Problem: pairing/grouping — deciding what pairs with what, which
side is the label, what an `X` selects — is exactly the hard, failure-prone
part (see the fragment above), and pre-fusing it into `(label, value)`
candidates before resolution touches them means the LLM's job becomes
"un-fuse a wrong guess," which is harder than grouping correctly from
scratch. A paired candidate that's wrong is also *more* dangerous than an
unpaired token, because it looks resolved.

The resolved split is by **act**, not by **mechanism**:

- **Binding resolution** — *inventing* a grouping: which spans form a
  field, which side is the label, what the control type is, how options
  are scoped. Semantic, hard, allowed to be wrong on first sight. This is
  the LLM's job on the cold path — or a high-confidence deterministic
  rule's job, treated identically (see below).
- **Binding application (replay)** — *reapplying* an already-invented
  grouping to a new instance, plus verifying it still holds. Mechanical,
  checkable, no semantics required. This is deterministic and is what
  makes template reuse actually cheap.

Concretely, once a binding is accepted (by LLM or by a high-confidence
rule), it is **promoted** into the template registry and becomes
deterministic forever after via replay. There is exactly one mechanism
that produces deterministic grouping output — replay — never a second,
parallel "obvious case" fast path. A rule that confidently pairs a
lone label/one-row/no-siblings row still goes through binding authoring
(`provenance: heuristic`) and gets replayed like any other binding;
otherwise there are two sources of truth that can silently disagree.

**Deterministic, no LLM, ever (perception + geometry + replay):**

- tokens/spans with bbox, font, size, color; merged-cell spans; reading
  order
- column/gutter segmentation (x-projection whitespace clustering) —
  needed because these are 2-column-ish forms and page-wide row-banding
  conflates the label column with the content column
- row-banding *within* a column (not across the whole page)
- region typing: `grid | field-row | header | prose`
- glyph classification (is this token a checkmark-shaped mark)
- non-authoritative candidate hypotheses — multiple where ambiguous, raw
  spans always retained
- the replay engine: apply a stored binding to a new instance and verify
  it still holds

**Resolution, cold path only (binding authoring):** role assignment
(label vs. value), control type, option scoping / select-all linkage,
canonical field-name mapping. Output is a **binding**, not a value —
accepted bindings get promoted to the template registry.

Execution model:

| Path | Trigger | What runs |
|---|---|---|
| Cold | no matching binding | deterministic hypotheses seed resolution; LLM (or a confident rule) authors a binding |
| Warm | binding matches, replay verifies | replay only — no re-derivation, no LLM |
| Escalate | match confidence below gate, or replay verification fails | partial/delta resolution on the mismatched piece only |

Replay must **verify, not trust**: e.g. "expected anchor token still
present near its expected relative address; option count matches the
stored control's option set." A mismatch escalates rather than silently
misapplying a stale binding.

Field model: a field is `{label, control_type: single_select | multi_select
| bool | text, options[], answer(s), annotations[]}`, not a `(label,
value)` pair. `Yes / X No` is `single_select{options:[Yes,No], answer:No}`;
the state grid is `multi_select{options:[50 states], selected:[...]}`;
`Order Log` is `bool{answer:true, label:"Support Order Log"}`. The
`X All` control above the state grid is a select-all link into the grid
and must be modeled as such, not emitted as a stray value. Annotations
(`Confirm on vendor portal`, the vendor-export parenthetical) are quarantined
separately from answers so aggregation isn't contaminated by instruction
text.

### 3. Template matching — anchor-based, not a hash

A hash of the detected label sequence was rejected: it hashes a fragile,
extraction-order-dependent thing (parser choice and column wrap change
the sequence), it captures neither geometry nor semantics well, and it's
zero-tolerance — one added field or one reordered label invalidates the
whole template and forces a full LLM re-run, when drift is the norm in
real batches, not the exception.

Decided approach:

- **Identification** ("is this the same template family?"): normalized
  Jaccard similarity over anchor labels (lowercased, punctuation/whitespace
  stripped) plus order-consistency via LCS/edit-distance over the anchor
  sequence. No geometric transform needed for identification — this is a
  set/sequence decision.
- **Anchors must be occurrence-qualified**, not just label text. Repeated
  labels (`Yes`/`No` appearing dozens of times) make plain Jaccard useless
  on their own; anchors are keyed by `label + occurrence_ordinal +
  region_id`, and order-consistency is what disambiguates them.
- **Match units are region-typed.** A grid (e.g. the state list, wrapped
  across two rows) is one matchable unit, not N separate row anchors — a
  different wrap must not look like a different template.
- **Projection** ("where does field F's value live on this instance?"):
  no global affine/similarity transform. These are digitally-rendered
  exports (openpyxl grid coordinates, or pdfplumber/PyMuPDF word boxes),
  not scanned/warped images — there is no skew or rotation to correct for.
  Instead, store each binding as a **local relational address**: "the
  value is the Nth text block to the right of anchor A, within A's column
  partition and row-band." Drift that moves anchor A moves the target
  with it, with no fitted transform required — this tolerates local
  change (row insert/delete, column-width change, moved page break) better
  than a global transform would, since a transform assumes rigidity the
  document class doesn't have.
  - A cheap optional per-axis 2-parameter robust fit (uniform scale +
    translation, no rotation/shear) may be used purely as a *consistency
    check* (large residuals ⇒ probably not the same template) and to
    absorb a uniform DPI/paper rescale — not as the mechanism that finds
    field F's position.
- **Store bindings relative to anchors, never absolute coordinates.**
  Absolute coordinates are parser-version- and DPI-sensitive; anchor-
  relative addresses survive both.
- **Templates are families with variants, not one-signature-one-template.**
  Below-threshold matches go to the LLM path; if a new resolution recurs,
  it is promoted to a variant of the family (`variant_of`, `delta_vs_parent`
  in the registry record) rather than becoming an unrelated new template.
  Delta reuse follows from this: if 38 of 40 anchors match, replay the 38
  and resolve only the 2 new ones — a global match/no-match check can't
  offer this.
- **Templates are keyed by `(signature, backend)`.** xlsx sees `Yes | X No`
  as one merged cell; PDF text sees two separate words. The same logical
  form produces different candidate groupings by backend, so a template
  learned from one backend must not be silently cross-applied to the
  other.
- **Parser version is pinned per template and per instance.** A
  `pdfplumber`→`PyMuPDF` swap (or a version bump within either) can reorder
  or reposition tokens; a template whose addresses were derived under an
  older parser version should be flagged "needs re-validation" rather than
  trusted blindly.
- If the vision/scanned fallback tier (Phase 5) ever needs it, skew/
  rotation only appears there, and is handled as a **deskew preprocessing
  step** before the same local-relational model applies — a general affine
  fit is never needed anywhere in the matching path.

### 4. Storage

JSON remains canonical, not the query engine. Three record types plus
two more identified during review that the original plan omitted:

1. **Raw archive** — original file, content-hashed.
2. **Instance extraction record** — per document instance.
3. **Template registry record** — per template family/variant.
4. **Batch record** — added: `{batch_id, members[], formed_at}`. This
   matters more than it looks: the whole premise of template reuse as a
   cost lever rests on real batches having repeated layouts, and this is
   where that's *measured* (e.g., confirm the 5/4 split empirically)
   rather than assumed.

For cross-form querying: **defer DuckDB until a recurring query need
actually appears** (cross-form analytics, joining against the template
registry, "which vendors left field X blank"). JSON-on-disk plus a query
script is sufficient at tens-to-hundreds of records, and DuckDB is
rebuildable/cheap to add later — there's no cost to deferring it. When it
is added:

- Use **long format**, not a flattened wide table: one row per extracted
  field (`instance_id, field_name, value, control_type, confidence,
  source, bbox_json`). The premise of this whole system is that forms are
  non-uniform and one-offs are common, so a wide table over a heterogeneous
  schema is mostly NULLs and a maintenance burden.
- DuckDB's actual advantage here is that it reads JSON directly
  (`read_json_auto`, `union_by_name`) — the "sync" step can be a *view*
  over the canonical JSON files, no ETL. That's less machinery than
  standing up SQLite, not more.

### 5. Package shape

Correction to the original plan: the sibling ecosystem (`exec-mcp`,
`code-mcp`) does not use a `toolimpl.py` of plain callables. Its actual
split is: pure, MCP-unaware domain modules (e.g. `sandbox.py`/`guards.py`,
`codeops.py`/`gitops.py`/`search.py`), plus `tools.py` (MCP tool
registration — the tools are closures inside `register_tools(mcp, backend,
settings)`, not standalone importable callables) and `server.py`
(entrypoint). The rule to mirror is: all logic lives in plain modules with
a clean function API; MCP-awareness is quarantined to `tools.py` +
`server.py`. No `toolimpl.py`.

Module list (revised — the data contracts and the replay mechanism are
the real spine of this design and were missing from the original list):

- `model.py` — shared data contracts: `Element, Region, Field, Control,
  Option, Binding, InstanceRecord, TemplateRecord, Anchor`. This is the
  single most important addition: once the boundary is "deterministic
  replays, resolution invents," these contracts *are* the architecture
  that both sides agree on.
- `schema.py` — target schema: canonical field names, control types,
  normalizers. A Phase 0 artifact; don't bury it inside `store.py` or
  `resolve.py`.
- `ingest/{xlsx,pdf_text,pdf_image}.py` — pure I/O → `Element` stream only.
  No geometry/typing logic here.
- `layout.py` — column/gutter segmentation, row-banding, region typing,
  glyph classification, candidate hypothesis generation.
- `resolve.py` — cold-path binding **authoring** only (LLM call plus
  prompt/response archival). Read the name as "authors bindings," not
  "resolves values."
- `replay.py` — split out from `templates.py`: apply a binding + verify.
  Per "one mechanism produces all deterministic grouping output," replay
  deserves to be addressable and testable on its own.
- `templates.py` — registry storage, signature computation, fuzzy
  identification (Jaccard + LCS).
- `store.py` — instance + template + raw-archive persistence; optional
  DuckDB view (Phase 4+).
- `review.py` — low-confidence flags and the human-correction feedback
  loop (this is also the golden-set growth mechanism, see Phase 2).
- `evals/` — golden-set manifest, harness, metrics (accuracy **and**
  cost).
- `pipeline.py` — orchestration across all of the above.
- Optional thin MCP `server.py` wrapper on top, added when actually
  needed (see phasing).

## Data shapes

### Instance extraction record

Identity/versioning: `instance_id`, `batch_id`, `schema_version`,
`pipeline_version`, `run_id`, `created_at`, idempotency key
(`content_hash` + `pipeline_version`) so re-ingest is a no-op.

Source: `content_hash` (sha256), `original_filename`, `mime`, `size`,
`page_count`/`sheet_names`, `backend` chosen + why (`has_text_layer` flag,
decision path), `parser` name + version (pinned on the record, not just
logged — addresses are parser-sensitive).

Per field:
- `label_text` (kept separate from value/raw_text)
- `control_type`
- `options[]{text, selected, raw_span}` — multi-select/grid state is per
  option, not one blended value
- `select_all_ref` — link from a select-all control (`X All`) into its
  grid, so it doesn't leak as a stray value
- `annotations[]` — quarantined instruction/note text
- `bbox{page, rect}` + `region_ref` — needed to render review crops;
  `position` alone isn't renderable
- `value_raw` vs. `value_normalized` + `normalizer_id`
- `edited_by_human` / `human_value` — the correction feedback loop

Provenance (replaces a single blended `confidence` scalar, which is the
wrong shape — geometric heuristics emit fake-precise confidence and LLM
self-reported confidence is weakly calibrated; blending them hides both
problems):
- `source` enum: `xlsx | pdf_text | llm | replay | heuristic`
- `binding_id`, `binding_version`, `match_score`, `verified` (replay path)
- `heuristic_agreement[]`
- `llm_confidence` — kept as its own field, never blended in
- `review_flag` + `review_reason` enum: `low_match | replay_mismatch |
  ambiguous_role | ambiguous_mark | novel_field`
- a single derived scalar may still be shown for convenience, but must be
  marked derived/non-authoritative

Top-level, also required:
- `residuals`/`unmapped_spans` — text not consumed by any field; without
  this you can't tell "field absent from the form" from "extraction
  missed it," and it's the recall audit and delta detector
- `llm_calls[]` — `{call_id, purpose, model, params, prompt_hash,
  prompt_ref, response_ref, tokens, latency}`; prompts/responses
  content-addressed and referenced, not inlined
- `match_candidates[]` — top-K template matches with scores, even when
  one wins, so a wrong-template warm path is debuggable
- `regions`/`tabs` snapshot — so the markdown/LLM projection is
  reproducible
- `signature` computed for this instance (seeds template bucketing)
- `status` (`complete | partial | failed`) + `errors[]`

### Template registry record

- `template_id`, `family_id`, `variant_of`, `supersedes`, `version`,
  `status` (`draft | active | deprecated | merged`)
- `backends[]` — templates are keyed by (signature, backend); record it
  explicitly or risk silent cross-application
- `schema_version` targeted, and the perception/parser version the
  addresses were derived under (parser bump ⇒ mark "needs re-validation")
- `anchors[]{anchor_id, normalized_text, occurrence_ordinal, region_id,
  relative_address}`
- `match_units[]`, region-typed (a grid is one unit)
- `bindings[]{field_id, canonical_name, control_type, options[],
  anchor_relational_address, select_all_ref, annotation_policy,
  normalizer_id}`
- `provenance`: `authored_by` (`llm | heuristic | human`), `authored_at`,
  `exemplar_instance_id`, `llm_call_ref`, `approval_state`
- template health: `sample_count`, `match_score` distribution,
  `replay_success_rate`, `escalation_rate`, `last_matched_at` — a rising
  escalation rate is the signal to split the family into a new variant
- `delta_vs_parent` for variants

### Raw archive and batch records

- Raw archive: `content_hash, size, mime, original_filename, ingested_at,
  source_system, storage_ref`
- Batch: `batch_id, members[], formed_at` — this is where template-reuse
  homogeneity assumptions get measured against reality, not just assumed

## Phasing

**Phase 0 — schema, golden set, eval harness.**
Define the target schema explicitly (canonical field names, control
types) — this is a product-defining artifact that was previously implicit.
Hand-label 15-20 real forms including variants. Seed a few flattened/
vector-no-text PDFs into the golden set now (trivially produced via
print-to-PDF from the source workbook) so the Phase 5 fallback tier ships
measured, not unmeasured. Build the eval harness so it scores **two
levels** — perception (are regions/anchors right) and binding (are fields
right) — and **cost** (LLM calls/tokens per instance) as a first-class
metric, not just accuracy. Without a cost metric, the entire "warm path
skips the LLM" thesis of Phase 3 has no way to be observed as working.

**Phase 1 — perception, deterministic, plus a stateless baseline LLM
slice.**
Tokens/bbox/font/merged-cells (xlsx + pdf-text ingestion), column/gutter
segmentation, row-banding within columns, region typing, glyph
classification, non-authoritative candidate hypotheses. No binding
persistence yet. Also: a thin, **stateless** LLM slice that emits
binding-shaped output (the same `label + control_type + options + answer
+ annotations + address` contract Phase 2 will persist) without writing
it to the registry — this validates whether perception hints help or hurt
the prompt, in isolation from any stale-binding effects. The binding data
contract must be frozen in Phase 0/1 so Phase 1's output isn't throwaway
once Phase 2 starts persisting it. The instance-record store is a Phase 1
dependency (needed to compare eval runs), even though registry population
is Phase 2 — these are two different pieces of `store.py`.

**Phase 2 — binding authoring, persistence, identity replay.**
LLM (or a high-confidence heuristic, treated identically) invents
bindings and promotes them into the template registry. Must include
**minimal replay**: apply a binding back to its own exemplar and assert
round-trip — without this, stored bindings are unverifiable prose, not a
tested artifact. (This is identity replay only — no fuzzy identification,
no delta reuse yet; that's Phase 3.) Also lands here: the review/
correction surface (a CLI or JSON sidecar is enough at this stage) —
template promotion should be human-gated, and human corrections are the
golden-set growth loop.

**Phase 3 — the replay engine (fuzzy identification + projection +
delta reuse).**
Normalized Jaccard + LCS order-consistency over anchors for identification;
local relational-address projection onto *other* instances; verification
with escalation on mismatch; delta reuse (partial match resolves only the
new anchors). This is what makes the warm path actually skip the LLM
including for grouping, since replaying an established binding is not
"re-deriving" anything, any more than loading a saved schema is.

**Phase 4 — query/reporting.**
JSON stays canonical. Add a DuckDB direct-JSON-view in long format
(`instance_id, field_name, value, control_type, confidence, source,
bbox_json`) only once a recurring cross-form query need actually appears.
Optional thin MCP wrapper, whenever something needs to call this over
MCP rather than in-process.

**Phase 5 — fallback tier for flattened/scanned/no-text-layer documents.**
Vision-LLM on rendered page images, preferred over OCR + geometry — this
document class is more likely to show up as a flattened/vector "print to
PDF" export than a true scan, and vision-LLM handles that layout class
better than OCR+geometry reconstruction. If skew/rotation appears here
(the only place it can), deskew as a preprocessing step, after which the
same local-relational template model applies unchanged — no general
affine fit anywhere in the pipeline, even here. Bbox crops (rendering the
region around a field) are also useful here as a verification aid for
ambiguous fields generally, not just in the fallback tier.

## Cross-cutting requirements (apply across all phases)

- **`X` is not unambiguous.** `X` vs. `☒` vs. `✓`, and in some forms a
  mark means *unselected*. Resolve with group context; never hard-assume,
  flag when uncertain.
- **Answer vs. instruction vs. annotation.** Parenthetical instructions
  and "Confirm on vendor portal"-style notes must be normalized out into
  `annotations[]`, not left contaminating `value`.
- **LLM I/O is archived, not just logged** — pin model + params, store
  prompt/response references for reproducibility and audit.
- **Multi-tab workbooks must be chunked per region/tab** for the LLM
  projection, not sent whole-document, to control token cost and reduce
  cross-tab confusion.

## Open disagreements / risks

None remain unresolved after the discussion — every point raised (the
candidate/pairing cut point, the hash signature, DuckDB's role, the
row-banding heuristic, and the phase ordering) converged on a joint
position, captured above. Residual **risks** to flag for the build,
not disagreements:

- **xlsx availability is unvalidated against real intake.** If vendor
  forms arrive almost entirely as PDF in practice, the xlsx backend is a
  nice-to-have that should not gate Phase 1 delivery.
- **Template reuse's value is unmeasured.** The whole Phase 3 cost
  argument assumes real batches look like "5 identical, 4 different."
  The batch record (added in Storage, above) is where this gets measured;
  if real-world layout homogeneity turns out to be low, Phase 3's ROI
  should be re-evaluated before investing further in it.
- **Anchor-based matching adds real implementation complexity** (LCS
  alignment, occurrence-qualified anchors, region-typed match units)
  compared to a hash. This was accepted as necessary given the concrete
  failure modes identified (repeated labels, page-break relocation, row
  insert/delete, reflowed grids, parser tokenization drift) — but it is
  more code than the original plan's one-line hash, and should be budgeted
  as such in Phase 2/3 estimates.
