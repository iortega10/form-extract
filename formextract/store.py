"""JSON-canonical persistence: raw archive, instance records, template registry,
batches, content-addressed LLM I/O. DuckDB deferred (Phase 4)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from .model import (
    BatchRecord,
    InstanceRecord,
    InstanceStatus,
    LLMCall,
    RawArchiveRecord,
    TemplateRecord,
    from_json,
    to_json,
)


def content_hash(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(data) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def _atomic_write_text(path: Path, text: str, *, idempotent: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if idempotent and path.exists():
        return
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        if path.exists():
            # Another thread already wrote a complete destination (identical
            # content-addressed chunks can target the same key); keep the
            # winner rather than raising on Windows' transient lock.
            try:
                tmp.unlink()
            except OSError:
                pass
            return
        raise


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.raw_dir = self.root / "raw"
        self.instances_dir = self.root / "instances"
        self.templates_dir = self.root / "templates"
        self.batches_dir = self.root / "batches"
        self.runs_dir = self.root / "runs"
        self.llm_dir = self.root / "llm"
        for d in (self.raw_dir, self.instances_dir, self.templates_dir, self.batches_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        (self.llm_dir / "prompts").mkdir(parents=True, exist_ok=True)
        (self.llm_dir / "responses").mkdir(parents=True, exist_ok=True)
        (self.llm_dir / "calls").mkdir(parents=True, exist_ok=True)

    @property
    def _instance_index(self) -> Path:
        return self.instances_dir / "index.json"

    def archive_raw(
        self, path: str | Path, *, mime: str = "", source_system: str = "local"
    ) -> RawArchiveRecord:
        path = Path(path)
        chash = content_hash(path)
        index_path = self.raw_dir / "index.json"
        index = _read_json(index_path, {})
        if chash in index:
            return RawArchiveRecord(**index[chash])
        dest = self.raw_dir / f"{chash}{path.suffix.lower()}"
        if not dest.exists():
            shutil.copy2(path, dest)
        record = RawArchiveRecord(
            content_hash=chash,
            size=path.stat().st_size,
            mime=mime or path.suffix.lower().lstrip("."),
            original_filename=path.name,
            ingested_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            source_system=source_system,
            storage_ref=str(dest.relative_to(self.root)),
        )
        index[chash] = record.__dict__
        _write_json(index_path, index)
        return record

    def find_instance(self, idempotency_key: str) -> InstanceRecord | None:
        index = _read_json(self._instance_index, {})
        instance_id = index.get(idempotency_key)
        if not instance_id:
            return None
        record = self.load_instance(instance_id)
        if record is None or record.status is not InstanceStatus.COMPLETE or record.errors:
            return None
        return record

    def record_run(self, record: InstanceRecord) -> InstanceRecord:
        if not record.instance_id:
            record.instance_id = uuid.uuid4().hex
        if not record.run_id:
            record.run_id = uuid.uuid4().hex
        path = self.runs_dir / record.source.content_hash / f"{record.run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(to_json(record), encoding="utf-8")
        return record

    def save_instance(
        self, record: InstanceRecord, *, overwrite: bool = False
    ) -> tuple[InstanceRecord, bool]:
        if record.status is not InstanceStatus.COMPLETE:
            return self.record_run(record), True
        index = _read_json(self._instance_index, {})
        key = record.idempotency_key
        if key in index and not overwrite:
            existing = self.load_instance(index[key])
            if existing is not None:
                return existing, False
        if not record.instance_id:
            record.instance_id = uuid.uuid4().hex
        path = self.instances_dir / f"{record.instance_id}.json"
        path.write_text(to_json(record), encoding="utf-8")
        index[key] = record.instance_id
        _write_json(self._instance_index, index)
        return record, True

    def load_instance(self, instance_id: str) -> InstanceRecord | None:
        path = self.instances_dir / f"{instance_id}.json"
        if not path.exists():
            return None
        return from_json(InstanceRecord, path.read_text(encoding="utf-8"))

    def save_template(self, record: TemplateRecord) -> None:
        path = self.templates_dir / f"{record.template_id}.json"
        path.write_text(to_json(record), encoding="utf-8")

    def load_template(self, template_id: str) -> TemplateRecord | None:
        path = self.templates_dir / f"{template_id}.json"
        if not path.exists():
            return None
        return from_json(TemplateRecord, path.read_text(encoding="utf-8"))

    def list_templates(self) -> list[str]:
        return sorted(p.stem for p in self.templates_dir.glob("*.json"))

    def save_batch(self, record: BatchRecord) -> None:
        path = self.batches_dir / f"{record.batch_id}.json"
        path.write_text(to_json(record), encoding="utf-8")

    def load_batch(self, batch_id: str) -> BatchRecord | None:
        path = self.batches_dir / f"{batch_id}.json"
        if not path.exists():
            return None
        return from_json(BatchRecord, path.read_text(encoding="utf-8"))

    def _llm_call_key(
        self, *, purpose: str, model: str, params: dict, prompt_hash: str
    ) -> str:
        return _sha256_text(
            "|".join([prompt_hash, model, canonical_json(params), purpose])
        )

    def find_llm_call(
        self, *, purpose: str, model: str, params: dict, prompt: str
    ) -> LLMCall | None:
        key = self._llm_call_key(
            purpose=purpose,
            model=model,
            params=params,
            prompt_hash=_sha256_text(prompt),
        )
        path = self.llm_dir / "calls" / f"{key}.json"
        if not path.exists():
            return None
        return from_json(LLMCall, path.read_text(encoding="utf-8"))

    def archive_llm_call(
        self,
        *,
        purpose: str,
        model: str,
        params: dict,
        prompt: str,
        response: str,
        tokens: int | None = None,
        latency_ms: int | None = None,
        index: bool = True,
    ) -> LLMCall:
        prompt_hash = _sha256_text(prompt)
        response_hash = _sha256_text(response)
        prompt_path = self.llm_dir / "prompts" / f"{prompt_hash}.txt"
        response_path = self.llm_dir / "responses" / f"{response_hash}.txt"
        _atomic_write_text(prompt_path, prompt, idempotent=True)
        _atomic_write_text(response_path, response, idempotent=True)
        call = LLMCall(
            call_id=uuid.uuid4().hex,
            purpose=purpose,
            model=model,
            params=dict(params),
            prompt_hash=prompt_hash,
            prompt_ref=str(prompt_path.relative_to(self.root)),
            response_ref=str(response_path.relative_to(self.root)),
            tokens=tokens,
            latency_ms=latency_ms,
        )
        if index:
            key = self._llm_call_key(
                purpose=purpose,
                model=model,
                params=params,
                prompt_hash=prompt_hash,
            )
            call_path = self.llm_dir / "calls" / f"{key}.json"
            _atomic_write_text(call_path, to_json(call))
        return call
