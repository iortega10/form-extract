"""Shared data contracts: the boundary deterministic replay and resolution agree on."""
from __future__ import annotations

import json
import types
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

# 0.3.1 moved this to "2": the marker classifier no longer counts a field's
# own label as an option candidate, so the same file produces different field
# values than 0.3.0 and cached 0.3.0 records must not be served.
# 0.4.0-C2 leaves this at "2": layout-signature reuse is opt-in
# (PipelineConfig.reuse_layout_bindings, default False) and the cache key
# carries the flag, so default-config output is unchanged from 0.3.1.
# 0.5.0 moves this to "3": the default prompt no longer asks for `address`
# (PipelineConfig.include_address defaults False), so default output changed
# and cached 0.4.0 records must not be served.
# 0.6.0-L1b moves this to "4": the lines output contract ships beside the JSON
# one (PipelineConfig.output_contract, default "json"), so every instance key
# moves once. The default json prompt and projection stay byte-identical to
# 0.5.0 and the call cache keys on the prompt text, so cached 0.5.x calls are
# still served even though the instance key misses.
# 0.6.0-K1 moves this to "5": the resolver re-derives a one-option
# `single_select` as `text`/`bool` by default (PipelineConfig.kind_rule), so a
# default-config run's fields change and cached 0.6.0-L1b instances must not be
# served. The call cache keys on prompt text, which does not change, so cached
# calls are still served.
# 0.6.1 moves this to "6": a resolver mark decision now also makes a field's
# `answers` agree with its `options[].selected` (A), and a lines label built from
# a mark-only cell is repaired from the row's sole text cell and review-flagged
# rather than emitted silently empty (B). Both change default output, so cached
# 0.6.0 instances must not be served. The call cache keys on prompt text, which
# does not change, so cached calls are still served.
PIPELINE_VERSION = "6"

#: 0.6.0-K1: the id of the `one_option_single` kind rule, recorded on a
#: `Provenance` the rule re-derived and used as the cache-key token when the
#: knob is off. "2": a literal ASCII checkbox spelling in the draft's row is a marker.
KIND_RULE_VERSION = "2"


class RegionType(str, Enum):
    GRID = "grid"
    FIELD_ROW = "field-row"
    HEADER = "header"
    PROSE = "prose"


class ControlType(str, Enum):
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    BOOL = "bool"
    TEXT = "text"


class ProvenanceSource(str, Enum):
    XLSX = "xlsx"
    PDF_TEXT = "pdf_text"
    LLM = "llm"
    REPLAY = "replay"
    HEURISTIC = "heuristic"


class AuthoredBy(str, Enum):
    LLM = "llm"
    HEURISTIC = "heuristic"
    HUMAN = "human"


class ReviewReason(str, Enum):
    LOW_MATCH = "low_match"
    REPLAY_MISMATCH = "replay_mismatch"
    AMBIGUOUS_ROLE = "ambiguous_role"
    AMBIGUOUS_MARK = "ambiguous_mark"
    NOVEL_FIELD = "novel_field"
    UNRESOLVED_REFERENCE = "unresolved_reference"


# Closed reason set for FieldAmbiguity.reason (a marker's geometric class,
# distinct from ReviewReason on provenance). Stored as strings in the record
# rather than new ReviewReason members, so SCHEMA_VERSION does not move.
AMBIGUITY_BETWEEN_OPTIONS = "between_options"
AMBIGUITY_COMPETING_OPTIONS = "competing_options"
AMBIGUITY_CONVENTION_UNSUPPORTED = "convention_unsupported_by_geometry"

CHECKBOX_MARK_PRECEDES_OPTION = "mark_precedes_option"
CHECKBOX_MARK_FOLLOWS_OPTION = "mark_follows_option"


class GlyphKind(str, Enum):
    CHECK = "check"
    CROSS = "cross"
    BOX = "box"
    NONE = "none"


