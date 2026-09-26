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
    ]
    item = manifest.items[0]
    assert item.synthetic == "spec_fragment_pdf"
    assert len(item.golden.fields) == 4
    assert len(item.golden.regions) == 7
    assert item.cost.llm_calls == 1


def test_run_manifest_canned(tmp_path):
    out = tmp_path / "report.json"
    report = run_manifest(
        MANIFEST, store_root=tmp_path / "store", llm="canned", out=out
    )

    agg = report["aggregate"]
    assert agg["items_run"] == 2
    assert agg["region_type_accuracy"] == 1.0
    assert agg["anchors"]["f1"] == 1.0
    assert agg["binding"]["f1"] == 1.0
    assert agg["binding"]["tp"] == 8
    assert agg["llm_calls"] == 2
    assert agg["budgets_ok"] is True
    assert all(i["status"] == "complete" for i in report["items"])

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["aggregate"] == agg


def test_run_manifest_without_llm(tmp_path):
    report = run_manifest(MANIFEST, store_root=tmp_path / "store", llm="none")
    agg = report["aggregate"]
    assert agg["llm_calls"] == 0
    assert agg["binding"]["f1"] == 0.0
    assert agg["binding"]["fn"] == 8
    # perception is independent of the LLM
    assert agg["region_type_accuracy"] == 1.0
    assert agg["anchors"]["f1"] == 1.0
    assert all(i["status"] == "partial" for i in report["items"])
