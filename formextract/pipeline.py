"""Orchestration: ingest → perception → (cold-path authoring) → instance record."""
from __future__ import annotations

import fnmatch
import hashlib
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl.utils import column_index_from_string

from .coverage import compute_coverage
from .ingest import IngestResult, ingest
from .layout import analyze, page_anchor_multiset, page_geometry_signature
from .model import (
    CHECKBOX_MARK_FOLLOWS_OPTION,
    CHECKBOX_MARK_PRECEDES_OPTION,
    PIPELINE_VERSION,
    CheckboxConvention,
    InstanceRecord,
    InstanceStatus,
    LLMCall,
    SourceInfo,
    Span,
    TabCoverage,
)
from .resolve import (
    LLMClient,
    PROMPT_VERSION,
    ResolutionError,
    author_drafts,
    drafts_resolve_cleanly,
    drafts_to_fields,
    project_chunks,
    remap_drafts_for_page,
)
from .reuse import apply_binding
from .schema import SCHEMA_VERSION, normalize_label
from .store import Store, canonical_json, content_hash

_MIME = {".xlsx": "xlsx", ".pdf": "pdf"}


def _valid_column_letters(column: str) -> bool:
    return 1 <= len(column) <= 3 and column.isalpha()


def _canonical_non_answer_columns(selectors) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for sel in selectors or []:
        if isinstance(sel, str):
            normalized.append({"tab": "*", "column": sel.upper()})
        else:
            normalized.append(
                {"tab": sel.get("tab", "*"), "column": sel["column"].upper()}
            )
    return sorted(normalized, key=lambda item: (item["tab"], item["column"]))


def validate_non_answer_columns(selectors) -> None:
    """Raise ValueError for a malformed `non_answer_columns` selector.

    Accepted shapes: ``"Z"`` (every tab) or ``{"tab": "Checklist*", "column": "Z"}``.
    """
    if selectors is None:
        return
    if not isinstance(selectors, (list, tuple)):
        raise ValueError("non_answer_columns must be a list of selectors")
    for sel in selectors:
        if isinstance(sel, str):
            if not _valid_column_letters(sel.strip()):
                raise ValueError(
                    f"invalid non_answer_columns selector {sel!r}: "
                    "column must be 1-3 letters"
                )
            continue
        if isinstance(sel, dict):
            unknown = set(sel) - {"tab", "column"}
            if unknown:
                raise ValueError(
                    f"invalid non_answer_columns selector keys: {sorted(unknown)}"
                )
            column = sel.get("column")
            if not isinstance(column, str) or not _valid_column_letters(column.strip()):
                raise ValueError(
                    "non_answer_columns selector 'column' must be 1-3 letters"
                )
            tab = sel.get("tab", "*")
            if not isinstance(tab, str) or not tab:
                raise ValueError(
                    "non_answer_columns selector 'tab' must be a non-empty glob string"
                )
            continue
        raise ValueError(
            f"invalid non_answer_columns selector {sel!r}: "
            "expected a column string or {'tab': glob, 'column': letters}"
        )


def validate_chunk_workers(value: int) -> None:
    """Raise ValueError unless `chunk_workers` is an int in 1..32 (bool excluded)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("chunk_workers must be an integer between 1 and 32")
    if not 1 <= value <= 32:
        raise ValueError("chunk_workers must be between 1 and 32")


def validate_min_coverage(value) -> None:
    """Raise ValueError unless `min_coverage` is None or a number in (0, 1]."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("min_coverage must be None or a number in (0, 1]")
    if not 0 < float(value) <= 1:
        raise ValueError("min_coverage must be in (0, 1]")


