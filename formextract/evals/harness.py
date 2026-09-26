"""Golden-set harness: run the pipeline over manifest items and score results.

Two levels, per the spec:
- perception: region detection/typing, anchor inventory — scored WITHOUT the LLM;
- binding: canonical field grouping and normalized values — scored against the
  resolved instance, plus a cost-budget check.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..ingest import ingest
from ..model import BBox, InstanceRecord, Region
from ..pipeline import Pipeline, PipelineConfig
from ..schema import normalize_label
from ..store import Store
from . import canned
from .manifest import Golden, GoldenItem, Manifest, load_manifest
from .metrics import PRF, accuracy, aggregate_prf, prf, score_sets


@dataclass
class ItemReport:
    item_id: str
    instance_id: str | None = None
    status: str = ""
    region_type_accuracy: float | None = None
    region_prf: dict[str, Any] = field(default_factory=dict)
    anchor_prf: dict[str, Any] = field(default_factory=dict)
    binding_prf: dict[str, Any] = field(default_factory=dict)
    control_accuracy: float | None = None
    llm_calls: int = 0
    tokens: int = 0
    budget_ok: bool = True
    errors: list[str] = field(default_factory=list)


def _center_in(inner: BBox, outer: Region) -> bool:
    cx = (inner.x0 + inner.x1) / 2
    cy = (inner.y0 + inner.y1) / 2
    b = outer.bbox
    return b.x0 <= cx <= b.x1 and b.y0 <= cy <= b.y1


def _region_blobs(regions: list[Region], elements) -> list[tuple[Region, str]]:
    out = []
    for r in regions:
        texts = sorted(
            (e for e in elements if _center_in(e.bbox, r)),
            key=lambda e: (e.bbox.y0, e.bbox.x0),
        )
        out.append((r, " ".join(normalize_label(e.text) for e in texts)))
    return out


def _score_regions(golden: Golden, regions: list[Region], elements) -> tuple[float, PRF]:
    preds = _region_blobs(regions, elements)
    if not golden.regions:
        return None, prf(0, len(preds), 0)
    correct = 0
    matched_pred: set[int] = set()
    fn = 0
    for g in golden.regions:
        hint = normalize_label(g.label_hint)
        found = [i for i, (_, blob) in enumerate(preds) if hint and hint in blob]
        if not found:
            fn += 1
            continue
        matched_pred.update(found)
        if any(preds[i][0].type == g.type for i in found):
            correct += 1
    region_prf = prf(len(matched_pred), len(preds) - len(matched_pred), fn)
    return accuracy(correct, len(golden.regions)), region_prf


def _field_key(label: str, canonical_name: str | None) -> str:
    from ..schema import canonical_name as canon

    return canonical_name or canon(label) or normalize_label(label)


def _annotation_ok(golden_ann: list[str], predicted_ann: list[str]) -> bool:
    blob = " ".join(normalize_label(a) for a in predicted_ann)
    return all(normalize_label(g) in blob for g in golden_ann)


def _score_fields(golden: Golden, record: InstanceRecord) -> tuple[PRF, float | None]:
    pred_map = {
        _field_key(f.label_text, f.canonical_name): f for f in record.fields
    }
    gold_map = {
        _field_key(g.label, g.canonical_name): g for g in golden.fields
    }
    tp = 0
    correct = 0
    matched = 0
    for key, g in gold_map.items():
        f = pred_map.get(key)
        if f is None:
            continue
        matched += 1
        control_ok = f.control_type == g.control_type
        value_ok = (f.value_normalized or f.value_raw) == g.value
        ann_ok = _annotation_ok(g.annotations, f.annotations)
        if control_ok and value_ok and ann_ok:
            correct += 1
            tp += 1
    fp = len(pred_map) - tp
    fn = len(gold_map) - correct
    return prf(tp, fp, fn), accuracy(correct, matched)


def score_item(item: GoldenItem, record: InstanceRecord, elements) -> ItemReport:
    report = ItemReport(
        item_id=item.item_id,
        instance_id=record.instance_id,
        status=record.status.value if hasattr(record.status, "value") else str(record.status),
        errors=list(record.errors),
    )
    report.region_type_accuracy, region_score = _score_regions(
        item.golden, record.regions, elements
    )
    report.region_prf = asdict(region_score)
    pred_anchors = {(a.normalized_text, a.occurrence_ordinal) for a in record.anchors}
    gold_anchors = {
        (a.normalized_text, a.occurrence_ordinal) for a in item.golden.anchors
    }
    anchor_score = score_sets(pred_anchors, gold_anchors)
    report.anchor_prf = asdict(anchor_score)

    binding, control_acc = _score_fields(item.golden, record)
    report.binding_prf = asdict(binding)
    report.control_accuracy = control_acc

    report.llm_calls = len(record.llm_calls)
    report.tokens = sum(c.tokens or 0 for c in record.llm_calls)
    if item.cost is not None:
        if item.cost.llm_calls is not None:
            report.budget_ok &= report.llm_calls <= item.cost.llm_calls
        if item.cost.tokens is not None:
            report.budget_ok &= report.tokens <= item.cost.tokens
    return report


def run_manifest(
    manifest: Manifest | str | Path,
    *,
    store_root: str | Path,
    llm: str = "canned",
    out: str | Path | None = None,
    model: str = "gpt-4o-mini",
) -> dict[str, Any]:
    if not isinstance(manifest, Manifest):
        manifest = load_manifest(manifest)
    store_root = Path(store_root)
    store = Store(store_root)
    fixture_dir = store_root / "fixtures"
    reports: list[ItemReport] = []

    for item in manifest.items:
        path = item.resolve_path(manifest.base_dir, fixture_dir)
        client = canned.client_for(item.item_id) if llm == "canned" else None
        pipeline = Pipeline(store, client, PipelineConfig(model=model))
        record = pipeline.run(path)
        elements = ingest(path).elements
        reports.append(score_item(item, record, elements))

    binding_scores = [PRF(**r.binding_prf) for r in reports if r.binding_prf]
    anchor_scores = [PRF(**r.anchor_prf) for r in reports if r.anchor_prf]
    region_scores = [PRF(**r.region_prf) for r in reports if r.region_prf]
    type_accs = [r.region_type_accuracy for r in reports if r.region_type_accuracy is not None]

    report: dict[str, Any] = {
        "manifest_version": manifest.manifest_version,
        "llm": llm,
        "items": [asdict(r) for r in reports],
        "aggregate": {
            "region_type_accuracy": accuracy(round(sum(type_accs), 6), len(type_accs))
            if type_accs
            else None,
            "regions": asdict(aggregate_prf(region_scores)) if region_scores else None,
            "anchors": asdict(aggregate_prf(anchor_scores)) if anchor_scores else None,
            "binding": asdict(aggregate_prf(binding_scores)) if binding_scores else None,
            "llm_calls": sum(r.llm_calls for r in reports),
            "tokens": sum(r.tokens for r in reports),
            "budgets_ok": all(r.budget_ok for r in reports),
            "items_run": len(reports),
        },
    }
    if out is not None:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the formextract golden-set eval")
    parser.add_argument("manifest", help="path to manifest.json")
    parser.add_argument("--store", default=".formextract-store", help="store root")
    parser.add_argument("--out", default=None, help="write JSON report here")
    parser.add_argument("--llm", choices=["canned", "none"], default="canned")
    parser.add_argument("--model", default="gpt-4o-mini")
    args = parser.parse_args(argv)
    report = run_manifest(
        args.manifest, store_root=args.store, llm=args.llm, out=args.out, model=args.model
    )
    print(json.dumps(report["aggregate"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