class MarkerClass(str, Enum):
    RIGHT_ONLY = "right_only"
    BETWEEN = "between"
    LEFT_ONLY = "left_only"
    UNATTACHED = "unattached"


class InstanceStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class TemplateStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    MERGED = "merged"


@dataclass
class BBox:
    page: int = 0
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2

    def union(self, other: "BBox") -> "BBox":
        return BBox(
            page=self.page,
            x0=min(self.x0, other.x0),
            y0=min(self.y0, other.y0),
            x1=max(self.x1, other.x1),
            y1=max(self.y1, other.y1),
        )

    def overlaps_vertically(self, other: "BBox", tolerance: float = 0.0) -> bool:
        return self.y0 <= other.y1 + tolerance and other.y0 <= self.y1 + tolerance


@dataclass
class Element:
    element_id: str
    text: str
    bbox: BBox
    font: str | None = None
    size: float | None = None
    color: int | None = None
    fill: int | None = None
    sheet: str | None = None
    merged: bool = False


@dataclass
class Region:
    region_id: str
    type: RegionType
    bbox: BBox
    column: int
    band_ids: list[int] = field(default_factory=list)
    element_ids: list[str] = field(default_factory=list)


@dataclass
class Glyph:
    element_id: str
    kind: GlyphKind
    bbox: BBox


@dataclass
class Option:
    text: str
    selected: bool | None = None
    raw_span: str | None = None
    bbox: BBox | None = None


@dataclass
class ElementRef:
    """A model-authored pointer into one band/segment of a projection chunk."""

    region_id: str
    band_id: int
    segment_index: int


@dataclass
class MarkerClassification:
    """Geometric class of one glyph marker plus its option candidates.

    Derived during layout analysis; consumed by resolution, never stored in an
    instance record.
    """

    marker_element_id: str
    marker_class: MarkerClass
    left_candidate_element_ids: list[str] = field(default_factory=list)
    right_candidate_element_ids: list[str] = field(default_factory=list)

    @property
    def candidate_element_ids(self) -> list[str]:
        return [*self.left_candidate_element_ids, *self.right_candidate_element_ids]


@dataclass(frozen=True)
class CheckboxConvention:
    """A caller-declared convention for markers that sit between two options.

    The selector is the tab (exact name or ``fnmatch`` glob) plus an optional
    regex matched against the normalised anchor/label text of the control the
    marker belongs to. ``None`` matches every control on the tab.
    """

    tab: str
    anchor_pattern: str | None = None
    convention: str = CHECKBOX_MARK_PRECEDES_OPTION

    def __post_init__(self) -> None:
        if self.convention not in (
            CHECKBOX_MARK_PRECEDES_OPTION,
            CHECKBOX_MARK_FOLLOWS_OPTION,
        ):
            raise ValueError(
                f"unknown checkbox convention {self.convention!r}; expected "
                f"{CHECKBOX_MARK_PRECEDES_OPTION!r} or "
                f"{CHECKBOX_MARK_FOLLOWS_OPTION!r}"
            )


@dataclass
class FieldAmbiguity:
    """Why a field's value could not be resolved unambiguously.

    `reason` is one of the AMBIGUITY_* string constants (between_options /
    competing_options), widened from ReviewReason in B2 without a schema bump.
    """

    marker_element_id: str | None = None
    candidate_element_ids: list[str] = field(default_factory=list)
    reason: str | None = None


