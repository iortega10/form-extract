from __future__ import annotations

import json
from pathlib import Path

from formextract.evals import load_manifest, run_manifest

MANIFEST = Path(__file__).resolve().parents[1] / "formextract" / "evals" / "manifests" / "spec_fragment.json"


def test_manifest_loads():
    manifest = load_manifest(MANIFEST)
    assert manifest.manifest_version == 1
    assert [i.item_id for i in manifest.items] == [
        "spec_fragment_pdf",
        "spec_fragment_xlsx",
        "spec_fragment_convention_xlsx",
    ]
    item = manifest.items[0]
    assert item.synthetic == "spec_fragment_pdf"
    assert len(item.golden.fields) == 4
    assert len(item.golden.regions) == 7
    assert item.cost.llm_calls == 1


def test_golden_has_exactly_one_positive_checkbox_assertion():
    manifest = load_manifest(MANIFEST)
    for item in manifest.items:
        positive = sum(len(f.selected_options) for f in item.golden.fields)
        assert positive == 1


def test_run_manifest_canned(tmp_path):
    out = tmp_path / "report.json"
    report = run_manifest(
        MANIFEST, store_root=tmp_path / "store", llm="canned", out=out
    )

    agg = report["aggregate"]
    assert agg["items_run"] == 3
    assert agg["region_type_accuracy"] == 1.0
    assert agg["anchors"]["f1"] == 1.0
    assert agg["binding"]["f1"] == 1.0
    assert agg["binding"]["tp"] == 9
    assert agg["field_id_coverage"] == 1.0
    assert agg["field_ids_unique"] is True
    assert agg["mark_selection_accuracy"] == 1.0
    assert agg["silent_selection_rate"] == 0.0
    assert agg["silent_selection_numerator"] == 0
    assert agg["silent_selection_denominator"] == 5
    assert agg["items_skipped"] == 0
    assert agg["llm_calls"] == 3
    assert agg["budgets_ok"] is True
    assert all(i["status"] == "complete" for i in report["items"])

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["aggregate"] == agg


def test_run_manifest_without_llm(tmp_path):
    report = run_manifest(MANIFEST, store_root=tmp_path / "store", llm="none")
    agg = report["aggregate"]
    assert agg["llm_calls"] == 0
    assert agg["binding"]["f1"] == 0.0
    assert agg["binding"]["fn"] == 9
    assert agg["field_id_coverage"] is None
    assert agg["field_ids_unique"] is True
    # perception is independent of the LLM
    assert agg["region_type_accuracy"] == 1.0
    assert agg["anchors"]["f1"] == 1.0
    assert all(i["status"] == "partial" for i in report["items"])


def test_golden_has_a_declared_convention_positive(tmp_path):
    from formextract.evals.canned import client_for
    from formextract.pipeline import Pipeline, PipelineConfig
    from formextract.store import Store

    manifest = load_manifest(MANIFEST)
    item = next(
        i for i in manifest.items if i.item_id == "spec_fragment_convention_xlsx"
    )
    path = item.resolve_path(manifest.base_dir, tmp_path / "fixtures")
    client = client_for(item.item_id)
    pipeline = Pipeline(
        Store(tmp_path / "store"),
        client,
        PipelineConfig(model="canned", checkbox_conventions=item.checkbox_conventions),
    )
    record = pipeline.run(path)

    field = next(f for f in record.fields if f.label_text == "Priority")
    assert [o.selected for o in field.options] == [True, None]
    assert field.value_normalized == "Low"
    assert field.ambiguity is None
    assert field.provenance.review_flag is False
