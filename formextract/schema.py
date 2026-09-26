"""Target schema: canonical field names, control types, normalizers (Phase 0 artifact)."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from .model import ControlType, Option

SCHEMA_VERSION = "1"

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_label(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _norm_yes_no(value_raw: str | None, options: list[Option]) -> str | None:
    selected = [o.text for o in options if o.selected]
    if len(selected) == 1:
        token = normalize_label(selected[0])
    elif value_raw is not None:
        token = normalize_label(value_raw)
    else:
        return None
    if token in ("yes", "y", "true"):
        return "true"
    if token in ("no", "n", "false"):
        return "false"
    return token or None


def _norm_bool_check(value_raw: str | None, options: list[Option]) -> str | None:
    selected = [o for o in options if o.selected]
    if selected:
        return "true"
    if value_raw is not None and normalize_label(value_raw) in ("true", "yes", "x"):
        return "true"
    if value_raw is not None and normalize_label(value_raw) in ("false", "no"):
        return "false"
    return "false" if options else (value_raw or None)


def _norm_state_list(value_raw: str | None, options: list[Option]) -> str | None:
    selected = [o.text for o in options if o.selected]
    if not selected and value_raw:
        selected = [t for t in _WS_RE.split(value_raw) if t]
    states = sorted({t.strip().upper().rstrip(",") for t in selected if t.strip()})
    return ", ".join(states) if states else None


def _norm_text(value_raw: str | None, options: list[Option]) -> str | None:
    if value_raw is None:
        return None
    return _WS_RE.sub(" ", value_raw).strip() or None


def _norm_none(value_raw: str | None, options: list[Option]) -> str | None:
    return value_raw


NORMALIZERS: dict[str, Callable[[str | None, list[Option]], str | None]] = {
    "yes_no": _norm_yes_no,
    "bool_check": _norm_bool_check,
    "state_list": _norm_state_list,
    "text": _norm_text,
    "none": _norm_none,
}


def normalize(normalizer_id: str | None, value_raw: str | None, options: list[Option]) -> str | None:
    if normalizer_id is None:
        return value_raw
    fn = NORMALIZERS.get(normalizer_id)
    if fn is None:
        raise KeyError(f"unknown normalizer: {normalizer_id}")
    return fn(value_raw, options)


@dataclass(frozen=True)
class CanonicalField:
    name: str
    control_type: ControlType
    aliases: tuple[str, ...] = ()
    normalizer_id: str = "none"
    description: str = ""


CANONICAL_FIELDS: dict[str, CanonicalField] = {
    cf.name: cf
    for cf in [
        CanonicalField(
            name="us_states_written",
            control_type=ControlType.MULTI_SELECT,
            aliases=("which us states do they write in", "us states written", "states written in"),
            normalizer_id="state_list",
            description="US states the carrier writes in",
        ),
        CanonicalField(
            name="writes_in_canada",
            control_type=ControlType.SINGLE_SELECT,
            aliases=("do they write in canada", "writes in canada"),
            normalizer_id="yes_no",
        ),
        CanonicalField(
            name="carrier_license",
            control_type=ControlType.SINGLE_SELECT,
            aliases=("carrier license",),
            normalizer_id="none",
        ),
        CanonicalField(
            name="loss_runs_supported",
            control_type=ControlType.BOOL,
            aliases=("loss runs", "support loss runs"),
            normalizer_id="bool_check",
        ),
    ]
}

_ALIAS_INDEX: dict[str, str] = {
    normalize_label(alias): cf.name for cf in CANONICAL_FIELDS.values() for alias in cf.aliases
}


def canonical_name(label: str) -> str | None:
    return _ALIAS_INDEX.get(normalize_label(label))


def canonical_field(name: str) -> CanonicalField | None:
    return CANONICAL_FIELDS.get(name)
