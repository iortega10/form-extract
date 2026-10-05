"""Orchestration: ingest → perception → (cold-path authoring) → instance record."""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ingest import ingest
from .layout import analyze
from .model import (
    PIPELINE_VERSION,
    InstanceRecord,
    InstanceStatus,
    LLMCall,
    SourceInfo,
    Span,
)
from .resolve import (
    LLMClient,
    PROMPT_VERSION,
    author_drafts,
    drafts_to_fields,
    project_chunks,
)
from .schema import SCHEMA_VERSION, normalize_label
from .store import Store, canonical_json, content_hash

_MIME = {".xlsx": "xlsx", ".pdf": "pdf"}


def compute_cache_key(
    *,
    content_hash: str,
    pipeline_version: str,
    schema_version: str,
    prompt_version: str,
    model: str,
    params: dict[str, Any],
) -> str:
    params_hash = hashlib.sha256(
        canonical_json(params).encode("utf-8")
    ).hexdigest()
    parts = "|".join(
        [
            content_hash,
            pipeline_version,
            schema_version,
            prompt_version,
            model or "none",
            params_hash,
        ]
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


@dataclass
class PipelineConfig:
    model: str = "gpt-4o-mini"
    params: dict[str, Any] = field(default_factory=lambda: {"temperature": 0})
    purpose: str = "cold_binding"


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
            source.backend = ing.backend.name
            source.backend_reason = ing.backend.reason
            source.has_text_layer = ing.backend.has_text_layer
            source.parser = ing.parser
            source.parser_version = ing.parser_version
            tabs = ing.sheet_names or [f"page {i}" for i in range(ing.page_count or 0)]
            layout = analyze(elements)
            if self.llm_client is not None:
                chunks = project_chunks(layout, elements, tabs)
                try:
                    drafts, calls, resolve_errors = author_drafts(
                        chunks,
                        self.llm_client,
                        model=self.config.model,
                        params=self.config.params,
                        store=self.store,
                        regions=layout.regions,
                        purpose=self.config.purpose,
                    )
                    fields = drafts_to_fields(drafts)
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
            batch_id=batch_id,
            signature=None,
            status=status,
            errors=errors,
            idempotency_key=idempotency_key,
        )
        saved, _ = self.store.save_instance(record, overwrite=force)
        return saved

