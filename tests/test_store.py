from __future__ import annotations

import hashlib

from formextract.model import (
    BatchRecord,
    InstanceRecord,
    PIPELINE_VERSION,
    SourceInfo,
    TemplateRecord,
)
from formextract.store import Store, content_hash


def _record(store_root_hash: str) -> InstanceRecord:
    return InstanceRecord(
        instance_id="inst1",
        schema_version="1",
        pipeline_version=PIPELINE_VERSION,
        run_id="run1",
        created_at="2024-01-01T00:00:00Z",
        source=SourceInfo(
            content_hash=store_root_hash, original_filename="a.pdf", mime="pdf", size=1
        ),
        idempotency_key=f"{store_root_hash}:{PIPELINE_VERSION}",
    )


def test_content_hash_matches_sha256(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello form")
    assert content_hash(p) == hashlib.sha256(b"hello form").hexdigest()


def test_archive_raw_idempotent(store, spec_pdf):
    r1 = store.archive_raw(spec_pdf, mime="pdf")
    r2 = store.archive_raw(spec_pdf, mime="pdf")
    assert r1.content_hash == r2.content_hash
    assert (store.root / r1.storage_ref).exists()
    import json

    index = json.loads((store.raw_dir / "index.json").read_text(encoding="utf-8"))
    assert len(index) == 1


def test_save_instance_no_overwrite_on_collision(store):
    rec = _record("hash1")
    saved, created = store.save_instance(rec)
    assert created is True
    assert store.find_instance(f"hash1:{PIPELINE_VERSION}").instance_id == "inst1"

    rec2 = _record("hash1")
    rec2.instance_id = "inst2"
    saved2, created2 = store.save_instance(rec2)
    assert created2 is False
    assert saved2.instance_id == "inst1"
    assert not (store.instances_dir / "inst2.json").exists()


def test_archive_llm_call_content_addressed(store):
    call1 = store.archive_llm_call(
        purpose="cold_binding",
        model="m",
        params={"temperature": 0},
        prompt="PROMPT",
        response="RESPONSE",
        tokens=10,
        latency_ms=5,
    )
    call2 = store.archive_llm_call(
        purpose="cold_binding",
        model="m",
        params={"temperature": 0},
        prompt="PROMPT",
        response="RESPONSE",
    )
    assert call1.prompt_hash == call2.prompt_hash
    assert call1.response_ref == call2.response_ref
    assert (store.root / call1.prompt_ref).read_text(encoding="utf-8") == "PROMPT"
    assert (store.root / call1.response_ref).read_text(encoding="utf-8") == "RESPONSE"
    assert call1.tokens == 10


def test_template_and_batch_roundtrip(store):
    from formextract.model import TemplateStatus

    tpl = TemplateRecord(
        template_id="t1",
        family_id="f1",
        version=1,
        status=TemplateStatus.DRAFT,
        backends=["pdf_text"],
        schema_version="1",
        perception_version="1",
    )
    store.save_template(tpl)
    assert store.list_templates() == ["t1"]
    loaded = store.load_template("t1")
    assert loaded.template_id == "t1"

    batch = BatchRecord(
        batch_id="b1",
        members=["i1", "i2"],
        formed_at="2024-01-01T00:00:00Z",
    )
    store.save_batch(batch)
    assert store.load_batch("b1").members == ["i1", "i2"]
