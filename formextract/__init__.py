"""Form/document field extraction package (hearth-cli design spec)."""
from .model import (
    KIND_RULE_VERSION,
    PIPELINE_VERSION,
    BBox,
    Element,
    Field,
    InstanceRecord,
    TemplateRecord,
)
from .pipeline import PROMPT_VARIANTS
from .schema import SCHEMA_VERSION

__all__ = [
    "KIND_RULE_VERSION",
    "PIPELINE_VERSION",
    "PROMPT_VARIANTS",
    "SCHEMA_VERSION",
    "BBox",
    "Element",
    "Field",
    "InstanceRecord",
    "TemplateRecord",
]