def _normalize_checkbox_conventions(selectors) -> list[CheckboxConvention]:
    """Normalise and validate `checkbox_conventions` selectors.

    Accepts ``CheckboxConvention`` instances or plain dicts with the same keys;
    raises ``ValueError`` naming the offending entry for any malformed form.
    """
    if selectors is None:
        return []
    if not isinstance(selectors, (list, tuple)):
        raise ValueError("checkbox_conventions must be a list of selectors")
    out: list[CheckboxConvention] = []
    for i, sel in enumerate(selectors):
        if isinstance(sel, CheckboxConvention):
            out.append(sel)
            continue
        if isinstance(sel, dict):
            unknown = set(sel) - {"tab", "anchor_pattern", "convention"}
            if unknown:
                raise ValueError(
                    f"invalid checkbox_conventions selector keys: {sorted(unknown)}"
                )
            if "tab" not in sel:
                raise ValueError(f"checkbox_conventions entry {i} is missing 'tab'")
            try:
                out.append(CheckboxConvention(**sel))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid checkbox_conventions entry {i}: {exc}"
                ) from exc
            continue
        raise ValueError(
            f"invalid checkbox_conventions entry {i}: "
            "expected a CheckboxConvention or a dict with "
            "{tab, anchor_pattern, convention}"
        )
    _validate_checkbox_conventions(out)
    return out


def _validate_checkbox_conventions(conventions: list[CheckboxConvention]) -> None:
    """Raise ``ValueError`` for a malformed or conflicting convention list."""
    for i, c in enumerate(conventions):
        if not isinstance(c.tab, str) or not c.tab:
            raise ValueError(
                f"checkbox_conventions entry {i} 'tab' must be a non-empty string"
            )
        if c.anchor_pattern is not None:
            if not isinstance(c.anchor_pattern, str):
                raise ValueError(
                    f"checkbox_conventions entry {i} 'anchor_pattern' must be "
                    "a string or None"
                )
            if len(c.anchor_pattern) > 200:
                raise ValueError(
                    f"checkbox_conventions entry {i} 'anchor_pattern' exceeds "
                    "200 characters"
                )
            try:
                re.compile(c.anchor_pattern)
            except re.error as exc:
                raise ValueError(
                    f"checkbox_conventions entry {i} has an invalid regex: {exc}"
                ) from exc

    by_selector: dict[tuple[str, str | None], list[CheckboxConvention]] = {}
    for c in conventions:
        by_selector.setdefault((c.tab, c.anchor_pattern), []).append(c)
    for (tab, anchor), group in by_selector.items():
        distinct = {c.convention for c in group}
        if len(distinct) > 1:
            raise ValueError(
                f"conflicting checkbox_conventions on tab {tab!r} "
                f"anchor_pattern {anchor!r} tie on specificity: "
                f"{sorted(distinct)}"
            )


def _canonical_checkbox_conventions(selectors) -> list[dict[str, str | None]]:
    normalized = _normalize_checkbox_conventions(selectors)
    items = [
        {
            "tab": c.tab,
            "anchor_pattern": c.anchor_pattern,
            "convention": c.convention,
        }
        for c in normalized
    ]
    return sorted(
        items,
        key=lambda item: (
            item["tab"],
            item["anchor_pattern"] or "",
            item["convention"],
        ),
    )


