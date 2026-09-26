"""Golden-set manifest: expected perception, bindings, and cost per item."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GoldenRegion:
    label_hint: str
    type: str


@dataclass
class GoldenAnchor:
    normalized_text: str
    occurrence_ordinal: int = 0


@dataclass
class GoldenField:
    label: str
    control_type: str
    canonical_name: str | None = None
    value: str | None = None
    annotations: list[str] = field(default_factory=list)


@dataclass
class CostBudget:
    llm_calls: int | None = None
    tokens: int | None = None


@dataclass
class Golden:
    regions: list[GoldenRegion] = field(default_factory=list)
    anchors: list[GoldenAnchor] = field(default_factory=list)
    fields: list[GoldenField] = field(default_factory=list)


@dataclass
class GoldenItem:
    item_id: str
    golden: Golden
    path: str | None = None
    synthetic: str | None = None
    cost: CostBudget | None = None

    def resolve_path(self, base_dir: Path, fixture_dir: Path) -> Path:
        if self.synthetic:
            from .synthetic import build

            return build(self.synthetic, fixture_dir)
        if self.path is None:
            raise ValueError(f"item {self.item_id} has neither path nor synthetic")
        p = Path(self.path)
        return p if p.is_absolute() else base_dir / p


@dataclass
class Manifest:
    manifest_version: int
    items: list[GoldenItem]
    source_path: Path | None = None

    @property
    def base_dir(self) -> Path:
        return self.source_path.parent if self.source_path else Path.cwd()


def _golden(data: dict) -> Golden:
    return Golden(
        regions=[GoldenRegion(**r) for r in data.get("regions", [])],
        anchors=[GoldenAnchor(**a) for a in data.get("anchors", [])],
        fields=[GoldenField(**f) for f in data.get("fields", [])],
    )


def load_manifest(path: str | Path) -> Manifest:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    items = []
    for item in data.get("items", []):
        items.append(
            GoldenItem(
                item_id=item["item_id"],
                path=item.get("path"),
                synthetic=item.get("synthetic"),
                golden=_golden(item.get("golden", {})),
                cost=CostBudget(**item["cost"]) if item.get("cost") else None,
            )
        )
    return Manifest(
        manifest_version=int(data.get("manifest_version", 1)),
        items=items,
        source_path=path,
    )