@dataclass
class Provenance:
    source: ProvenanceSource | None = None
    binding_id: str | None = None
    binding_version: int | None = None
    match_score: float | None = None
    verified: bool | None = None
    heuristic_agreement: list[str] = field(default_factory=list)
    llm_confidence: float | None = None
    review_flag: bool = False
    review_reason: ReviewReason | None = None
    derived_confidence: float | None = None
    #: 0.6.0-K1: the model's stated control type, kept only when the resolver's
    #: ``one_option_single`` kind rule re-derived the field. Emitted only while
    #: set (``omit_if_default``), so an unaffected record's bytes are unchanged.
    stated_control_type: str | None = field(
        default=None, metadata={"omit_if_default": True}
    )
    #: 0.6.0-K1: the id of the rule that re-derived the field
    #: (``one_option_single``); emitted only while set.
    kind_rule: str | None = field(default=None, metadata={"omit_if_default": True})
    #: 0.6.1-A: the model's own ``answers`` for a field whose mark decision the
    #: resolver overrode. Emitted only while set (``omit_if_default``), so an
    #: unaffected record's bytes are unchanged.
    model_answers: list[str] | None = field(
        default=None, metadata={"omit_if_default": True}
    )


@dataclass
class Field:
    label_text: str
    control_type: ControlType
    bbox: BBox
    options: list[Option] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    region_ref: str | None = None
    select_all_ref: str | None = None
    canonical_name: str | None = None
    value_raw: str | None = None
    value_normalized: str | None = None
    normalizer_id: str | None = None
    edited_by_human: bool = False
    human_value: str | None = None
    provenance: Provenance = field(default_factory=Provenance)
    tab: str | None = None
    source_elements: list[str] = field(default_factory=list)
    field_id: str | None = None
    section_path: list[str] = field(default_factory=list)
    ambiguity: FieldAmbiguity | None = None
    unresolved_source_refs: list[str] = field(default_factory=list)


@dataclass
class AnchorLocation:
    column: int
    row_band: int
    ordinal_in_band: int


@dataclass
class RelationalAddress:
    anchor_id: str
    column: int | None = None
    row_band: int | None = None
    offset_right: int = 0


@dataclass
class Anchor:
    anchor_id: str
    normalized_text: str
    occurrence_ordinal: int
    region_id: str
    relative_address: AnchorLocation


@dataclass
class BindingProvenance:
    authored_by: AuthoredBy
    authored_at: str
    exemplar_instance_id: str | None = None
    llm_call_ref: str | None = None
    approval_state: str = "draft"


@dataclass
class Binding:
    binding_id: str
    field_id: str
    canonical_name: str | None
    control_type: ControlType
    options: list[str]
    anchor_relational_address: RelationalAddress
    select_all_ref: str | None = None
    annotation_policy: str = "quarantine"
    normalizer_id: str | None = None
    version: int = 1
    provenance: BindingProvenance | None = None


@dataclass
class BindingDraft:
    """Cold-path output: binding shape plus the exemplar's answers."""

    draft_id: str
    label: str
    control_type: ControlType
    bbox: BBox
    options: list[Option] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    canonical_name: str | None = None
    region_id: str | None = None
    source_refs: list[ElementRef] = field(default_factory=list)
    address: RelationalAddress | None = None
    confidence: float | None = None
    provenance: BindingProvenance | None = None
    #: The projection chunk's tab, set by ``author_drafts``. Consumed by
    #: ``drafts_to_fields`` when a draft's ``region_id`` does not resolve, so a
    #: region-less field still reports its tab (and a tab-matched convention can
    #: still apply to it). Never serialised: it is not part of a record.
    tab: str | None = None
    #: 0.6.0-L1b lines contract: the ``A=`` refs of a TEXT field, so a reused
    #: tab re-reads the value from the TARGET tab's own cells (``reuse.py``)
    #: instead of the label-region drop rule. Empty on the JSON path, which
    #: keeps its current replay rule (and therefore its byte-identical output).
    #: Defaulted and additive: SCHEMA_VERSION stays 2.
    answer_refs: tuple[ElementRef, ...] = ()
    #: 0.6.0-L1b lines contract: a review reason the parser decided without
    #: re-scanning the refs (an unknown kind degrades to ``AMBIGUOUS_ROLE``).
    #: The resolver honours it after its own unresolved-ref and ambiguity flags.
    #: ``None`` on the JSON path.
    review_reason: ReviewReason | None = None
    #: 0.6.0-K1: the ``A=`` cell refs of a one-option ``single_select`` the
    #: resolver's kind rule may re-derive as ``text``. Defaulted and not part of
    #: a record (a draft is never serialised). Empty on the JSON path.
    option_refs: tuple[ElementRef, ...] = ()
    #: 0.6.0-K1: the rule id when the kind rule re-derived this draft, and the
    #: model's stated control type it overrode. Defaulted; carried onto the
    #: ``Field.provenance`` only when set.
    kind_rule: str | None = None
    stated_control_type: str | None = None