def _spreadsheet_column(element_id: str) -> int | None:
    try:
        return int(element_id.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return None


def _resolve_non_answer_element_ids(elements, selectors) -> set[str]:
    out: set[str] = set()
    for sel in selectors or []:
        if isinstance(sel, str):
            letters, tab_glob = sel, "*"
        else:
            letters, tab_glob = sel["column"], sel.get("tab", "*")
        column_index = column_index_from_string(letters)
        for e in elements:
            if e.sheet is not None and fnmatch.fnmatch(e.sheet, tab_glob):
                if _spreadsheet_column(e.element_id) == column_index:
                    out.add(e.element_id)
    return out


def compute_cache_key(
    *,
    content_hash: str,
    pipeline_version: str,
    schema_version: str,
    prompt_version: str,
    model: str,
    params: dict[str, Any],
    include_hidden_sheets: bool = False,
    non_answer_columns: tuple[Any, ...] = (),
    checkbox_conventions: tuple[Any, ...] = (),
    reuse_layout_bindings: bool = False,
    include_address: bool = False,
    min_coverage: float | None = None,
) -> str:
    params_hash = hashlib.sha256(
        canonical_json(params).encode("utf-8")
    ).hexdigest()
    non_answer_hash = hashlib.sha256(
        canonical_json(_canonical_non_answer_columns(non_answer_columns)).encode("utf-8")
    ).hexdigest()
    conventions_hash = hashlib.sha256(
        canonical_json(_canonical_checkbox_conventions(checkbox_conventions)).encode("utf-8")
    ).hexdigest()
    parts = [
        content_hash,
        pipeline_version,
        schema_version,
        prompt_version,
        model or "none",
        "1" if include_hidden_sheets else "0",
        params_hash,
        non_answer_hash,
        conventions_hash,
        "1" if reuse_layout_bindings else "0",
        "1" if include_address else "0",
    ]
    # Appended only when set, so every default-config key stays byte-identical
    # to 0.5.0's; an instance cached by 0.5.0 is still served under the default
    # config (and carries coverage=[], the cost of not invalidating).
    if min_coverage is not None:
        parts.append(f"mincov={float(min_coverage)!r}")
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def page_tabs_for(ing: IngestResult) -> dict[int, str]:
    """Map every page to its own sheet name, falling back to ``page N``.

    Shared by ``Pipeline.run`` and ``tools/profile_workbook.py`` so both name
    projection chunks the same way.
    """
    page_tabs: dict[int, str] = {}
    if ing.sheet_names:
        for page in range(ing.page_count or 0):
            page_tabs[page] = (
                ing.sheet_names[page]
                if page < len(ing.sheet_names)
                else f"page {page}"
            )
            for e in ing.elements:
                if e.bbox.page == page and e.sheet is not None:
                    page_tabs[page] = e.sheet
                    break
    else:
        for page in range(ing.page_count or 0):
            page_tabs[page] = f"page {page}"
    return page_tabs


@dataclass
class PipelineConfig:
    """Every knob that changes a run's calls, and therefore its cost.

    ``model`` (default ``"gpt-4o-mini"``) and ``params`` (default
    ``{"temperature": 0}``) identify the LLM; a fast non-reasoning model with
    thinking off is what keeps one call per tab cheap. The default ``params``
    stays ``{"temperature": 0}``: a reasoning-model integrator must pass their
    own ``params`` (those models reject ``temperature``, e.g. HTTP 400) and the
    change moves the cache key. ``purpose`` (default
    ``"cold_binding"``) namespaces the call-cache key. ``include_hidden_sheets``
    (default ``False``) keeps hidden sheets out of the projection, so they cost
    no call at all. ``non_answer_columns`` (default empty) projects those
    columns as annotations only, shrinking each prompt and stopping reference
    text being read as an answer. ``checkbox_conventions`` (default empty)
    resolves between-markers deterministically and changes no call.
    ``reuse_layout_bindings`` (default ``False``) skips authoring for tabs whose
    quantised geometry and anchor labels both match an earlier tab, trading
    fewer calls for an exact-match requirement that can legitimately never
    fire. ``include_address`` (default ``False``) drops the per-field
    ``address`` object from the contract - the largest single prompt/output
    saving; ``True`` restores the pre-0.5.0 prompt. ``chunk_workers`` (default
    ``1``, allowed 1..32) authors that many tabs at once, which cuts wall-clock
    time without changing per-call cost, and requires a thread-safe client.
    ``min_coverage`` (default ``None``, opt-in) turns the per-tab row-coverage
    block into a status rule: every eligible tab whose ratio is below it adds
    one error and makes the run ``partial``. No threshold is defensible from one
    file, so the integrator picks it; ``None`` leaves status and errors exactly
    as they were before the block existed.
    """

    model: str = "gpt-4o-mini"
    params: dict[str, Any] = field(default_factory=lambda: {"temperature": 0})
    purpose: str = "cold_binding"
    include_hidden_sheets: bool = False
    non_answer_columns: list[Any] = field(default_factory=list)
    checkbox_conventions: list[Any] = field(default_factory=list)
    reuse_layout_bindings: bool = False
    include_address: bool = False
    chunk_workers: int = 1
    min_coverage: float | None = None


class Pipeline:
    def __init__(
        self,
        store: Store,
        llm_client: LLMClient | None = None,
        config: PipelineConfig | None = None,
    ):
        self.store = store
        self.llm_client = llm_client
        self.config = config or PipelineConfig()
        validate_non_answer_columns(self.config.non_answer_columns)
        validate_chunk_workers(self.config.chunk_workers)
        validate_min_coverage(self.config.min_coverage)
        self._checkbox_conventions = _normalize_checkbox_conventions(
            self.config.checkbox_conventions
        )

    def _author_with_reuse(self, chunks, layout, elements, *, force: bool = False):
        """Author each tab once and replay geometrically identical tabs.

        Per workbook and in memory only: the exemplar map lives for the duration
        of one ``run`` call and is never persisted. A tab reuses an earlier
        exemplar only when its geometry signature and normalised anchor-label
        multiset both match, and every remapped binding resolves to a live
        element in the current tab. Any miss falls back to a fresh call for
        that tab; resolution still runs against the current tab's own elements.
        """
        elements_by_id = {e.element_id: e for e in elements}
        region_bbox = {r.region_id: r.bbox for r in layout.regions}

        # Phase A: key every chunk and pick the first chunk of each distinct
        # (geometry signature, anchor multiset) key as its exemplar.
        chunk_pages: list[int] = []
        chunk_keys: list[tuple[str, tuple[str, ...]]] = []
        exemplar_index_by_key: dict[tuple[str, tuple[str, ...]], int] = {}
        exemplar_indices: list[int] = []
        for idx, chunk in enumerate(chunks):
            page = chunk.page if chunk.page is not None else idx
            key = (
                page_geometry_signature(layout, elements_by_id, page),
                page_anchor_multiset(layout, page),
            )
            chunk_pages.append(page)
            chunk_keys.append(key)
            if key not in exemplar_index_by_key:
                exemplar_index_by_key[key] = idx
                exemplar_indices.append(idx)

        def author_one(pair):
            idx, chunk = pair
            return author_drafts(
                [chunk],
                self.llm_client,
                model=self.config.model,
                params=self.config.params,
                store=self.store,
                regions=layout.regions,
                purpose=self.config.purpose,
                include_address=self.config.include_address,
                force=force,
            )

        def author_many(indices):
            pairs = [(idx, chunks[idx]) for idx in indices]
            if self.config.chunk_workers <= 1:
                return [author_one(pair) for pair in pairs]
            with ThreadPoolExecutor(max_workers=self.config.chunk_workers) as executor:
                return list(executor.map(author_one, pairs))

        # Phase B: author the exemplars (concurrently when configured).
        exemplar_results = author_many(exemplar_indices)
        exemplar_drafts_by_key: dict[tuple[str, tuple[str, ...]], tuple[int, list]] = {}
        for exemplar_idx, (fresh_drafts, _fresh_calls, _fresh_errors) in zip(
            exemplar_indices, exemplar_results
        ):
            if fresh_drafts:
                exemplar_drafts_by_key[chunk_keys[exemplar_idx]] = (
                    chunk_pages[exemplar_idx],
                    fresh_drafts,
                )

        # Phase C: walk chunks in order, replaying non-exemplars and collecting
        # the fall-through chunks that still need a fresh authoring call. Results
        # are assembled below in chunk order, so a fallback before a later
        # exemplar still lands in the same position as the serial path.
        results_by_index: dict[int, tuple[list, list[LLMCall], list[ResolutionError]]] = {}
        for pos, exemplar_idx in enumerate(exemplar_indices):
            results_by_index[exemplar_idx] = exemplar_results[pos]

        replayed_draft_ids: set[str] = set()
        fallback_indices: list[int] = []

        for idx, chunk in enumerate(chunks):
            if idx in results_by_index:
                continue
            key = chunk_keys[idx]
            exemplar = exemplar_drafts_by_key.get(key)
            if exemplar is not None:
                exemplar_page, exemplar_drafts = exemplar
                page = chunk_pages[idx]
                remapped = remap_drafts_for_page(
                    exemplar_drafts,
                    exemplar_page,
                    page,
                    regions=layout.regions,
                    region_bbox=region_bbox,
                )
                if drafts_resolve_cleanly(remapped, layout):
                    remapped = apply_binding(
                        remapped,
                        elements_by_id,
                        layout,
                        self.config,
                    )
                    replayed_draft_ids.update(d.draft_id for d in remapped)
                    results_by_index[idx] = (remapped, [], [])
                    continue
                # Reuse miss: fall through to a fresh call for this tab.
            fallback_indices.append(idx)

        if fallback_indices:
            fallback_results = author_many(fallback_indices)
            for idx, result in zip(fallback_indices, fallback_results):
                results_by_index[idx] = result

        drafts: list = []
        calls: list[LLMCall] = []
        errors: list[ResolutionError] = []
        for idx in range(len(chunks)):
            fresh_drafts, fresh_calls, fresh_errors = results_by_index[idx]
            drafts.extend(fresh_drafts)
            calls.extend(fresh_calls)
            for err in fresh_errors:
                errors.append(ResolutionError(str(idx), err.tab, err.reason))
        return drafts, calls, errors, replayed_draft_ids

    def run(
        self, path: str | Path, batch_id: str | None = None, *, force: bool = False
    ) -> InstanceRecord:
        path = Path(path)
        chash = content_hash(path)
        idempotency_key = compute_cache_key(
            content_hash=chash,
            pipeline_version=PIPELINE_VERSION,
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            model=self.config.model if self.llm_client is not None else "none",
            params=self.config.params,
            include_hidden_sheets=self.config.include_hidden_sheets,
            non_answer_columns=tuple(self.config.non_answer_columns),
            checkbox_conventions=tuple(self._checkbox_conventions),
            reuse_layout_bindings=self.config.reuse_layout_bindings,
            include_address=self.config.include_address,
            min_coverage=self.config.min_coverage,
        )
        if not force:
            existing = self.store.find_instance(idempotency_key)
            if existing is not None:
                return existing

        self.store.archive_raw(path, mime=_MIME.get(path.suffix.lower(), ""))
        errors: list[str] = []
        status = InstanceStatus.COMPLETE
        elements = []
        layout = None
        tabs: list[str] = []
        hidden_sheets: list[str] = []
        page_tabs: dict[int, str] | None = None
        non_answer_ids: set[str] = set()
        source = SourceInfo(
            content_hash=chash,
            original_filename=path.name,
            mime=_MIME.get(path.suffix.lower(), ""),
            size=path.stat().st_size,
        )
        fields = []
        calls: list[LLMCall] = []

        try:
            ing = ingest(path)
        except NotImplementedError as exc:
            source.backend_reason = str(exc)
            status = InstanceStatus.FAILED
            errors.append(str(exc))
            ing = None
        except Exception as exc:  # noqa: BLE001
            source.backend_reason = f"ingest failed: {exc}"
            status = InstanceStatus.FAILED
            errors.append(f"ingest failed: {exc}")
            ing = None

        if ing is not None:
            elements = ing.elements
            source.page_count = ing.page_count
            source.sheet_names = ing.sheet_names
            source.sheet_state = ing.sheet_state
            source.backend = ing.backend.name
            source.backend_reason = ing.backend.reason
            source.has_text_layer = ing.backend.has_text_layer
            source.parser = ing.parser
            source.parser_version = ing.parser_version
            hidden_names = {
                name for name, state in ing.sheet_state.items() if state != "visible"
            }
            visible_sheets = [n for n in ing.sheet_names if n not in hidden_names]
            if ing.sheet_state and not self.config.include_hidden_sheets and hidden_names:
                elements = [e for e in elements if e.sheet not in hidden_names]
            if ing.sheet_state:
                tabs = (
                    visible_sheets
                    if not self.config.include_hidden_sheets
                    else list(ing.sheet_names)
                )
            else:
                tabs = ing.sheet_names or [
                    f"page {i}" for i in range(ing.page_count or 0)
                ]
            hidden_sheets = (
                sorted(hidden_names)
                if not self.config.include_hidden_sheets
                else []
            )
            page_tabs = page_tabs_for(ing)
            non_answer_ids = _resolve_non_answer_element_ids(
                elements, self.config.non_answer_columns
            )
            layout = analyze(elements, non_answer_element_ids=non_answer_ids)
            if self.llm_client is not None:
                chunks = project_chunks(
                    layout,
                    elements,
                    tabs,
                    non_answer_element_ids=non_answer_ids,
                    page_tabs=page_tabs,
                )
                try:
                    if self.config.reuse_layout_bindings:
                        drafts, calls, resolve_errors, replayed_draft_ids = (
                            self._author_with_reuse(
                                chunks, layout, elements, force=force
                            )
                        )
                        fields = drafts_to_fields(
                            drafts,
                            layout=layout,
                            elements_by_id={e.element_id: e for e in elements},
                            tabs=tabs,
                            non_answer_element_ids=non_answer_ids,
                            checkbox_conventions=self._checkbox_conventions,
                            replayed_draft_ids=replayed_draft_ids,
                            page_tabs=page_tabs,
                        )
                    else:
                        drafts, calls, resolve_errors = author_drafts(
                            chunks,
                            self.llm_client,
                            model=self.config.model,
                            params=self.config.params,
                            store=self.store,
                            regions=layout.regions,
                            purpose=self.config.purpose,
                            max_workers=self.config.chunk_workers,
                            include_address=self.config.include_address,
                            force=force,
                        )
                        fields = drafts_to_fields(
                            drafts,
                            layout=layout,
                            elements_by_id={e.element_id: e for e in elements},
                            tabs=tabs,
                            non_answer_element_ids=non_answer_ids,
                            checkbox_conventions=self._checkbox_conventions,
                            page_tabs=page_tabs,
                        )
                except Exception as exc:  # noqa: BLE001
                    status = InstanceStatus.PARTIAL
                    errors.append(f"resolution failed: {exc}")
                else:
                    for re_ in resolve_errors:
                        status = InstanceStatus.PARTIAL
                        errors.append(
                            f"chunk {re_.chunk_id} tab {re_.tab}: {re_.reason}"
                        )

        if (
            status is InstanceStatus.COMPLETE
            and layout is not None
            and not fields
            and layout.hypotheses
        ):
            status = InstanceStatus.PARTIAL
            errors.append("no fields resolved despite candidate hypotheses")

        coverage: list[TabCoverage] = []
        coverage_min_ratio: float | None = None
        if self.llm_client is not None and layout is not None:
            coverage = compute_coverage(
                layout,
                {e.element_id: e for e in elements},
                fields,
                page_tabs=page_tabs,
                tabs=tabs,
                non_answer_element_ids=non_answer_ids,
            )
            eligible = [c.ratio for c in coverage if c.ratio is not None]
            coverage_min_ratio = min(eligible) if eligible else None
            if self.config.min_coverage is not None:
                for block in coverage:
                    if block.ratio is not None and block.ratio < self.config.min_coverage:
                        status = InstanceStatus.PARTIAL
                        errors.append(
                            f"tab {block.tab}: row coverage {block.ratio:.2f} "
                            f"< {self.config.min_coverage:.2f}"
                        )

        consumed_blob = " ".join(
            normalize_label(t)
            for f in fields
            for t in (
                [f.label_text]
                + [o.text for o in f.options]
                + f.answers
                + f.annotations
                + ([f.value_raw] if f.value_raw else [])
            )
            if t
        )
        glyph_ids = {g.element_id for g in layout.glyphs} if layout else set()
        residuals = [
            Span(text=e.text, bbox=e.bbox, element_id=e.element_id)
            for e in elements
            if e.element_id not in glyph_ids
            and normalize_label(e.text)
            and normalize_label(e.text) not in consumed_blob
        ]

        record = InstanceRecord(
            instance_id=uuid.uuid4().hex,
            schema_version=SCHEMA_VERSION,
            pipeline_version=PIPELINE_VERSION,
            run_id=uuid.uuid4().hex,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            source=source,
            fields=fields,
            regions=layout.regions if layout else [],
            anchors=layout.anchors if layout else [],
            residuals=residuals,
            llm_calls=calls,
            match_candidates=[],
            tabs=tabs,
            hidden_sheets=hidden_sheets,
            batch_id=batch_id,
            signature=None,
            status=status,
            errors=errors,
            idempotency_key=idempotency_key,
            coverage=coverage,
            coverage_min_ratio=coverage_min_ratio,
        )
        saved, _ = self.store.save_instance(record, overwrite=force)
        return saved

