from __future__ import annotations

from pathlib import Path

from formextract.evals.canned import client_for
from formextract.model import InstanceStatus
from formextract.pipeline import Pipeline, PipelineConfig
from formextract.store import Store


def test_pipeline_end_to_end_canned(spec_pdf, store):
    pipe = Pipeline(store, client_for("spec_fragment_pdf"), PipelineConfig())
    record = pipe.run(spec_pdf)

    assert record.status is InstanceStatus.COMPLETE
    assert record.errors == []
    assert len(record.fields) == 4
    assert len(record.llm_calls) == 1

    by_canon = {f.canonical_name: f for f in record.fields}
    assert by_canon["us_states_shipped"].value_normalized == "ALL"
    assert by_canon["ships_to_canada"].value_normalized is None
    assert by_canon["vendor_status"].value_normalized is None
    assert by_canon["order_log_supported"].value_normalized == "true"

    ships = by_canon["ships_to_canada"]
    assert ships.provenance.review_flag is True
    assert ships.provenance.review_reason.value == "ambiguous_mark"
    assert ships.ambiguity is not None
    assert ships.ambiguity.reason == "between_options"
    assert all(o.selected is None for o in ships.options)

    order_log = by_canon["order_log_supported"]
    assert order_log.provenance.review_flag is False
    assert order_log.provenance.heuristic_agreement == ["marker_auto_select"]
    assert [o.selected for o in order_log.options] == [True]

    assert record.regions and record.anchors
    assert len(record.anchors) == 5

    # title and grid tokens are unconsumed text; glyph marks are excluded
    residual_texts = [s.text for s in record.residuals]
    assert "COMPLIANCE" in residual_texts
    assert "AK" in residual_texts
    assert all(t != "X" for t in residual_texts)

    assert store.find_instance(record.idempotency_key).instance_id == record.instance_id
    assert (store.instances_dir / f"{record.instance_id}.json").exists()


def test_pipeline_idempotent(spec_pdf, store):
    pipe = Pipeline(store, client_for("spec_fragment_pdf"), PipelineConfig())
    first = pipe.run(spec_pdf)
    second = pipe.run(spec_pdf)
    assert first.instance_id == second.instance_id


def test_pipeline_without_llm_is_partial_with_residuals(spec_xlsx):
    store = Store(spec_xlsx.parent / "store")
    pipe = Pipeline(store, None, PipelineConfig())
    record = pipe.run(spec_xlsx)

    assert record.fields == []
    assert record.status is InstanceStatus.PARTIAL
    assert record.residuals
    assert "no fields resolved despite candidate hypotheses" in record.errors
    assert record.anchors


def test_pipeline_unsupported_source_fails(tmp_path, store):
    bad = tmp_path / "form.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    record = Pipeline(store, None).run(bad)
    assert record.status is InstanceStatus.FAILED
    assert record.errors
    assert "unsupported document type" in record.errors[0]


def test_pipeline_batches_can_be_tagged(spec_pdf, store):
    record = Pipeline(store, None).run(spec_pdf, batch_id="batch-1")
    assert record.batch_id == "batch-1"


def test_pipeline_deterministic_across_stores(spec_xlsx, tmp_path):
    first = Pipeline(
        Store(tmp_path / "s1"), client_for("spec_fragment_xlsx"), PipelineConfig()
    ).run(spec_xlsx)
    second = Pipeline(
        Store(tmp_path / "s2"), client_for("spec_fragment_xlsx"), PipelineConfig()
    ).run(spec_xlsx)

    assert [f.field_id for f in first.fields] == [f.field_id for f in second.fields]
    assert [f.value_normalized for f in first.fields] == [
        f.value_normalized for f in second.fields
    ]
    assert [f.source_elements for f in first.fields] == [
        f.source_elements for f in second.fields
    ]
    assert [f.section_path for f in first.fields] == [
        f.section_path for f in second.fields
    ]


def test_pipeline_two_runs_identical_modulo_volatile_ids(spec_xlsx, tmp_path):
    def run():
        import uuid

        return Pipeline(
            Store(tmp_path / uuid.uuid4().hex),
            client_for("spec_fragment_xlsx"),
            PipelineConfig(),
        ).run(spec_xlsx)

    first, second = run(), run()
    assert first.instance_id != second.instance_id
    assert first.run_id != second.run_id
    assert [f.field_id for f in first.fields] == [f.field_id for f in second.fields]
    assert [f.value_normalized for f in first.fields] == [
        f.value_normalized for f in second.fields
    ]
    assert [f.provenance.review_flag for f in first.fields] == [
        f.provenance.review_flag for f in second.fields
    ]
    assert [f.ambiguity for f in first.fields] == [f.ambiguity for f in second.fields]
    assert [[o.selected for o in f.options] for f in first.fields] == [
        [o.selected for o in f.options] for f in second.fields
    ]