@dataclass
class CandidateHypothesis:
    """Non-authoritative grouping guess; raw spans always retained."""

    hypothesis_id: str
    region_id: str
    label_element_ids: list[str]
    value_element_ids: list[str]
    glyph_element_ids: list[str]
    control_guesses: list[ControlType]
    ambiguous: bool = False


@dataclass
class LayoutResult:
    page_count: int
    columns: dict[int, list[int]]
    regions: list[Region]
    bands: dict[int, list[list[str]]]
    glyphs: list[Glyph]
    hypotheses: list[CandidateHypothesis]
    anchors: list[Anchor]
    marker_classes: list[MarkerClassification] = field(default_factory=list)


@dataclass
class LLMCall:
    call_id: str
    purpose: str
    model: str
    params: dict[str, Any]
    prompt_hash: str
    prompt_ref: str
    response_ref: str
    tokens: int | None = None
    latency_ms: int | None = None


@dataclass
class MatchCandidate:
    template_id: str
    score: float


@dataclass
class Span:
    text: str
    bbox: BBox
    element_id: str | None = None


@dataclass
class SourceInfo:
    content_hash: str
    original_filename: str
    mime: str
    size: int
    page_count: int | None = None
    sheet_names: list[str] = field(default_factory=list)
    backend: str = ""
    backend_reason: str = ""
    has_text_layer: bool | None = None
    parser: str = ""
    parser_version: str = ""
    sheet_state: dict[str, str] = field(default_factory=dict)


@dataclass
class UnboundUnit:
    """One perceived anchor row or GRID region no resolved field used.

    Ids only: no label text, no element text, so the handle can be logged.
    """

    region_id: str
    band_id: int


@dataclass
class TabCoverage:
    """Row coverage for one tab: the share of its perceived units a run used.

    ``units_total`` is anchor rows plus GRID regions, after units whose
    elements are all ``non_answer_columns`` ids are excluded. ``ratio`` is
    ``None`` when ``units_total`` is 0 (ineligible, never ``1.0``). ``anchors``
    and ``grid_regions`` count the units that survived that exclusion, and
    ``max_anchors_per_tab`` is the anchor count of the largest authoring chunk.
    This is a perception signal, not a quality score.
    """

    tab: str = ""
    units_total: int = 0
    units_consumed: int = 0
    ratio: float | None = None
    anchors: int = 0
    grid_regions: int = 0
    chunked: bool = False
    max_anchors_per_tab: int = 0
    unbound: list[UnboundUnit] = field(default_factory=list)
    unbound_truncated: bool = False
    #: 0.6.1-C: the number of lattice rows on this tab the model dispositioned as
    #: non-fields (its ``hdr``/``note``/``skip`` records; distinct rows). ``0``
    #: under the json contract, which has no dispositions. ``omit_if_default`` so
    #: a json record's bytes are unchanged from 0.6.0 and ``SCHEMA_VERSION`` stays
    #: ``"2"``.
    dispositions: int = field(default=0, metadata={"omit_if_default": True})
    #: 0.6.1-C: True when the tab has anchors, the model returned at least one
    #: disposition and no field: "the model labelled every row non-field" is
    #: distinguishable from "anchors present but unbound" (which leaves this
    #: False and shows in ``unbound``). ``None`` under the json contract (the
    #: information does not exist there). ``omit_if_default`` for the same reason
    #: as ``dispositions``. Does not change ``InstanceStatus`` or
    #: ``min_coverage``.
    model_declined: bool | None = field(default=None, metadata={"omit_if_default": True})


@dataclass
class InstanceRecord:
    instance_id: str
    schema_version: str
    pipeline_version: str
    run_id: str
    created_at: str
    source: SourceInfo
    fields: list[Field] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    anchors: list["Anchor"] = field(default_factory=list)
    residuals: list[Span] = field(default_factory=list)
    llm_calls: list[LLMCall] = field(default_factory=list)
    match_candidates: list[MatchCandidate] = field(default_factory=list)
    tabs: list[str] = field(default_factory=list)
    batch_id: str | None = None
    signature: str | None = None
    hidden_sheets: list[str] = field(default_factory=list)
    status: InstanceStatus = InstanceStatus.COMPLETE
    errors: list[str] = field(default_factory=list)
    idempotency_key: str = ""
    coverage: list[TabCoverage] = field(default_factory=list)
    coverage_min_ratio: float | None = None


@dataclass
class TemplateHealth:
    sample_count: int = 0
    replay_success_rate: float | None = None
    escalation_rate: float | None = None
    last_matched_at: str | None = None


@dataclass
class TemplateRecord:
    template_id: str
    family_id: str
    version: int
    status: TemplateStatus
    backends: list[str]
    schema_version: str
    perception_version: str
    anchors: list[Anchor] = field(default_factory=list)
    match_units: list[str] = field(default_factory=list)
    bindings: list[Binding] = field(default_factory=list)
    variant_of: str | None = None
    supersedes: str | None = None
    delta_vs_parent: dict[str, Any] | None = None
    provenance: BindingProvenance | None = None
    health: TemplateHealth = field(default_factory=TemplateHealth)


@dataclass
class RawArchiveRecord:
    content_hash: str
    size: int
    mime: str
    original_filename: str
    ingested_at: str
    source_system: str = "local"
    storage_ref: str = ""


@dataclass
class BatchRecord:
    batch_id: str
    members: list[str]
    formed_at: str


T = TypeVar("T")

_UNION_TYPES = (Union, types.UnionType)


def _decode(tp: Any, val: Any) -> Any:
    if val is None:
        return None
    origin = get_origin(tp)
    if origin in _UNION_TYPES:
        args = [a for a in get_args(tp) if a is not type(None)]
        return _decode(args[0], val) if args else val
    if origin is list:
        item_tp = get_args(tp)[0] if get_args(tp) else Any
        return [_decode(item_tp, v) for v in val]
    if origin is dict:
        key_tp, val_tp = get_args(tp) if get_args(tp) else (Any, Any)
        return {_decode(key_tp, k): _decode(val_tp, v) for k, v in val.items()}
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(val)
    if isinstance(tp, type) and is_dataclass(tp):
        hints = get_type_hints(tp)
        known = {f.name for f in fields(tp)}
        kwargs = {k: _decode(hints.get(k, Any), v) for k, v in val.items() if k in known}
        return tp(**kwargs)
    return val


def _encode(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {}
        for f in fields(obj):
            value = getattr(obj, f.name)
            # Emit-on-set: a field marked ``omit_if_default`` is written only
            # while it differs from its declared default, so a field added to an
            # existing dataclass does not move an unaffected record's bytes. No
            # field carried the flag before 0.6.0-K1, so every existing record
            # serialises exactly as it did and SCHEMA_VERSION stays "2".
            if f.metadata.get("omit_if_default") and value == f.default:
                continue
            out[f.name] = _encode(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [_encode(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    return obj


def to_dict(obj: Any) -> Any:
    return _encode(obj)


def from_dict(cls: type[T], data: dict[str, Any]) -> T:
    return _decode(cls, data)


def to_json(obj: Any, *, indent: int | None = 2) -> str:
    return json.dumps(_encode(obj), indent=indent, sort_keys=True, ensure_ascii=False)


def from_json(cls: type[T], text: str) -> T:
    return _decode(cls, json.loads(text))
